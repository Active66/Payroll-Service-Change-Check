"""端到端测试：检索质量 + 拒答闸门 + 索引流水线。

架构文档 4.3 定的验收线是"引用正确率 ≥ 90%"，这里把它固化成断言，
让"改坏检索"在 CI 阶段就暴露，而不是等到业务方发现答案开始胡编。
"""

from __future__ import annotations

from dataclasses import replace

from my_tool.core.config import Settings
from my_tool.core.eval_dataset import EVAL_CASES
from my_tool.core.pipeline import build_index, load_index


def test_eval_set_passes_recall_and_clause_gate(orchestrator):
    """hit@3 与 clause@3 双 100%：证据必须被召回，且条款号必须对得上。"""
    failures = []
    for case in EVAL_CASES:
        res = orchestrator.answer(case["q"], top_k=3)
        contents = [h.chunk.content for h in res.hits]
        clause_sets = [set(h.chunk.clauses) for h in res.hits]

        kw_ok = any(case["expect_kw"] in c for c in contents)
        clause_ok = any(case["expect_clause"] in cs for cs in clause_sets)
        if not (kw_ok and clause_ok):
            failures.append(
                f"{case['q']} → 关键词={'✓' if kw_ok else '×'} 条款={'✓' if clause_ok else '×'}"
            )

    assert not failures, "检索评测未达标：\n" + "\n".join(failures)


def test_answer_always_has_traceable_citations(orchestrator):
    """每条回答的引用都要能回到具体 chunk_id —— 这是 L1 铁律的最低要求。"""
    res = orchestrator.answer("冻结期内可以改银行账户吗？")
    assert res.citations
    chunk_ids = {h.chunk.chunk_id for h in res.hits}
    for c in res.citations:
        assert c.chunk_id in chunk_ids
        assert c.doc_id == "POL-CMP-2026-001"


def test_out_of_scope_question_is_refused(orchestrator):
    """
    库里没有依据时必须拒答，且**不能调用模型**。
    当前靠检索闸门兜底（阶段 2 由 Router 分流），这条断言防止它被悄悄改坏。
    """
    res = orchestrator.answer("员工报销流程怎么走？")
    assert res.status == "refused"
    assert res.citations == []
    assert res.tokens_in == 0 and res.tokens_out == 0


def test_empty_question_is_error(orchestrator):
    res = orchestrator.answer("   ")
    assert res.status == "error"


def test_build_index_writes_manifest_and_can_reload(tmp_path, settings: Settings):
    """流水线可重建、可加载，且 manifest 记下了向量模型 —— 换模型后才能自动发现要重建。"""
    s = replace(settings, index_dir=tmp_path / "index")
    store, manifest = build_index(s)

    assert manifest["doc_id"] == "POL-CMP-2026-001"
    assert manifest["chunk_count"] == len(store.chunks)
    assert manifest["embedding_model"] == "hash-tf-512"
    assert len(manifest["doc_checksum"]) == 64

    reloaded, m2 = load_index(s)
    assert len(reloaded.chunks) == len(store.chunks)
    assert reloaded.chunks[0].content == store.chunks[0].content
    assert m2["backend"] in {"faiss", "numpy"}


def test_load_index_detects_embedding_model_drift(tmp_path, settings: Settings):
    """索引与当前配置的向量模型不一致时必须报错，而不是给出垃圾相似度。"""
    s = replace(settings, index_dir=tmp_path / "index")
    build_index(s)

    drifted = replace(s, embedding_provider="openai", embedding_model="text-embedding-3-small",
                      embedding_base_url="https://example.invalid/v1", embedding_api_key="x",
                      embedding_dim=8)
    try:
        load_index(drifted)
    except RuntimeError as ex:
        assert "向量模型" in str(ex)
    else:  # pragma: no cover
        raise AssertionError("模型漂移未被检测到")
