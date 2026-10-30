"""LLM 适配层的对外出口：工厂 + 协议 + 两种实现。
"""

from ..config import Settings
from .base import ChatModel, LLMResult
from .mock import MockChatModel
from .openai_compat import OpenAICompatChatModel

__all__ = [
    "ChatModel",
    "LLMResult",
    "MockChatModel",
    "OpenAICompatChatModel",
    "build_chat_model",
]


def build_chat_model(s: Settings) -> ChatModel:
    """工厂：配置 → 具体实现。换供应商只改环境变量，业务代码零改动。"""
    if s.llm_provider == "mock":
        return MockChatModel()
    return OpenAICompatChatModel(
        base_url=s.llm_base_url, api_key=s.llm_api_key, model=s.llm_model,
        timeout=s.llm_timeout, retries=s.llm_max_retries, provider=s.llm_provider,
    )
