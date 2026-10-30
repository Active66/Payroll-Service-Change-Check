"""向量库封装。接口 search(vec, k) 保持稳定，换 pgvector 只替换本类内部。
"""

import json
from pathlib import Path
from typing import Any

from ...utils.console import log
from ...utils.env import faiss, np
from ..entities import Chunk


# =============================================================================
# 【模块 07 · 向量库（FAISS）】目标文件：src/my_tool/core/retrieval/vector_store.py
# ★核心模块（阶段 2 复用）：接口 search(vec, k) -> [(chunk_idx, score)] 保持不变，
#   实现从 FAISS 换成 pgvector 时，只替换本类内部，hybrid 层无感。
#
#   FAISS 选型说明：
#     · IndexFlatIP + 向量 L2 归一化 == 余弦相似度（精确检索，小库首选）；
#     · 切片量超过 1 万条再换 IndexHNSWFlat(M=16)，和架构文档 pgvector 的
#       HNSW(m=16, ef_construction=64) 参数对齐，召回表现一致。
# =============================================================================


class VectorStore:
    """
    向量库封装。同时支持 FAISS 与 numpy 两种后端，对外接口完全一致。
    持久化三件套：index 文件 + chunks.jsonl + manifest.json。
    """

    INDEX_FILE = "index.faiss"
    VECTORS_FILE = "vectors.npy"
    CHUNKS_FILE = "chunks.jsonl"
    MANIFEST_FILE = "manifest.json"

    def __init__(self, chunks: list[Chunk], dim: int):
        self.chunks = chunks
        self.dim = dim
        self.index = None            # FAISS index
        self._matrix = None          # numpy 回退
        self.backend = "none"

    # ---------------- 构建 ----------------
    def build(self, vectors: list[list[float]]) -> None:
        if np is None:
            raise RuntimeError("缺少 numpy：请先 pip install numpy")
        mat = np.asarray(vectors, dtype="float32")
        # L2 归一化 → 内积即余弦相似度
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms
        self.dim = int(mat.shape[1])

        # 无论用哪个后端都留一份归一化矩阵：
        # FAISS 索引本身取不出向量，而"删掉一个文档后重建"必须能按切片取子集。
        # 重算一遍 embedding 意味着为删一行付整个库的钱，所以这份副本是必需的。
        self._matrix = mat

        if faiss is not None:
            n = mat.shape[0]
            if n > 10000:
                # 大库用 HNSW，参数与 pgvector 侧保持一致，便于横向对比
                self.index = faiss.IndexHNSWFlat(self.dim, 16)
                self.index.hnsw.efConstruction = 64
                self.index.hnsw.efSearch = 64
            else:
                self.index = faiss.IndexFlatIP(self.dim)
            self.index.add(mat)
            self.backend = "faiss"
        else:
            self.backend = "numpy"
            log("未安装 faiss，向量检索降级为 numpy 暴力内积（结果等价，仅性能差）。", "warn")

    # ---------------- 检索 ----------------
    def search(self, query_vec: list[float], top_k: int) -> list[tuple[int, float]]:
        """返回 [(chunk 下标, 余弦相似度)]，按分数降序。"""
        if not self.chunks:
            return []
        q = np.asarray([query_vec], dtype="float32")
        n = np.linalg.norm(q)
        if n > 0:
            q = q / n
        k = min(top_k, len(self.chunks))

        if self.backend == "faiss" and self.index is not None:
            scores, idxs = self.index.search(q, k)
            return [(int(i), float(s)) for i, s in zip(idxs[0], scores[0]) if i >= 0]

        sims = (self._matrix @ q[0])  # type: ignore[operator]
        order = np.argsort(-sims)[:k]
        return [(int(i), float(sims[i])) for i in order]

    # ---------------- 持久化 ----------------
    def save(self, index_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        """
        写索引三件套，返回**补齐后**的 manifest。

        返回值必须带上 backend / dim / chunk_count：调用方（build_index、运维脚本）
        拿到的对象要与落盘文件一致，否则会出现"内存里说 20 条、文件里说 20 条但少个字段"
        这种查起来极烦的错位。
        """
        index_dir.mkdir(parents=True, exist_ok=True)
        with open(index_dir / self.CHUNKS_FILE, "w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c.to_json(), ensure_ascii=False) + "\n")
        # 向量与 chunks.jsonl 同序落盘：重建多文档索引时按切片复用，避免重复调用 embedding
        if self._matrix is not None:
            np.save(index_dir / self.VECTORS_FILE, self._matrix)
        if self.backend == "faiss" and self.index is not None:
            faiss.write_index(self.index, str(index_dir / self.INDEX_FILE))
        manifest = {**manifest, "backend": self.backend, "dim": self.dim,
                    "chunk_count": len(self.chunks)}
        (index_dir / self.MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    @classmethod
    def load(cls, index_dir: Path) -> tuple["VectorStore", dict[str, Any]]:
        mpath = index_dir / cls.MANIFEST_FILE
        if not mpath.exists():
            raise FileNotFoundError(
                f"索引不存在：{index_dir}\n请先执行：payroll-check build"
            )
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        chunks = [
            Chunk.from_json(json.loads(line))
            for line in (index_dir / cls.CHUNKS_FILE).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        store = cls(chunks, int(manifest.get("dim", 0)))

        backend = manifest.get("backend", "faiss")
        if backend == "faiss":
            store.index = faiss.read_index(str(index_dir / cls.INDEX_FILE))
            store.backend = "faiss"
        else:
            store.backend = "numpy"

        # 向量副本：老索引可能没有这个文件（升级前写的），读不到就算了，
        # 只是重建时无法复用缓存，功能不受影响。
        vpath = index_dir / cls.VECTORS_FILE
        if np is not None and vpath.exists():
            try:
                store._matrix = np.load(vpath)
            except (ValueError, OSError):
                store._matrix = None
        return store, manifest
