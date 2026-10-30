"""生成模型协议。业务代码只认 chat(messages) -> LLMResult，不认厂商。
"""

import abc
from dataclasses import dataclass, field
from typing import Any, Iterator


# =============================================================================
# 【模块 11 · LLM 适配器】目标文件：src/my_tool/core/llm/base.py
# ★核心模块（阶段 2 复用）：这是系统**唯一允许出网调用生成模型的地方**。
#   接口只有一个方法 chat(messages) -> LLMResult，屏蔽掉所有供应商差异。
#
#   两个实现：
#     MockChatModel           —— 抽取式假模型，零依赖、零成本、确定性，
#                                 用来验证"检索+引用+护栏"是否打通，不评估生成质量；
#     OpenAICompatChatModel   —— 真实模型，一家协议接所有厂商（含 vLLM / Ollama 本地部署）。
#
#   阶段 2 已落地：chat_stream() 流式接口（SSE 打字机效果）。它同样是**可选实现**，
#   基类给了"退化实现"，所以新增供应商时只要实现 chat()，流式自动可用。
# =============================================================================

@dataclass
class LLMResult:
    """统一返回结构，token 与耗时是成本核算的原始数据，必须回传。"""

    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    provider: str = ""


@dataclass
class StreamChunk:
    """
    流式输出的最小单元。**一帧只表达一件事**，避免调用方去猜字段：

      · StreamChunk(text="冻结期内")        → 增量文本，立刻转发给前端
      · StreamChunk(result=LLMResult(...))  → 收尾帧，带全文、token 与耗时

    为什么不用"yield 字符串 + 用 StopIteration.value 传结果"：那样调用方
    必须手写 while/next 循环才能拿到返回值，可读性差且容易漏掉收尾逻辑。
    """

    text: str = ""
    result: LLMResult | None = None


@dataclass
class ToolCall:
    """
    模型请求调用一个工具。

    `arguments` 是**已解析的 dict**，不是原始 JSON 字符串 —— 解析放在适配器里，
    因为不同厂商给的格式不同（有的给字符串，有的直接给对象），
    而 Agent 循环不该关心这个差异。解析失败时给空 dict + `parse_error`，
    让循环把"参数坏了"当成一次失败的工具调用，而不是崩溃。
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    parse_error: str = ""


@dataclass
class ToolCallResponse:
    """
    一次带工具的生成结果。

    两种形态互斥但允许同时出现（有的模型会"边说边调"）：
      · `tool_calls` 非空 → 循环继续，执行工具
      · `tool_calls` 为空且有 `text` → 循环结束，这就是最终答案
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    provider: str = ""
    finish_reason: str = ""

    @property
    def has_tools(self) -> bool:
        return bool(self.tool_calls)


class ChatModel(abc.ABC):
    """生成模型协议。阶段 2 用 typing.Protocol 重写，保持同一签名。"""

    provider: str = "base"
    model: str = "base"

    @abc.abstractmethod
    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2,
             max_tokens: int = 900) -> LLMResult:
        ...

    def chat_stream(self, messages: list[dict[str, str]], *, temperature: float = 0.2,
                    max_tokens: int = 900) -> Iterator[StreamChunk]:
        """
        流式生成。**基类给的是退化实现**：一次性生成完，再把全文当一帧吐出去。

        这样设计的好处是"流式是可选能力"：新增一家供应商只实现 chat() 就能跑，
        接口不会因为不支持 stream 而报错——前端看到的仍是打字机效果（只是服务端
        没逐字发），功能可用性优先。真流式由 OpenAICompatChatModel 覆盖。
        """
        res = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        yield StreamChunk(text=res.text)
        yield StreamChunk(result=res)

    # ------------------------------------------------------------------ #
    # 工具调用（阶段 5）：同样是**可选能力**
    # ------------------------------------------------------------------ #
    def chat_with_tools(self, messages: list[dict[str, Any]], *,
                        tools: list[dict[str, Any]] | None = None,
                        temperature: float = 0.2,
                        max_tokens: int = 900) -> ToolCallResponse:
        """
        带工具的生成。**基类给的是退化实现**：忽略 tools，当普通对话处理。

        为什么退化实现要这么写而不是抛 NotImplementedError：
        供应商支持与否是部署环境的事，不该让上层在运行时才发现"这家不支持"。
        退化后 Agent 循环会看到"这一轮没有工具调用"，于是把模型的话当最终答案
        —— 用户拿到的是一次普通问答，而不是一个报错。功能降级优于功能不可用。

        真实现由 OpenAICompatChatModel 覆盖（也由 MockToolCallingModel 覆盖，
        后者按剧本返回工具调用，用于离线测试闸门）。
        """
        res = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return ToolCallResponse(
            text=res.text, model=res.model, tokens_in=res.tokens_in,
            tokens_out=res.tokens_out, latency_ms=res.latency_ms,
            provider=res.provider, finish_reason="stop")
