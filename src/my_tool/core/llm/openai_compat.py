"""OpenAI 兼容生成模型。一家协议对接 OpenAI / DeepSeek / 通义 / Kimi / 智谱 / Ollama / vLLM。
"""

import json
import time
from itertools import chain
from typing import Any, Iterator

from ...utils.console import log
from .base import ChatModel, LLMResult, StreamChunk, ToolCall, ToolCallResponse
from .http import http_post_json, http_post_stream


def _parse_arguments(raw: Any) -> tuple[dict[str, Any], str]:
    """
    解析工具参数。

    三种厂商行为都要兼容：给 JSON 字符串（OpenAI）、给对象（个别兼容实现）、
    给空串（模型只想调用不带参数的工具）。解析失败**不抛异常** ——
    它是"模型生成了坏参数"，属于可预期的失败，应该变成一次失败的工具调用
    让循环去处理（模型可以据此重试），而不是让整个请求 500。
    """
    if isinstance(raw, dict):
        return raw, ""
    if raw in (None, ""):
        return {}, ""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as ex:
        return {}, f"参数不是合法 JSON：{ex}"
    if not isinstance(parsed, dict):
        return {}, "参数必须是 JSON 对象"
    return parsed, ""


def _prime(it: Iterator[str]) -> Iterator[str]:
    """
    先取出第一行再交还给调用方。

    目的：`http_post_stream` 是生成器，**连接与 4xx/5xx 判定都发生在第一次 next()**。
    如果直接在 for 循环里迭代，错误会在"已经开始 yield 文本之后"才抛出来，
    调用方就无法安全地重试（前面的字已经发给前端了）。先 prime 一次，
    把"连接失败"与"流中途断开"两类错误分开处理。
    """
    try:
        first = next(it)
    except StopIteration:
        return iter(())
    return chain([first], it)


