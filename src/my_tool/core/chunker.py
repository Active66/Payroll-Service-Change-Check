"""切片器。全流程质量的分水岭，策略见下方横幅注释。
"""

import re
from typing import Sequence

from .entities import Chunk
from .parser import Block


# =============================================================================
# 【模块 05 · 切片器】目标文件：src/my_tool/core/chunker.py
# ★核心模块（阶段 2 原样复用）：架构文档 4.5 把"切片质量"列为第一周真正的风险，
#   所以这里不是简单 text[i:i+500]，而是五层策略：
#
#     ① 500 字装箱，但装箱单元是"条款"：一条放得下就继续放下一条，放不下就在
#        条款边界断开 —— 纯按字符硬切会把一条制度切成两半，引用就失去意义；
#     ② 章为硬边界：同一章内可合并多条款，跨章绝不合并（否则召回串味）；
#     ③ 表格不可切：整张表作为原子单元独占一个切片，制度里的基数表/分级表
#        一旦被切断，模型就会拿着半张表编数字；
#     ④ 标题内联：把"第X条 XXX"写进正文，脱离标题的条文无法自证身份，
#        也让"续段"能通过 `（续）` 标记保持可追溯；
#     ⑤ 重叠衔接：相邻切片重叠 chunk_overlap 字并对齐句首，避免答案正好
#        跨在切口上导致"召回命中却答不全"。
#
#   引用粒度：每个切片记录 clauses[] 与 clause_range，即使一个切片覆盖 4 条，
#   也能精确说出"第五章 第十七条–第二十条"。这是本实现与"朴素 500 字硬切"的关键差别。
#
#   阶段 2 迁移注意：本实现基于字符数。若改用 tiktoken 计数，
#   把 len() 换成 token 计数即可，切分骨架不动。
# =============================================================================

# 中文句子边界：句号/问号/感叹号/分号/换行，保留标点
_SENT_SPLIT = re.compile(r"[^。！？；!?;\n]*[。！？；!?;\n]|[^。！？；!?;\n]+$")
# 条款号抽取：第五条 / 第十二条
_CLAUSE_RE = re.compile(r"第[一二三四五六七八九十百零〇\d]+条")
# 为新条款预留的正文空间：剩余不足这么多字时，宁可提前封口，
# 也不要把"第X条"的标题留在上一块末尾（会导致孤儿标题，引用失真）
_BODY_RESERVE = 120


def _clause_no_of(heading_path: str) -> str:
    """从标题路径里取最后一个条款号，作为引用主键。"""
    found = _CLAUSE_RE.findall(heading_path)
    return found[-1] if found else ""


def split_sentences(text: str) -> list[str]:
    """按中文标点切句，保留标点。切不开时退回整段。"""
    parts = [s for s in _SENT_SPLIT.findall(text) if s.strip()]
    return parts or [text]


def _pack_sentences(sentences: list[str], limit: int) -> list[str]:
    """把句子贪心装箱成不超过 limit 的分组，不跨句切开。"""
    groups: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sentences:
        s_len = len(s)
        if cur and cur_len + s_len > limit:
            groups.append("".join(cur))
            cur, cur_len = [], 0
        # 单句本身就超长（制度里偶有超长枚举）→ 硬切
        while s_len > limit:
            groups.append(s[:limit])
            s = s[limit:]
            s_len = len(s)
        cur.append(s)
        cur_len += s_len
    if cur:
        groups.append("".join(cur))
    return groups


def _overlap_tail(text: str, overlap: int) -> str:
    """
    取上一块的尾部作为下一块的开头。对齐到句首，避免从半个词开始。
    这是"检索能命中但答不全"的常见成因，必须在切片层解决。

    注意：要把尾部的标题行（"第X条 …"）丢掉。标题一旦被带进下一块，
    就会出现"第四条 生效与版本"在相邻两块各出现一次的重复，
    模型读到两遍同样的条号，很容易把归属搞错。
    """
    if overlap <= 0 or len(text) <= overlap:
        return ""
    tail = text[-overlap:]
    # 去掉尾部的标题行（"第X条 …" / "第X章 …"）
    tail = re.sub(r"(?m)^第[一二三四五六七八九十百零〇\d]+[条章][^\n]*$", "", tail)
    # 从尾部片段里找到第一个句末标点之后的位置，作为干净的起点
    for i in range(len(tail)):
        if tail[i] in "。！？；\n":
            return tail[i + 1 :].strip()
    return tail.strip()


