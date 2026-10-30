"""PDF → 伪 Markdown。

    目的不是"完美还原 PDF"，而是**把 PDF 的文本层喂给同一个切片器**。
    多写一套切片逻辑是这类系统最常见的技术债：两套切法意味着
    两种召回质量，出了问题不知道该修哪一边。

    边界（必须诚实）：
      · 只处理有文本层的 PDF。扫描件（图片页）提取不到字符，这里直接报错，
        绝不静默产出空文档 —— "上传成功但永远答不出"是最坏的失败方式。
      · PDF 的表格只能拿到文本流，行列关系会丢。这类内容切片后会变成
        一串数字，检索价值有限，页面上会把"识别到的条款数"显示出来让人自己判断。
"""

import re
from pathlib import Path

# 中文章节标题：第三章 / 第二节 / 第五条
_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百零〇\d]+章")
_SECTION_RE = re.compile(r"^第[一二三四五六七八九十百零〇\d]+节")
_CLAUSE_RE = re.compile(r"^第[一二三四五六七八九十百零〇\d]+条")

# 每页平均有效字符数低于这个值，判定为"没有文本层"
# 20 字是经验值：正常排版一页 A4 中文至少几百字，扫描件通常只剩页眉页脚几个字符。
MIN_CHARS_PER_PAGE = 20


class PdfError(ValueError):
    """PDF 解析失败。上层翻译成 KB_004，message 直接给管理员看。"""


def _clean(text: str) -> str:
    """去页码行、去首尾空白、压缩连续空行。PDF 抽出来的文本常有大量孤立空行。"""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 纯页码："12"、"- 12 -"、"第 12 页" —— 它们对检索只有噪音
        if re.fullmatch(r"[-—\s]*\d{1,4}[-—\s]*", line):
            continue
        if re.fullmatch(r"第\s*\d+\s*页.*", line):
            continue
        lines.append(line)
    return "\n".join(lines)


def to_markdown(text: str) -> tuple[str, dict]:
    """
    纯文本 → 伪 Markdown，并返回统计信息。

    只识别"第X章 / 第X节 / 第X条"这类制度文档的标准标题。
    为什么不把"1. xxx""一、xxx"也算标题：误判的代价比漏判高得多 ——
    把正文行当成标题，会让切片器在错误的位置断开，引用范围全乱。

    进去先洗一遍（去页码、去空行）：这个函数也可能被单独复用（比如直接喂
    txt 或粘贴的文本），不能假设调用方已经清洗过。
    """
    text = _clean(text)
    out: list[str] = []
    stats = {"chapters": 0, "sections": 0, "clauses": 0}

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _CHAPTER_RE.match(s):
            out.append("## " + s)
            stats["chapters"] += 1
        elif _SECTION_RE.match(s):
            out.append("### " + s)
            stats["sections"] += 1
        elif _CLAUSE_RE.match(s):
            # 条标题常与正文同在一行："第五条 常规提交时限 影响当期发放的变更，须……"
            head, _, rest = s.partition(" ")
            rest = rest.strip()
            # 只有"看起来像标题"的那部分才进标题（不超过 16 字且不含句号），
            # 否则整行当正文，避免把一条正文拆得支离破碎。
            if rest and len(rest) <= 16 and "。" not in rest:
                out.append("#### " + s)
            else:
                out.append("#### " + head)
                if rest:
                    out.append(rest)
            stats["clauses"] += 1
        else:
            out.append(s)

    return "\n\n".join(out), stats


def extract(path: str | Path, title: str = "") -> tuple[str, dict]:
    """
    读取 PDF，返回 (伪 Markdown 正文, 统计)。失败抛 PdfError。

    统计里带 pages / chars / chars_per_page，页面会展示出去：
    管理员据此判断"这份 PDF 到底能不能用"，而不是上传完才发现问答没效果。
    """
    try:
        from pypdf import PdfReader
    except ImportError as ex:                      # pragma: no cover
        raise PdfError("服务端缺少 pypdf 依赖，无法解析 PDF") from ex

    p = Path(path)
    try:
        reader = PdfReader(str(p))
    except Exception as ex:                        # noqa: BLE001 — pypdf 的异常类型很杂
        raise PdfError(f"PDF 文件无法打开（{type(ex).__name__}）") from ex

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")                     # 有些 PDF 只是"禁止编辑"，空密码可解
        except Exception as ex:                    # noqa: BLE001
            raise PdfError("PDF 已加密，需要先去除密码保护") from ex

    pages = len(reader.pages)
    chunks: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            t = page.extract_text() or ""
        except Exception:                          # noqa: BLE001 — 单页坏了不该整份失败
            t = ""
        chunks.append(t)

    raw = "\n".join(chunks)
    chars = len(_clean(raw))
    per_page = chars / pages if pages else 0

    if pages == 0:
        raise PdfError("PDF 没有任何页面")
    if chars < MIN_CHARS_PER_PAGE or per_page < MIN_CHARS_PER_PAGE:
        raise PdfError(
            "未提取到有效正文（扫描件需先 OCR）。"
            f"共 {pages} 页，仅提取到 {chars} 个字符"
        )

    md, stats = to_markdown(_clean(raw))
    stats.update({"pages": pages, "chars": chars,
                  "chars_per_page": round(per_page, 1),
                  "title_guess": title})

    if stats["clauses"] == 0 and stats["chapters"] == 0:
        # 没有识别到任何条款结构：不报错（有些制度就是流水段落），但要让页面提示，
        # 因为切片会退化成"按字数硬切"，引用只能精确到段落而不能到条款。
        stats["no_structure"] = True

    return md, stats