class OpenAICompatChatModel(ChatModel):
    """
    真实生成模型。POST {base_url}/chat/completions，字段与 OpenAI 完全一致。
    已验证可对接：OpenAI / DeepSeek / 通义千问(compatible-mode) / Kimi / 智谱 / Ollama / vLLM。
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0,
                 retries: int = 3, provider: str = "openai_compat"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.provider = provider

    # ------------------------------------------------------------------ #
    # 非流式：CLI、评测、单元测试都走这条，行为与阶段 1 完全一致
    # ------------------------------------------------------------------ #
    def chat(self, messages, *, temperature: float = 0.2, max_tokens: int = 900) -> LLMResult:
        t0 = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = http_post_json(
            f"{self.base_url}/chat/completions", payload, self.api_key, self.timeout, self.retries
        )
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as ex:
            raise RuntimeError(f"LLM 返回结构异常：{str(data)[:300]}") from ex

        usage = data.get("usage") or {}
        return LLMResult(
            text=text.strip(),
            model=data.get("model", self.model),
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            provider=self.provider,
        )

    # ------------------------------------------------------------------ #
    # 工具调用（阶段 5）
    # ------------------------------------------------------------------ #
    def chat_with_tools(self, messages: list[dict[str, Any]], *,
                        tools: list[dict[str, Any]] | None = None,
                        temperature: float = 0.2,
                        max_tokens: int = 900) -> ToolCallResponse:
        """
        带 tools 的生成。

        与 `chat()` 的差别只有三个字段：请求多 `tools` / `tool_choice`，
        响应多 `tool_calls`，且 `content` 可能为空（模型只调工具、不说话）。

        刻意**不做流式**：工具调用阶段模型输出的是结构化 JSON，逐字流式对用户
        没有意义（前端只会在工具执行完之后看到文字）。最终答案那一步如果要流式，
        由循环在"决定不再调工具"之后单独走 `chat_stream()`。
        """
        t0 = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            # auto 而不是 required：有的问题根本不需要工具（"制度是什么时候发布的"），
            # 强制调用会让模型为了满足约束而硬调一个不相干的工具
            payload["tool_choice"] = "auto"

        data = http_post_json(
            f"{self.base_url}/chat/completions", payload, self.api_key,
            self.timeout, self.retries)

        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError) as ex:
            raise RuntimeError(f"LLM 返回结构异常：{str(data)[:300]}") from ex

        calls: list[ToolCall] = []
        for i, raw in enumerate(message.get("tool_calls") or []):
            fn = raw.get("function") or {}
            args, err = _parse_arguments(fn.get("arguments"))
            calls.append(ToolCall(
                id=str(raw.get("id") or f"call_{i}"),
                name=str(fn.get("name") or ""),
                arguments=args,
                parse_error=err,
            ))
            if err:
                log(f"工具参数解析失败（{fn.get('name')}）：{err}", "warn")

        usage = data.get("usage") or {}
        return ToolCallResponse(
            text=(message.get("content") or "").strip(),
            tool_calls=calls,
            model=data.get("model", self.model),
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            provider=self.provider,
            finish_reason=str(choice.get("finish_reason") or ""),
        )

    # ------------------------------------------------------------------ #
    # 流式：SSE 逐行解析。只有这一处需要理解厂商的流式协议
    # ------------------------------------------------------------------ #
    def chat_stream(self, messages, *, temperature: float = 0.2,
                    max_tokens: int = 900) -> Iterator[StreamChunk]:
        """
        逐字返回。协议原文是 `data: {json}\\n\\n` 的重复，末尾一个 `data: [DONE]`。

        两个兼容性处理（不写就会在部分厂商上炸）：

        ① `stream_options.include_usage` —— OpenAI / DeepSeek 支持，用它才能在最后
           一帧拿到真实 token 数；但个别兼容实现会因"未知字段"直接 400。
           所以带它试一次，400 就去掉重试（此时尚未吐出任何文本，重试安全）。
        ② 有的实现会在流里插入 `: keep-alive` 注释行或空行，必须跳过而不是报错。
        """
        t0 = time.perf_counter()
        url = f"{self.base_url}/chat/completions"

        include_usage = True
        while True:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            }
            if include_usage:
                payload["stream_options"] = {"include_usage": True}

            raw = http_post_stream(url, payload, self.api_key, self.timeout)
            try:
                lines = _prime(raw)
            except RuntimeError as ex:
                # 只在"建立连接就被拒"时降级重试；此时一个字都没发出去
                if include_usage and " 400" in str(ex):
                    log("该端点不接受 stream_options，去掉 include_usage 后重试", "warn")
                    include_usage = False
                    continue
                raise RuntimeError(str(ex)) from ex
            break

        parts: list[str] = []
        usage_in = usage_out = 0
        model = self.model
        for line in lines:
            if not line or line.startswith(":"):     # 空行、心跳注释
                continue
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                # 半行/脏数据不应打断整段回答，跳过即可（下一行通常就是完整的）
                continue

            model = obj.get("model") or model
            usage = obj.get("usage") or {}
            if usage:                                # 只有最后一帧会带
                usage_in = int(usage.get("prompt_tokens") or usage_in)
                usage_out = int(usage.get("completion_tokens") or usage_out)

            choices = obj.get("choices") or []
            if not choices:
                continue
            # delta.content 为空串是正常的（首帧只有 role、末帧只有 finish_reason）
            piece = (choices[0].get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
                yield StreamChunk(text=piece)

        text = "".join(parts)
        # 厂商没给 usage 时按中文粗估（1 字 ≈ 0.75 token），只为成本核算不全为 0，
        # 不参与任何计费或断言逻辑。
        yield StreamChunk(result=LLMResult(
            text=text.strip(),
            model=model,
            tokens_in=usage_in or int(len(json.dumps(messages, ensure_ascii=False)) * 0.4),
            tokens_out=usage_out or int(len(text) * 0.75),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            provider=self.provider,
        ))
