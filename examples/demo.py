#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小可运行示例：从零建索引 → 提问 → 看引用 → 看护栏结论。

    python examples/demo.py

特点：
  · 完全离线（hash 占位向量 + mock 抽取模型），不需要任何 API Key；
  · 索引建在临时目录，跑完即删，**不会覆盖 data/index**；
  · 输出里刻意保留"通道来源"和"护栏结论"，这两项才是排查问题的入口。
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

# 未安装成包时也能直接跑：把 src 加进 import 路径
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from my_tool.core.chunker import chunk_blocks                    # noqa: E402
from my_tool.core.config import Settings                          # noqa: E402
from my_tool.core.llm.mock import MockChatModel                    # noqa: E402
from my_tool.core.orchestrator import Orchestrator, RAGAnswer      # noqa: E402
from my_tool.core.parser import parse_markdown                     # noqa: E402
from my_tool.core.pipeline import build_bm25                       # noqa: E402
from my_tool.core.retrieval.embedding import HashEmbedding         # noqa: E402
from my_tool.core.retrieval.vector_store import VectorStore        # noqa: E402
from my_tool.utils.console import _init_console, section           # noqa: E402

QUESTIONS = [
    "2026 年上海社保缴费基数上限是多少？",
    "冻结期内可以改银行账户吗？",
    "员工报销流程怎么走？",           # 故意问一个制度里没有的，用来看拒答闸门
]


def build_in_memory(s: Settings) -> Orchestrator:
    """内存里走一遍 解析 → 切片 → 向量化，不落盘。"""
    meta, blocks = parse_markdown(s.doc_path.read_text(encoding="utf-8"))
    chunks = chunk_blocks(
        blocks, chunk_size=s.chunk_size, overlap=s.chunk_overlap,
        min_size=s.min_chunk_size, meta=meta, mode=s.chunk_mode,
    )
    embedder = HashEmbedding(dim=s.embedding_dim)
    store = VectorStore(chunks, s.embedding_dim)
    store.build(embedder.embed([c.content for c in chunks]))
    return Orchestrator(store, build_bm25(store), embedder, MockChatModel(), s)


def show(res: RAGAnswer) -> None:
    print(f"\n问：{res.question}")
    print(f"答：{res.answer}\n")
    if res.citations:
        print("引用：")
        for c in res.citations:
            hit = next((h for h in res.hits if h.chunk.chunk_id == c.chunk_id), None)
            print(f"  [{c.index}] {c.chunk_id}  {c.clause_no or '-'}  "
                  f"{hit.channels if hit else '-'}")
    g = res.guard
    status = f"护栏={g.status}" if g else "护栏=跳过（未进模型）"
    print(f"{status}｜状态={res.status}｜耗时 {res.latency_ms} ms")


def main() -> int:
    _init_console()
    s = Settings.from_env()

    with tempfile.TemporaryDirectory() as tmp:
        # 演示用临时索引目录；真实使用请去掉这一行，让索引落到 data/index
        s = replace(s, index_dir=Path(tmp))

        section("① 内存建索引")
        orch = build_in_memory(s)
        n = len(orch.retriever.store.chunks)
        print(f"切片 {n} 条｜向量维度 {s.embedding_dim}｜检索 top{s.top_k}")

        section("② 提问")
        for q in QUESTIONS:
            show(orch.answer(q))

    section("③ 下一步")
    print("真实模型：  set LLM_PROVIDER=deepseek & set LLM_API_KEY=sk-xxx")
    print("持久索引：  python -m my_tool build")
    print("检索评测：  python scripts/eval_retrieval.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
