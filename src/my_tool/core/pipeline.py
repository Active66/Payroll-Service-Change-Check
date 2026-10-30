"""索引流水线：解析 → 切片 → 向量化 → 入库，以及加载与配置漂移体检。
"""

import time
from datetime import datetime
from typing import Any

from ..utils.console import log, section
from ..utils.env import HAS_JIEBA
from ..utils.text import sha256_text, tokenize
from .chunker import chunk_blocks
from .config import Settings
from .parser import parse_markdown
from .prompt import PROMPT_VERSION
from .retrieval.bm25 import BM25Index
from .retrieval.embedding import build_embedding_model
from .retrieval.vector_store import VectorStore


# =============================================================================
# 【模块 13 · 索引流水线】目标文件：src/my_tool/core/pipeline.py
# ★核心模块（阶段 2 复用）：解析→切片→向量→入库 四步编排，唯一变化是最后一步
#   从"写 FAISS"变成"写 pgvector + 发 DocumentIndexed 事件"。
#
#   manifest 是给运维用的，不是装饰：
#     · embedding_model / dim 变了 → 旧向量与新查询不在同一空间，必须重建；
#     · 文档 checksum 变了 → 说明制度被改过，需要走版本流程而不是原地覆盖。
#     这两条是架构文档 3.5「制度不能覆盖更新」在索引层的落地。
# =============================================================================


def build_index(s: Settings) -> tuple[VectorStore, dict[str, Any]]:
    """完整重建索引。生产环境应改为异步任务 + 进度上报，原型里同步执行。"""
    section("① 解析制度文件")
    if not s.doc_path.exists():
        raise FileNotFoundError(f"找不到制度文件：{s.doc_path}")
    raw = s.doc_path.read_text(encoding="utf-8")
    checksum = sha256_text(raw)
    meta, blocks = parse_markdown(raw)
    log(f"文件：{s.doc_path.name}", "ok")
    log(f"doc_id={meta.get('doc_id')}  version={meta.get('version')}  "
        f"生效 {meta.get('effective_from')} ~ {meta.get('effective_to') or '长期'}")
    log(f"算得 {len(blocks)} 个结构块（标题/正文/表格），checksum={checksum[:12]}…")

    section("② 切片（500 字 / 条款为装箱单元 / 表格不切 / 句末对齐 / 80 字重叠）")
    # 把物理文件名塞进 meta：切片要带上它，前端引用卡片才能显示"文件名 + 片段"
    meta = {**meta, "source_file": s.doc_path.name}
    chunks = chunk_blocks(
        blocks,
        chunk_size=s.chunk_size,
        overlap=s.chunk_overlap,
        min_size=s.min_chunk_size,
        meta=meta,
        mode=s.chunk_mode,
    )
    if not chunks:
        raise RuntimeError("切片结果为空，请检查文档解析逻辑")
    lens = [c.char_len for c in chunks]
    log(f"共 {len(chunks)} 个切片，长度 min={min(lens)} / avg={sum(lens)//len(lens)} / max={max(lens)}"
        f"（目标 {s.chunk_size} 字，模式 {s.chunk_mode}）")
    for c in chunks[:3]:
        log(f"  {c.chunk_id}  [{c.clause_range or c.heading_path}]  {c.char_len} 字  "
            f"{c.content[:30].replace(chr(10), ' ')}…")

    section("③ 向量化")
    embedder = build_embedding_model(s)
    log(f"向量模型：{embedder.name}（dim={s.embedding_dim}，provider={s.embedding_provider}）")
    t0 = time.perf_counter()
    vectors = embedder.embed([c.content for c in chunks])
    log(f"完成 {len(vectors)} 条向量，耗时 {time.perf_counter() - t0:.2f}s", "ok")

    section("④ 写入向量库（FAISS）")
    store = VectorStore(chunks, embedder.dim)
    store.build(vectors)
    manifest = {
        "doc_path": str(s.doc_path),
        "doc_id": meta.get("doc_id", ""),
        "doc_title": meta.get("title", ""),
        "doc_version": meta.get("version", "1"),
        "doc_checksum": checksum,
        "source_file": s.doc_path.name,
        "effective_from": meta.get("effective_from", ""),
        "effective_to": meta.get("effective_to") or None,
        "chunk_size": s.chunk_size,
        "chunk_overlap": s.chunk_overlap,
        "chunk_mode": s.chunk_mode,
        "embedding_provider": s.embedding_provider,
        "embedding_model": embedder.name,
        "prompt_version": PROMPT_VERSION,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "tokenizer": "jieba.cut_for_search" if HAS_JIEBA else "char-ngram",
    }
    manifest = store.save(s.index_dir, manifest)   # save 会补齐 backend/dim/chunk_count
    log(f"索引已写入：{s.index_dir}", "ok")
    log(f"后端：{store.backend}（{len(chunks)} 条向量）")
    return store, manifest


def load_index(s: Settings) -> tuple[VectorStore, dict[str, Any]]:
    """加载索引，并做一次"配置漂移"体检 —— 这是上线后最容易踩的坑。"""
    store, manifest = VectorStore.load(s.index_dir)
    embedder = build_embedding_model(s)
    if manifest.get("embedding_model") != embedder.name:
        log(f"索引用的向量模型是 {manifest.get('embedding_model')}，"
            f"当前配置是 {embedder.name}，两者不在同一向量空间，请先 build。", "err")
        raise RuntimeError("向量模型与索引不一致")
    if int(manifest.get("chunk_size", 0)) != s.chunk_size:
        log(f"切片参数不一致（索引 {manifest.get('chunk_size')}，配置 {s.chunk_size}），建议重建。", "warn")
    return store, manifest


def build_bm25(store: VectorStore) -> BM25Index:
    """BM25 倒排表在内存里现建（1000 条切片以内 < 10ms）。产出即用，无需持久化。"""
    return BM25Index([tokenize(c.content) for c in store.chunks])
