"""Markdown 解析：产出带 heading_path 的结构化块，是"按条款引用"的地基。
"""

import re
from dataclasses import dataclass


# =============================================================================
# 【模块 04 · Markdown 解析】目标文件：src/my_tool/core/parser.py
# ★核心模块（阶段 2 复用）：PDF/DOCX 解析器只要产出同样的 Block 列表，
#   下游切片器与索引流程完全不用改 —— 这就是"解析与切片解耦"的价值。
# =============================================================================


@dataclass
class Block:
    """
    解析后的结构化块。刻意只保留"标题层级 + 正文 + 表格"三种，
    因为薪酬制度真正要保住的结构就这三样：条款归属、条文正文、参数表格。
    """

    kind: str            # "heading" / "text" / "table"
    text: str
    heading_path: str    # 该块所属的完整标题路径
    level: int = 0       # heading 级别，其它为 0
    is_table: bool = False


def parse_markdown(text: str) -> tuple[dict[str, str], list[Block]]:
    """
    把 Markdown 拆成 (front_matter, blocks)。

    关键决策：
      1. 表格整块保留，绝不在行中间切断 —— 制度里的"基数上下限""风险分级"
         一旦被切断，模型就会拿着半张表编数字。
      2. 每个正文块都挂 heading_path，这是实现"按条款引用"的前提。
      3. YAML front matter 里读 doc_id / effective_from 等元数据，
         对应 policy_documents 表字段。
    """
    meta: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").split("\n")
    idx = 0

    # —— 1) front matter：极简 YAML 解析（只支持 key: value，够原型用）——
    if lines and lines[0].strip() == "---":
        idx = 1
        while idx < len(lines) and lines[idx].strip() != "---":
            line = lines[idx]
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
            idx += 1
        idx += 1  # 跳过结束的 ---

    blocks: list[Block] = []
    heading_stack: list[tuple[int, str]] = []   # [(level, title)]
    buf: list[str] = []

    def current_path() -> str:
        return " > ".join(t for _, t in heading_stack)

    def flush_text() -> None:
        """把累积的正文行合并成一个 text 块。"""
        nonlocal buf
        body = "\n".join(buf).strip()
        buf = []
        if body:
            blocks.append(Block(kind="text", text=body, heading_path=current_path()))

    while idx < len(lines):
        raw = lines[idx]
        stripped = raw.strip()

        # 空行 = 块分隔符
        if not stripped:
            flush_text()
            idx += 1
            continue

        # —— 标题 ——
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if m:
            flush_text()
            level = len(m.group(1))
            title = m.group(2)
            # 弹掉层级更深或同级的旧标题，维护正确的祖先链
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            blocks.append(Block(kind="heading", text=title, heading_path=current_path(), level=level))
            idx += 1
            continue

        # —— 表格：连续的 | 开头行整块收集 ——
        if stripped.startswith("|"):
            flush_text()
            table_lines: list[str] = []
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                table_lines.append(lines[idx].strip())
                idx += 1
            blocks.append(
                Block(kind="table", text="\n".join(table_lines), heading_path=current_path(), is_table=True)
            )
            continue

        buf.append(raw)
        idx += 1

    flush_text()
    # 第一块若是文档大标题，去掉重复的 H1（避免 heading_path 里出现两次标题）
    return meta, blocks
