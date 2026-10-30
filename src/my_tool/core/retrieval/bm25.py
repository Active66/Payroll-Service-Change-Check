"""Okapi BM25 全文检索。精确词（金额、专有名词）靠这一路兜底。
"""

import math


# =============================================================================
# 【模块 08 · BM25 全文检索】目标文件：src/my_tool/core/retrieval/bm25.py
# ★核心模块（阶段 2 复用）：算法部分一行不用改；上生产时把倒排表换成
#   PostgreSQL 的 tsvector + zhparser，或者直接调 rank_bm25 库。
#
#   为什么必须有这一路：制度问题里大量是**精确词**——"36,921""冻结期""L4"
#   这类字符串，向量相似度天然不敏感。架构文档 0.2 把这一点列为选型理由之一。
# =============================================================================


class BM25Index:
    """
    Okapi BM25（k1=1.5, b=0.75，中文场景的经验值）。
    公式：score = Σ idf(qi) * tf*(k1+1) / (tf + k1*(1-b+b*dl/avgdl))
    """

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = docs_tokens
        self.n_docs = len(docs_tokens)
        self.doc_len = [len(t) for t in docs_tokens]
        self.avgdl = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0

        # 词频表与文档频率
        self.tfs: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for toks in docs_tokens:
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self.tfs.append(tf)
            for t in tf:
                df[t] = df.get(t, 0) + 1
        # idf 用带平滑的变体，避免高频词出现负分
        self.idf = {
            t: math.log(1 + (self.n_docs - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }

    def search(self, query_tokens: list[str], top_k: int) -> list[tuple[int, float]]:
        scores: list[tuple[int, float]] = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            dl = self.doc_len[i] or 1
            for q in query_tokens:
                f = tf.get(q)
                if not f:
                    continue
                idf = self.idf.get(q, 0.0)
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += idf * f * (self.k1 + 1) / denom
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]
