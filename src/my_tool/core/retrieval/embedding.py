"""向量模型抽象与两种实现。上层只依赖 EmbeddingModel 接口。
"""

import abc
import math
import zlib
from typing import Sequence

from ...utils.console import log
from ...utils.text import tokenize
from ..config import Settings
from ..llm.http import http_post_json


# =============================================================================
# 【模块 06 · Embedding 抽象与实现】目标文件：src/my_tool/core/retrieval/embedding.py
# ★核心模块（阶段 2 复用）：抽象层决定"换模型不动业务"。
#
#   两种实现：
#     HashEmbedding          —— 离线确定性哈希向量，无网可跑，用于验证链路骨架；
#                                ★不要用于生产★，它只有词面相似度，没有语义。
#     OpenAICompatEmbedding  —— 真实向量模型（bge-m3 / text-embedding-3 / Qwen-embedding），
#                               同一协议，换厂商只改 base_url + model + dim。
# =============================================================================

class EmbeddingModel(abc.ABC):
    """
    向量模型协议。阶段 2 拆包后这就是 app/llm/base.py 里的 Protocol。
    签名固定为 list[str] -> list[list[float]]，批量优先（真实模型一次请求多条省时省钱）。
    """

    name: str = "base"
    dim: int = 0

    @abc.abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class HashEmbedding(EmbeddingModel):
    """
    离线哈希向量（hashing trick + 对数词频 + L2 归一化）。

    用途只有一个：**没有 API key 时也能把"切片→向量→FAISS→检索→引用"整条链路跑通**，
    让阶段 1 的验收不被采购流程卡住。

    为什么用 crc32 而不是内置 hash()：Python 对 str 的 hash 每个进程加随机盐，
    重启后向量全变，索引直接失效。这是自己实现哈希向量时最容易踩的坑。
    """

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.name = f"hash-tf-{dim}"

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text)
        if not tokens:
            return vec
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        for tok, cnt in tf.items():
            # 对数词频压缩长文本优势，避免一条长条款吞掉整个语义
            w = 1.0 + math.log(cnt)
            h = zlib.crc32(tok.encode("utf-8"))          # 确定性哈希
            vec[h % self.dim] += w
            # 位置无关的二次散列，降低碰撞造成的方向偏差
            vec[(h >> 11) % self.dim] += w * 0.5
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class OpenAICompatEmbedding(EmbeddingModel):
    """
    真实向量模型，走 OpenAI /embeddings 兼容协议。
    bge-m3、text-embedding-3-*、text-embedding-v3 都能直接接。
    """

    def __init__(self, base_url: str, api_key: str, model: str, dim: int,
                 batch: int = 16, timeout: float = 60.0, retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = model
        self.dim = dim
        self.batch = batch
        self.timeout = timeout
        self.retries = retries

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            part = list(texts[i : i + self.batch])
            payload = {"model": self.name, "input": part}
            data = http_post_json(
                f"{self.base_url}/embeddings", payload, self.api_key, self.timeout, self.retries
            )
            # 按 index 排序，防止服务端乱序返回
            items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            out.extend([it["embedding"] for it in items])
            log(f"embedding 进度 {min(i + self.batch, len(texts))}/{len(texts)}", "info")
        if out and len(out[0]) != self.dim:
            log(f"向量维度与配置不符：配置 {self.dim}，实际 {len(out[0])}，已按实际值修正。", "warn")
            self.dim = len(out[0])
        return out


def build_embedding_model(s: Settings) -> EmbeddingModel:
    """工厂：配置 → 实例。上层永远只依赖 EmbeddingModel 接口。"""
    if s.embedding_provider == "hash":
        return HashEmbedding(dim=s.embedding_dim)
    return OpenAICompatEmbedding(
        base_url=s.embedding_base_url,
        api_key=s.embedding_api_key,
        model=s.embedding_model,
        dim=s.embedding_dim,
        batch=s.embedding_batch,
        timeout=s.llm_timeout,
        retries=s.llm_max_retries,
    )
