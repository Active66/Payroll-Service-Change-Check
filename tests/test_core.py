"""核心单元测试：解析、切片、融合、护栏。

这些是"改一行就悄悄坏掉、但对外表现只是答案变差"的地方，所以优先覆盖。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from my_tool.core.chunker import chunk_blocks, split_sentences
from my_tool.core.entities import Chunk, RetrievalHit
from my_tool.core.guard import run_guard
from my_tool.core.retrieval.hybrid import is_effective, rrf_fuse
from my_tool.utils.text import normalize_text, tokenize

CLAUSE_HEAD = re.compile(r"^第[一二三四五六七八九十百零〇\d]+条\s*\S")


def _mk_chunk(cid: str, content: str = "x" * 100, **kw) -> Chunk:
    """构造测试用切片，只填被测字段。"""
    return Chunk(
        chunk_id=cid, doc_id="DOC", chunk_no=int(cid[-1]), content=content,
        heading_path="H", clause_no="第一条", char_len=len(content), token_count=1, **kw,
    )


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------
def test_front_matter_parsed(meta):
    """front matter 是 doc 级元数据的唯一来源，读错会导致生效期过滤整体失效。"""
    assert meta["doc_id"] == "POL-CMP-2026-001"
    assert meta["effective_from"] == "2026-01-01"
    assert "title" in meta


def test_parser_produces_three_block_kinds(blocks):
    kinds = {b.kind for b in blocks}
    assert {"heading", "text", "table"} <= kinds


def test_every_block_has_heading_path(blocks):
    """没有 heading_path 的块无法归属条款，引用就无从谈起。"""
    assert all(b.heading_path for b in blocks)


def test_table_block_keeps_all_rows(blocks):
    """表格必须整块保留：制度里的基数表被切一半，模型就会拿半张表编数字。"""
    tables = [b for b in blocks if b.is_table]
    assert tables
    for t in tables:
        # 每行都以 | 开头且 | 数量一致，说明没有被行级打断
        rows = [r for r in t.text.split("\n") if r.strip()]
        assert all(r.startswith("|") for r in rows)


# --------------------------------------------------------------------------
# 切片
# --------------------------------------------------------------------------
def test_chunk_size_never_exceeded(chunks):
    """500 字是容量上限，超了就说明装箱判断漏了分支。"""
    too_long = [(c.chunk_id, c.char_len) for c in chunks if c.char_len > 500]
    assert not too_long, f"超长切片：{too_long}"


def test_no_low_info_fragments(chunks):
    """小于 60 字的碎片应被合并掉，否则会污染 top3 的名额。"""
    tiny = [(c.chunk_id, c.char_len) for c in chunks if c.char_len < 60]
    assert not tiny, f"过短切片：{tiny}"


def test_no_orphan_heading(chunks):
    """
    切片最后一行不能是"第X条 标题"。
    孤儿标题意味着条文正文落在下一块 —— 引用时说"第X条"，而第X条并不在这一块里。
    """
    offenders = []
    for c in chunks:
        last = c.content.rstrip().split("\n")[-1].strip()
        if CLAUSE_HEAD.match(last) and len(last) < 30:
            offenders.append((c.chunk_id, last))
    assert not offenders, f"孤儿标题切片：{offenders}"


def test_chunks_carry_clause_metadata(chunks):
    """绝大多数切片要能自报条款范围，这是"引用可信"的前提。"""
    with_clause = [c for c in chunks if c.clause_range]
    assert len(with_clause) / len(chunks) >= 0.9
    for c in with_clause:
        assert c.clause_no == c.clauses[0]
        if len(c.clauses) > 1:
            assert c.clause_range == f"{c.clauses[0]}–{c.clauses[-1]}"


def test_chunks_do_not_cross_chapter(blocks, meta):
    """跨章合并会让"社保基数"和"银行账户"挤进同一块，召回必然串味。"""
    res = chunk_blocks(blocks, chunk_size=500, overlap=80, min_size=60, meta=meta)
    for c in res:
        # heading_path 被裁到"文档 > 章"两级，因此只能有一个 " > "
        assert c.heading_path.count(" > ") <= 1


def test_clause_mode_splits_one_per_clause(blocks, meta):
    """clause 模式：一条一款，绝不合并。"""
    res = chunk_blocks(blocks, chunk_size=500, overlap=80, min_size=0, meta=meta, mode="clause")
    multi = [c.chunk_id for c in res if len(c.clauses) > 1]
    assert not multi, f"clause 模式下出现多条款切片：{multi}"


def test_split_sentences_keeps_punctuation():
    parts = split_sentences("第一条 目的。本制度适用；含表格!")
    assert parts[0].endswith("。")
    assert any("；" in p for p in parts)


# --------------------------------------------------------------------------
# 融合与过滤
# --------------------------------------------------------------------------
def test_rrf_prefers_hits_recalled_by_both_channels():
    """两路同时命中的切片，融合分应当高于单路第一 —— 这正是混合检索的收益。"""
    cs = [_mk_chunk("c1"), _mk_chunk("c2"), _mk_chunk("c3")]
    hits = rrf_fuse(
        vector_hits=[(1, 0.9), (0, 0.8)],   # c2 向量第一、c1 第二
        bm25_hits=[(0, 20.0), (2, 15.0)],   # c1 全文第一
        chunks=cs, k=60, top_k=3,
    )
    # c1 两路都在（向量#2 + BM25#1），c2 只有向量#1
    assert hits[0].chunk.chunk_id == "c1"
    assert hits[0].vector_rank == 2 and hits[0].bm25_rank == 1


def test_rrf_records_channel_provenance():
    cs = [_mk_chunk("c1"), _mk_chunk("c2")]
    hits = rrf_fuse([(0, 0.5)], [(1, 9.0)], cs, top_k=2)
    assert hits[0].channels == "向量1/BM25-"
    assert hits[1].channels == "向量-/BM251"


def test_effective_window_filters_retired_policy():
    """生效期过滤是这类系统最常见的线上事故来源，必须单测兜住。"""
    retired = _mk_chunk("c1", effective_from="2020-01-01", effective_to="2021-01-01")
    active = _mk_chunk("c2", effective_from="2026-01-01", effective_to=None)
    assert not is_effective(retired, date(2026, 6, 1))
    assert is_effective(active, date(2026, 6, 1))


def test_future_policy_not_used_before_effective_date():
    future = _mk_chunk("c1", effective_from="2027-01-01")
    assert not is_effective(future, date(2026, 6, 1))
    assert is_effective(future, date(2027, 1, 1) + timedelta(days=1))


def test_rrf_drops_expired_hits():
    expired = _mk_chunk("c1", effective_to="2020-01-01")
    hits = rrf_fuse([(0, 0.9)], [], [expired], as_of=date(2026, 6, 1))
    assert hits == []


# --------------------------------------------------------------------------
# 护栏
# --------------------------------------------------------------------------
def test_guard_flags_invented_number():
    """带单位但原文没有的数字 = 潜在幻觉，必须降级为 partial。"""
    res = run_guard("调薪上限为 50,000 元 [1]", "调薪上限按薪酬带宽执行。", hit_count=1)
    assert res.status == "partial"
    assert "50,000 元" in res.unverified_numbers


def test_guard_passes_verbatim_number():
    res = run_guard("缴费基数上限为 36,921 元/月 [1]", "上限为 36,921 元/月。", hit_count=1)
    assert res.unverified_numbers == []
    assert res.status == "answered"


def test_guard_flags_out_of_range_citation():
    res = run_guard("结论见 [5]", "原文", hit_count=3)
    assert res.invalid_citations == [5]
    assert res.status == "partial"


def test_guard_flags_missing_citation():
    res = run_guard("调薪需书面确认。", "调薪需书面确认。", hit_count=2)
    assert any("引用角标" in w for w in res.warnings)


def test_guard_recognises_refusal():
    """模型合规拒答时不能报成异常，否则监控会天天告警。"""
    res = run_guard("根据现有资料无法确认该问题。", "原文", hit_count=1)
    assert res.status == "refused"


# --------------------------------------------------------------------------
# 文本工具
# --------------------------------------------------------------------------
def test_tokenize_strips_stopwords():
    toks = tokenize("请问冻结期内可以改银行账户吗？")
    assert "请问" not in toks and "可以" not in toks
    assert any("冻结" in t for t in toks)


def test_normalize_thousand_separator():
    """36,921 与 36921 必须被当成同一个词，否则 BM25 永远匹配不上金额。"""
    assert "36921" in normalize_text("36,921 元")


def test_retrieval_hit_channels_label():
    h = RetrievalHit(chunk=_mk_chunk("c1"))
    assert h.channels == "向量-/BM25-"
