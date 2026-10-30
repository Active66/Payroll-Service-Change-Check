"""pytest 公共夹具。

原则：**测试不依赖 data/index 下预构建的索引**。
索引是派生物，CI 里不一定存在；如果测试挂在"请先 build"上，那就成了脆测试。
这里全部在内存里现建，单个用例耗时在百毫秒级。
"""

from __future__ import annotations

import os

# 测试隔离：强制 LLM 走 mock，绝不让测试误用本机 .env 里的真模型 key。
# 两层意义：① 确定性 —— 测试不该依赖开发者机器上的 .env；② 安全 ——
# 一旦误读真 key，测试每次运行都会真的向外发请求、花真金白银。
# 必须在任何 Settings.from_env() 调用之前设置，因为 _load_dotenv 只补不覆盖，
# 这里先占住 LLM_PROVIDER，.env 里的 deepseek 就不会生效。
os.environ["LLM_PROVIDER"] = "mock"
os.environ.pop("LLM_API_KEY", None)
os.environ.pop("LLM_BASE_URL", None)

import pytest

from my_tool.core.chunker import chunk_blocks
from my_tool.core.config import DEFAULT_DOC, Settings
from my_tool.core.llm.mock import MockChatModel
from my_tool.core.orchestrator import Orchestrator
from my_tool.core.parser import parse_markdown
from my_tool.core.retrieval.bm25 import BM25Index
from my_tool.core.retrieval.embedding import HashEmbedding
from my_tool.core.retrieval.vector_store import VectorStore
from my_tool.utils.text import tokenize

DIM = 512


@pytest.fixture(scope="session")
def policy_text() -> str:
    """真实制度原文。用真文档而不是造样例，否则测不出条款边界这类真问题。"""
    return DEFAULT_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def parsed(policy_text: str):
    """(front_matter, blocks)"""
    return parse_markdown(policy_text)


@pytest.fixture(scope="session")
def blocks(parsed):
    return parsed[1]


@pytest.fixture(scope="session")
def meta(parsed):
    return parsed[0]


@pytest.fixture(scope="session")
def chunks(blocks, meta):
    """默认参数（500 字 / 80 重叠 / 60 最小）下的切片结果。"""
    return chunk_blocks(blocks, chunk_size=500, overlap=80, min_size=60, meta=meta)


@pytest.fixture(scope="session")
def store(chunks) -> VectorStore:
    """用离线哈希向量建一个内存向量库（不落盘）。"""
    s = VectorStore(chunks, DIM)
    s.build(HashEmbedding(dim=DIM).embed([c.content for c in chunks]))
    return s


@pytest.fixture(scope="session")
def bm25(chunks) -> BM25Index:
    return BM25Index([tokenize(c.content) for c in chunks])


@pytest.fixture(scope="session")
def settings() -> Settings:
    """走 config/default.yaml + 环境变量，保证测的是真实配置链路。"""
    return Settings.from_env()


@pytest.fixture(scope="session")
def orchestrator(store, bm25, settings) -> Orchestrator:
    """全链路编排器，生成模型固定用 mock（确定性、不联网）。"""
    return Orchestrator(store, bm25, HashEmbedding(dim=DIM), MockChatModel(), settings)
