"""混合检索：两路召回 + RRF 融合 + 生效区间过滤 + 拒答闸门。
"""

import time
from datetime import date
from typing import Any, Sequence

from ...utils.text import tokenize
from ..config import Settings
from ..entities import Chunk, RetrievalHit
from .bm25 import BM25Index
from .embedding import EmbeddingModel
from .vector_store import VectorStore


# =============================================================================
# 【模块 09 · 混合检索（RRF 融合）】目标文件：src/my_tool/core/retrieval/hybrid.py
# ★核心模块（阶段 2 复用，且是最该原样复用的一个）：
#
#   融合用 RRF（Reciprocal Rank Fusion）而不是"加权分数相加"，
#   原因：向量余弦（0~1）与 BM25（0~30）量纲完全不同，直接相加等于让 BM25 独裁。
#   RRF 只看名次：score = Σ w_i / (k + rank_i)，天然免疫量纲问题，也不用调归一化。
#
#   阶段 2 的扩展点（本类已留出接口）：
#     · 加第三路（图谱扩展）→ 往 channels 里多塞一路即可；
#     · 加真实 reranker    → 在 fuse 之后、截断之前插一层 cross-encoder。
# =============================================================================


def is_effective(chunk: Chunk, as_of: date) -> bool:
    """
    生效区间过滤。架构文档 3.5 的两个"容易设计错的点"之一：
    检索必须带 effective_from <= 业务日期 <= coalesce(effective_to, ∞)，
    否则会把已废止制度答给用户。
    """
    try:
        if chunk.effective_from:
            if date.fromisoformat(chunk.effective_from) > as_of:
                return False
        if chunk.effective_to:
            if date.fromisoformat(chunk.effective_to) < as_of:
                return False
    except ValueError:
        return True   # 元数据脏数据不阻断检索，但要记日志（阶段 2 补）
    return True


def rrf_fuse(
    vector_hits: list[tuple[int, float]],
    bm25_hits: list[tuple[int, float]],
    chunks: Sequence[Chunk],
    *,
    k: int = 60,
    w_vector: float = 1.0,
    w_bm25: float = 1.0,
    top_k: int = 3,
    as_of: date | None = None,
) -> list[RetrievalHit]:
    """
    两路召回 → RRF 融合 → top_k。
    同一个 chunk 被两路同时召回时，分数叠加，这正是"精确词 + 语义"互补的收益。
    """
    pool: dict[int, RetrievalHit] = {}

    for rank, (idx, score) in enumerate(vector_hits, start=1):
        ch = chunks[idx]
        if as_of and not is_effective(ch, as_of):
            continue
        h = pool.setdefault(idx, RetrievalHit(chunk=ch))
        h.vector_score = score
        h.vector_rank = rank
        h.fused_score += w_vector / (k + rank)

    for rank, (idx, score) in enumerate(bm25_hits, start=1):
        ch = chunks[idx]
        if as_of and not is_effective(ch, as_of):
            continue
        h = pool.setdefault(idx, RetrievalHit(chunk=ch))
        h.bm25_score = score
        h.bm25_rank = rank
        h.fused_score += w_bm25 / (k + rank)

    ranked = sorted(pool.values(), key=lambda h: -h.fused_score)
    return ranked[:top_k]


class HybridRetriever:
    """
    编排"两路召回 + 融合"。这是 orchestrator 唯一需要认识的检索入口。
    """

    def __init__(self, store: VectorStore, bm25: BM25Index, embedder: EmbeddingModel,
                 settings: Settings):
        self.store = store
        self.bm25 = bm25
        self.embedder = embedder
        self.s = settings

    def retrieve(self, question: str, top_k: int | None = None,
                 as_of: date | None = None) -> tuple[list[RetrievalHit], dict[str, Any]]:
        top_k = top_k or self.s.top_k
        as_of = as_of or self.s.as_of_date()
        t0 = time.perf_counter()

        # ① 向量通道
        t1 = time.perf_counter()
        qvec = self.embedder.embed_one(question)
        vector_hits = self.store.search(qvec, self.s.recall_k)
        t_vec = (time.perf_counter() - t1) * 1000

        # ② 全文通道（与向量通道用的 tokenize 完全一致，减少口径差异）
        t2 = time.perf_counter()
        q_tokens = tokenize(question)
        bm25_hits = self.bm25.search(q_tokens, self.s.recall_k)
        t_bm = (time.perf_counter() - t2) * 1000

        # ③ 融合
        hits = rrf_fuse(
            vector_hits, bm25_hits, self.store.chunks,
            k=self.s.rrf_k, w_vector=self.s.w_vector, w_bm25=self.s.w_bm25,
            top_k=top_k, as_of=as_of,
        )
        debug = {
            "query_tokens": q_tokens,
            "vector_candidates": len(vector_hits),
            "bm25_candidates": len(bm25_hits),
            "as_of": as_of.isoformat(),
            "t_vector_ms": round(t_vec, 2),
            "t_bm25_ms": round(t_bm, 2),
            "t_total_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

        # ④ 拒答闸门：注意看的是"两路各自的最好成绩"，而不是融合后的 top1。
        #    RRF 只看名次，一条垃圾切片只要在两路都排第一，融合分照样最高；
        #    用原始分数设闸才能把"库里确实没有"和"有但排得靠后"区分开。
        if hits:
            best_vec = max(h.vector_score for h in hits)
            best_bm = max(h.bm25_score for h in hits)
            debug["best_vector_score"] = round(best_vec, 4)
            debug["best_bm25_score"] = round(best_bm, 3)
            if best_vec < self.s.min_vector_score and best_bm < self.s.min_bm25_score:
                debug["gate"] = "blocked_no_evidence"
                hits = []
            else:
                debug["gate"] = "passed"
        return hits, debug
