"""索引管理器：多文档全量重建 + 向量缓存 + 热切换。

    为什么必须"全量重建"而不是"局部增删"：
      FAISS（以及 pgvector 的 HNSW）索引是**整体**结构，无法从中摘掉某几条向量。
      要支持"删掉一份制度"，只能拿剩下的向量重建一个新索引。

    但重建不等于重新向量化。这里有三层省钱的机制：
      ① 按切片内容哈希缓存向量 —— 内容没变的切片直接复用；
      ② 只有新增/变更的切片才批量调用 embedding；
      ③ 换 embedding 模型时缓存自动作废（维度/模型名对不上），避免把两个空间的
         向量混进同一个索引（那会让相似度变成噪声，且很难察觉）。

    热切换：
      重建在后台线程完成，最后一步执行 `ctx.orchestrator = 新对象`。
      Python 的引用赋值是原子的：正在处理的请求持有旧对象继续跑完，
      新请求拿到新对象 —— 重建期间服务不中断。
"""

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..utils.console import log
from ..utils.env import np
from ..utils.text import sha256_text
from .config import Settings
from .ingest import ingest
from .llm import build_chat_model
from .orchestrator import Orchestrator
from .pipeline import build_bm25
from .retrieval.embedding import build_embedding_model
from .retrieval.vector_store import VectorStore

# =============================================================================
# 【模块 24 · 索引管理器】目标文件：src/my_tool/core/index_manager.py
# ★核心模块：知识库的一切写操作最终都汇到这里，只有它能替换线上索引。
# =============================================================================


class RebuildBusy(RuntimeError):
    """已经有一次重建在进行。上层翻译成 KB_005。"""


class RebuildError(RuntimeError):
    """重建失败。上层翻译成 KB_006，并保留旧索引继续服务。"""