def _section_key(heading_path: str) -> str:
    """
    取标题路径的前两段（文档标题 + 章）作为"绝不跨越的语义边界"。
    切片可以横跨同一章内的多个条款，但绝不能横跨两章 ——
    否则一个切片里同时出现"社保基数"和"银行账户"，检索会把风马牛不相及的内容一起召回。
    """
    parts = [p for p in heading_path.split(" > ") if p.strip()]
    return " > ".join(parts[:2])


def chunk_blocks(
    blocks: Sequence[Block],
    *,
    chunk_size: int = 500,
    overlap: int = 80,
    min_size: int = 60,
    meta: dict[str, str] | None = None,
    mode: str = "size",
) -> list[Chunk]:
    """
    块列表 → 切片列表。这是全流程质量的分水岭，策略见本模块头部注释。

    参数 mode：
      · "size"   （默认）按 500 字装箱。装箱的**单元是条款**：一条放得下就继续放下一条，
                 放不下就在条款边界处断开。这样既满足"500 字切片"，又因为标题内联在正文里、
                 并记录了 clause_range，引用精度仍然是条款级。
      · "clause" 一条一款，绝不合并。适合需要"精确到条"的核对场景（阶段 2 可切换）。
    """
    meta = meta or {}
    doc_id = meta.get("doc_id") or "DOC-UNKNOWN"
    doc_title = meta.get("title", "")
    # 版本号在制度文件里常写成 1.0 / 1 / v2，统一折成整数存库（1.0 → 1）
    try:
        version = int(float(str(meta.get("version", "1") or "1")))
    except ValueError:
        version = 1
    region = meta.get("region_code", "")
    eff_from = meta.get("effective_from", "")
    eff_to = meta.get("effective_to") or None
    doc_type = meta.get("doc_type", "policy")
    # 物理文件名：前端"引用来源"要显示"文件名 + 片段"，所以随切片一起冗余存下。
    # 旧索引（阶段 1 构建的）没有这个字段，Chunk.source_file 有默认值，不会炸。
    source_file = meta.get("source_file", "")

    chunks: list[Chunk] = []
    chunk_no = 0

    # 当前正在累积的切片状态
    cur_parts: list[str] = []
    cur_len = 0
    cur_section = ""            # 当前所属"章"
    cur_path = ""               # 当前所属"章"的完整路径（用作 heading_path）
    cur_clauses: list[str] = [] # 本切片覆盖的条款号
    carry = ""                  # 从上一块继承的 overlap 文本

    def emit(keep_carry: bool = False) -> None:
        """
        把当前累积内容落成一个切片，并清空状态。

        keep_carry 只在"同一条款被迫续切"时置 True：
        切在条款边界上时不需要重叠（语义单元已经完整），加了只会让相邻两块重复，
        既浪费 token 又让模型看到两遍相同内容。
        """
        nonlocal chunk_no, cur_parts, cur_len, carry, cur_clauses
        body = "\n".join(p for p in cur_parts if p).strip()
        if not body:
            cur_parts, cur_len, cur_clauses = [], 0, []
            return
        chunk_no += 1
        clauses = list(dict.fromkeys(cur_clauses))   # 去重且保序
        if not clauses:
            clause_range = ""
        elif len(clauses) == 1:
            clause_range = clauses[0]
        else:
            clause_range = f"{clauses[0]}–{clauses[-1]}"
        chunks.append(
            Chunk(
                chunk_id=f"{doc_id}#c{chunk_no:03d}",
                doc_id=doc_id,
                chunk_no=chunk_no,
                content=body,
                heading_path=cur_path,
                clause_no=clauses[0] if clauses else "",
                clause_range=clause_range,
                clauses=clauses,
                char_len=len(body),
                token_count=int(len(body) * 0.75),   # 中文粗估：1 字 ≈ 0.75 token
                page_no=None,
                doc_title=doc_title,
                doc_type=doc_type,
                region_code=region,
                version=version,
                effective_from=eff_from,
                effective_to=eff_to,
                source_file=source_file,
            )
        )
        carry = _overlap_tail(body, overlap) if keep_carry else ""
        # 表格行不适合当 overlap 前缀（会把半张表带到下一块），直接丢弃
        if carry.lstrip().startswith("|"):
            carry = ""
        cur_parts, cur_len, cur_clauses = [], 0, []

    for blk in blocks:
        # ---------------- 标题块：决定"封口时机" ----------------
        if blk.kind == "heading":
            section = _section_key(blk.heading_path)
            clause = _clause_no_of(blk.text)

            # ① 跨章 → 强制封口
            if cur_parts and section and cur_section and section != cur_section:
                emit()
            # ② 条款级模式 → 每遇新条款就封口
            if mode == "clause" and clause and cur_clauses:
                emit()
            # ③ 剩余空间放不下"这一条 + 一段正文"→ 在条款边界处提前封口。
            #    这里必须给正文预留空间（_BODY_RESERVE），否则会出现
            #    "条标题落在上一块末尾、条文正文落在下一块"的孤儿标题，
            #    引用时说的第X条并不在它所在的那一块里，是致命的可举证性缺陷。
            if clause and cur_parts and cur_len + len(blk.text) + _BODY_RESERVE > chunk_size:
                emit()

            cur_section = section or cur_section
            cur_path = section or blk.heading_path

            # 章标题与条标题要写进正文：脱离标题的条文会失去"这是第几条"的自证能力
            if blk.level >= 2:
                cur_parts.append(blk.text)
                cur_len += len(blk.text) + 1
            if clause:
                cur_clauses.append(clause)
            continue

        # ---------------- 表格块：原子单元，永不在行间切断 ----------------
        if blk.is_table:
            # 放得下 → 并入当前条款块，引用仍然落在该条款上（如"第二十六条 + 分级表"）
            if cur_parts and cur_len + len(blk.text) + 1 <= chunk_size:
                cur_parts.append(blk.text)
                cur_len += len(blk.text) + 1
                continue
            # 放不下 → 整表独占一个切片，但继承前一条款归属，避免出现"无主表格"
            prev_clauses = list(cur_clauses)
            prev_path = cur_path
            if cur_parts:
                emit()
            cur_path = prev_path
            cur_clauses = prev_clauses
            cur_parts.append(blk.text)
            cur_len = len(blk.text)
            emit()
            continue

        # ---------------- 正文块 ----------------
        if not cur_path:
            cur_path = _section_key(blk.heading_path)

        # 超长段落按句切；正常段落整块参与装箱
        pieces = (
            [blk.text]
            if len(blk.text) <= chunk_size
            else _pack_sentences(split_sentences(blk.text), chunk_size)
        )

        for piece in pieces:
            piece_len = len(piece)
            if cur_parts and cur_len + piece_len + 1 > chunk_size:
                # 记录断点所属条款，让"被切开的后半段"仍然能自称是第几条
                prev_clause = cur_clauses[-1] if cur_clauses else ""
                # 同一条款内部被迫续切 → 用 overlap 把上一句带过来，避免答案被切口截断
                emit(keep_carry=True)
                if carry:
                    cur_parts.append(carry)
                    cur_len = len(carry)
                if prev_clause:
                    cont = f"{prev_clause}（续）"
                    cur_parts.append(cont)
                    cur_len += len(cont) + 1
                    cur_clauses.append(prev_clause)
            cur_parts.append(piece)
            cur_len += piece_len + 1

    emit()

    # —— 收尾：合并同章内的过短碎片，避免出现"只有一句话"的低信息切片 ——
    merged: list[Chunk] = []
    for c in chunks:
        if merged and c.char_len < min_size and merged[-1].heading_path == c.heading_path:
            prev = merged[-1]
            prev.content = (prev.content + "\n" + c.content).strip()
            prev.char_len = len(prev.content)
            prev.token_count = int(prev.char_len * 0.75)
            prev.clauses = list(dict.fromkeys(prev.clauses + c.clauses))
            if prev.clauses:
                prev.clause_no = prev.clauses[0]
                prev.clause_range = (
                    prev.clauses[0] if len(prev.clauses) == 1
                    else f"{prev.clauses[0]}–{prev.clauses[-1]}"
                )
            continue
        merged.append(c)
    # 重新编号，保证 chunk_id 连续可读
    for i, c in enumerate(merged, start=1):
        c.chunk_no = i
        c.chunk_id = f"{doc_id}#c{i:03d}"
    return merged