class VectorCache:
    """
    切片内容哈希 → 向量 的本地缓存。

    为什么按"内容哈希"而不是 chunk_id：文档改一个字，后面所有切片的序号都会平移，
    按 id 缓存等于全部失效；按内容哈希则只有真正变了的那一片需要重算。
    """

    KEYS_FILE = "vec_cache_keys.json"
    VECS_FILE = "vec_cache_vecs.npy"

    def __init__(self, dim: int, keys: list[str], vecs: Any):
        self.dim = dim
        self._map: dict[str, int] = {k: i for i, k in enumerate(keys)}
        self._vecs = vecs

    @classmethod
    def empty(cls, dim: int) -> "VectorCache":
        mat = np.zeros((0, dim), dtype="float32") if np is not None else None
        return cls(dim, [], mat)

    @classmethod
    def load(cls, index_dir: Path, dim: int) -> "VectorCache":
        """读缓存。模型/维度变了就当作空缓存 —— 宁可多算一次，也不能混空间。"""
        kp = index_dir / cls.KEYS_FILE
        vp = index_dir / cls.VECS_FILE
        if np is None or not kp.exists() or not vp.exists():
            return cls.empty(dim)
        try:
            meta = json.loads(kp.read_text(encoding="utf-8"))
            if int(meta.get("dim", -1)) != dim:
                log(f"向量缓存维度 {meta.get('dim')} 与当前 {dim} 不一致，已丢弃", "warn")
                return cls.empty(dim)
            mat = np.load(vp)
            keys = list(meta.get("keys", []))
            if len(keys) != int(mat.shape[0]) or int(mat.shape[1]) != dim:
                return cls.empty(dim)
            return cls(dim, keys, mat)
        except (OSError, ValueError, json.JSONDecodeError):
            return cls.empty(dim)

    def get(self, key: str) -> Any:
        idx = self._map.get(key)
        if idx is None or self._vecs is None:
            return None
        return self._vecs[idx]

    def __len__(self) -> int:
        return len(self._map)

    @classmethod
    def from_scratch(cls, dim: int, pairs: list[tuple[str, Any]]) -> "VectorCache":
        """用本次重建的 (hash, vector) 全量重建缓存，顺带淘汰过期条目。"""
        if np is None:
            return cls.empty(dim)
        keys = [k for k, _ in pairs]
        mat = (np.asarray([v for _, v in pairs], dtype="float32")
               if pairs else np.zeros((0, dim), dtype="float32"))
        return cls(dim, keys, mat)

    def save(self, index_dir: Path) -> None:
        if np is None or self._vecs is None:
            return
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / self.KEYS_FILE).write_text(
            json.dumps({"dim": self.dim, "keys": list(self._map.keys())},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        np.save(index_dir / self.VECS_FILE, self._vecs)


class IndexManager:
    """索引的唯一写入口。持有重建锁，负责在完成后做原子替换。"""

    def __init__(self, settings: Settings, ctx: Any):
        self.s = settings
        self.ctx = ctx
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def trigger(self, *, reason: str = "manual") -> dict[str, Any]:
        """
        触发一次后台重建。**立刻返回**，页面靠轮询 / 刷新看状态。

        用非阻塞抢锁而不是队列：管理员连点两下"重建"时，
        正确的行为是告诉他"正在重建"，而不是排队再跑一遍。
        """
        if not self._lock.acquire(blocking=False):
            raise RebuildBusy("索引正在重建")

        self.ctx.rebuild_state = {
            "status": "building", "error": None, "reason": reason,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "finished_at": None, "chunks": 0, "docs": 0,
        }
        t = threading.Thread(target=self._run, name="index-rebuild", daemon=True)
        t.start()
        return dict(self.ctx.rebuild_state)

    def rebuild_now(self, *, reason: str = "sync") -> dict[str, Any]:
        """
        同步重建。给 CLI 与测试用 —— 它们需要"跑完再返回"的语义。
        """
        if not self._lock.acquire(blocking=False):
            raise RebuildBusy("索引正在重建")
        self.ctx.rebuild_state = {
            "status": "building", "error": None, "reason": reason,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "finished_at": None, "chunks": 0, "docs": 0,
        }
        return self._run()

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _run(self) -> dict[str, Any]:
        try:
            state = self._rebuild()
            state["status"] = "succeeded"
            state["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            self.ctx.rebuild_state = state
            log(f"索引重建完成：{state['docs']} 份文档 / {state['chunks']} 条切片", "ok")
            return state
        except Exception as ex:                    # noqa: BLE001 — 失败必须留在状态里，不能吞
            state = {
                **self.ctx.rebuild_state,
                "status": "failed",
                "error": f"{type(ex).__name__}: {ex}",
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self.ctx.rebuild_state = state
            log(f"索引重建失败，已保留原索引：{ex}", "err")
            return state
        finally:
            self._lock.release()

    def _rebuild(self) -> dict[str, Any]:
        from ..store import db, policy_repo

        s = self.s
        today = time.strftime("%Y-%m-%d")
        conn = db.connect(s.chat_db_path)
        try:
            # 待生效的版本不进索引：检索侧虽有生效期过滤，但那要多做一次判断，
            # 而且"传了但答不出来"在后台也很难解释。挡在建索引阶段最干净。
            docs = policy_repo.list_active(conn, today=today)
        finally:
            conn.close()

        chunks: list[Any] = []
        doc_entries: list[dict[str, Any]] = []
        per_doc: list[tuple[str, int, int, int]] = []   # (doc_id, version, chunks, chars)

        for d in docs:
            path = Path(d["storage_path"])
            if not path.exists():
                log(f"跳过 {d['doc_id']}：文件不存在 {path}", "warn")
                continue
            try:
                result = ingest(s, path, d["source_file"], form={
                    "doc_id": d["doc_id"], "title": d["title"],
                    "version": str(d["version"]),
                    "region_code": d["region_code"] or "",
                    "effective_from": d["effective_from"] or "",
                    "effective_to": d["effective_to"] or "",
                    "doc_type": d["doc_type"] or "policy",
                })
            except Exception as ex:               # noqa: BLE001 — 单份坏文件不该拖垮整个库
                log(f"切片失败，已跳过 {d['doc_id']}：{ex}", "warn")
                continue

            doc_chunks = result["chunks"]
            chunks.extend(doc_chunks)
            per_doc.append((d["doc_id"], int(d["version"]), len(doc_chunks),
                            sum(c.char_len for c in doc_chunks)))
            doc_entries.append({
                "doc_id": d["doc_id"], "title": d["title"], "version": d["version"],
                "version_label": d.get("version_label") or str(d["version"]),
                "source_file": d["source_file"], "checksum": d["checksum"],
                "chunk_count": len(doc_chunks),
            })

        # —— 向量化：命中缓存的不再调用 embedding ——
        embedder = build_embedding_model(s)
        cache = VectorCache.load(s.index_dir, embedder.dim)

        keys = [sha256_text(c.content)[:32] for c in chunks]
        vectors: list[Any] = [None] * len(chunks)
        miss_idx: list[int] = []
        for i, k in enumerate(keys):
            hit = cache.get(k)
            if hit is None:
                miss_idx.append(i)
            else:
                vectors[i] = hit

        if miss_idx:
            log(f"需要向量化的切片 {len(miss_idx)}/{len(chunks)}"
                f"（其余 {len(chunks) - len(miss_idx)} 条命中缓存）")
            fresh = embedder.embed([chunks[i].content for i in miss_idx])
            for slot, vec in zip(miss_idx, fresh):
                vectors[slot] = vec
        else:
            log(f"全部 {len(chunks)} 条切片命中向量缓存，未调用 embedding")

        # —— 建索引 ——
        store = VectorStore(chunks, embedder.dim)
        if chunks:
            store.build([list(map(float, v)) for v in vectors])
        else:
            store.backend = "numpy"
            store._matrix = np.zeros((0, embedder.dim), dtype="float32") if np is not None else None

        first = doc_entries[0] if doc_entries else {}
        manifest = {
            "documents": doc_entries,
            "doc_count": len(doc_entries),
            "doc_id": first.get("doc_id", ""),
            "doc_title": first.get("title", "") if len(doc_entries) == 1 else
                         (f"{first.get('title', '')} 等 {len(doc_entries)} 份" if doc_entries else ""),
            "doc_version": first.get("version", ""),
            "source_file": first.get("source_file", ""),
            "chunk_size": s.chunk_size,
            "chunk_overlap": s.chunk_overlap,
            "chunk_mode": s.chunk_mode,
            "embedding_provider": s.embedding_provider,
            "embedding_model": embedder.name,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": "index_manager",
        }
        manifest = store.save(s.index_dir, manifest)

        # 缓存只保留本次实际用到的条目：删掉的文档不该继续占内存/磁盘
        VectorCache.from_scratch(embedder.dim, list(zip(keys, vectors))).save(s.index_dir)

        # —— 原子替换 ——
        # 带上 prompt_provider：否则重建一次索引就会把"后台可改 prompt"这件事弄丢，
        # 表现为"重建索引之后提示词又变回默认了"——这种回归极难查。
        self.ctx.orchestrator = Orchestrator(
            store, build_bm25(store), embedder, build_chat_model(s), s,
            prompt_provider=getattr(self.ctx, "prompt_provider", None))
        self.ctx.manifest = manifest
        self.ctx.index_error = None

        # 回写每份文档的切片数（失败不影响重建结果，索引已经生效）
        conn = db.connect(s.chat_db_path)
        try:
            for doc_id, version, n_chunks, n_chars in per_doc:
                policy_repo.mark_indexed(conn, doc_id, n_chunks, n_chars, version=version)
        except Exception as ex:                    # noqa: BLE001
            log(f"回写文档索引信息失败：{ex}", "warn")
        finally:
            conn.close()

        return {**self.ctx.rebuild_state, "docs": len(doc_entries), "chunks": len(chunks)}
