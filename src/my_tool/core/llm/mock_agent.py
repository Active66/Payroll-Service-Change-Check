"""脚本化假 Agent：让"模型决策"可重复，从而让循环与闸门可测试。

    **为什么必须有它，不只是为了省钱**

      真模型的输出不确定。用真模型测"跳步会不会被拦住"，得到的结果是
      "大部分时候会被拦" —— 而这不足以证明闸门有效，因为失败的那几次
      到底是模型没跳、还是闸门漏了，分不清。

      脚本化假 Agent 按预设序列返回工具调用，于是：
        · 可以精确构造"跳过校验直接建单"这种序列，断言它被拒；
        · 可以构造"无限重试"的序列，断言预算能刹住；
        · 可以构造"模型乱编参数"的序列，断言坏参数不导致 500。

      它测的是**循环与闸门的逻辑**，不测模型智商。这两件事必须分开，
      否则调提示词时会把"循环的 bug"误诊成"模型不听话"。

    剧本写法（`steps`）：
        ("tool", "lookup_param", {"keys": ["si_base_cap"]})   调用工具
        ("text", "答复用户的一句话")                            输出最终答案
        ("raw", "工具名", "{坏 JSON")                          构造一次参数坏掉的调用
        ("repeat", ("tool", ...))                              无限重复（测预算）

    剧本用完之后：重复最后一步（`repeat_last=True` 时）或直接收尾。
    默认收尾 —— 因为"剧本演完了"最合理的结果是"模型说完了"。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterator

from .base import ChatModel, LLMResult, StreamChunk, ToolCall, ToolCallResponse

Step = tuple[Any, ...]


class MockToolCallingModel(ChatModel):
    """
    按剧本返回工具调用的假模型。

    它同时实现 `chat()`（退化到抽取式回答）与 `chat_with_tools()`（按剧本），
    这样同一个实例既能跑 Agent 循环，也能在被降级时跑普通问答。
    """

    provider = "mock"
    model = "mock-agent"

    def __init__(self, steps: list[Step] | None = None, *,
                 final_text: str = "（脚本结束，未给出最终答复）",
                 repeat_last: bool = False):
        self.steps = list(steps or [])
        self.final_text = final_text
        self.repeat_last = repeat_last
        self.cursor = 0
        # 记录每次请求的 tools，用于断言"工具集裁剪真的发生了"
        self.seen_tools: list[list[str]] = []

    # ------------------------------------------------------------------ #
    # 剧本解释
    # ------------------------------------------------------------------ #
    def _next_step(self) -> Step:
        if self.cursor < len(self.steps):
            step = self.steps[self.cursor]
            self.cursor += 1
            return step
        if self.repeat_last and self.steps:
            return self.steps[-1]
        return ("text", self.final_text)

    def chat_with_tools(self, messages: list[dict[str, Any]], *,
                        tools: list[dict[str, Any]] | None = None,
                        temperature: float = 0.2,
                        max_tokens: int = 900) -> ToolCallResponse:
        names = [((t.get("function") or {}).get("name") or "") for t in (tools or [])]
        self.seen_tools.append(names)

        step = self._next_step()
        kind = step[0] if step else "text"
        t0 = time.perf_counter()

        if kind == "tool":
            _, name, args = (list(step) + [None, {}])[:3]
            return self._resp(tool_calls=[ToolCall(
                id=f"call_{self.cursor}", name=str(name),
                arguments=dict(args or {}))], t0=t0)

        if kind == "raw":
            # 参数是坏 JSON：模拟模型生成了不合法的 arguments
            _, name, raw = (list(step) + [None, ""])[:3]
            try:
                json.loads(str(raw))
                err = ""
            except json.JSONDecodeError as ex:
                err = f"参数不是合法 JSON：{ex}"
            return self._resp(tool_calls=[ToolCall(
                id=f"call_{self.cursor}", name=str(name),
                arguments={}, parse_error=err)], t0=t0)

        if kind == "repeat":
            # 无限重复同一个调用，用来测预算刹车。
            # 关键：把剧本**改写成只剩这一步**并打开 repeat_last，
            # 否则第二次取剧本就落到"剧本演完了"分支，刹车根本测不到。
            inner = step[1]
            self.steps = [inner]
            self.cursor = 0
            self.repeat_last = True
            self.cursor = 1
            return self.chat_with_tools_forced(inner, t0)

        # 默认当文本
        text = str(step[1]) if len(step) > 1 else self.final_text
        return self._resp(text=text, t0=t0)

    def chat_with_tools_forced(self, step: Step, t0: float | None = None) -> ToolCallResponse:
        _, name, args = (list(step) + [None, {}])[:3]
        return self._resp(tool_calls=[ToolCall(
            id=f"call_{self.cursor}_r", name=str(name),
            arguments=dict(args or {}))], t0=t0 or time.perf_counter())

    def _resp(self, *, text: str = "", tool_calls: list[ToolCall] | None = None,
              t0: float | None = None) -> ToolCallResponse:
        return ToolCallResponse(
            text=text, tool_calls=tool_calls or [], model=self.model,
            tokens_in=len(str(t0)), tokens_out=len(text),   # 假 token，仅用于预算测试的单调性
            latency_ms=int(((time.perf_counter() - (t0 or time.perf_counter())) * 1000)),
            provider=self.provider, finish_reason="tool_calls" if tool_calls else "stop")

    # ------------------------------------------------------------------ #
    # 普通问答（降级通道用）
    # ------------------------------------------------------------------ #
    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2,
             max_tokens: int = 900) -> LLMResult:
        user = next((m.get("content", "") for m in reversed(messages)
                     if m.get("role") == "user"), "")
        return LLMResult(
            text=self.final_text or f"（离线模式）关于「{user[:30]}」的回答",
            model=self.model, tokens_in=len(user), tokens_out=10,
            latency_ms=1, provider=self.provider)

    def chat_stream(self, messages: list[dict[str, str]], *, temperature: float = 0.2,
                    max_tokens: int = 900) -> Iterator[StreamChunk]:
        res = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        yield StreamChunk(text=res.text)
        yield StreamChunk(result=res)


# ---------------------------------------------------------------------------
# 常用剧本：覆盖四条关键路径
#
# 剧本里可以用 `$last.<字段>` 引用**上一次工具返回的 data 字段**，
# 由 loop 在执行前替换。这是必需的：check_rules 返回的 validation_token
# 是动态的，写死就测不到"拿到凭证 → 建单成功"这条正路。
# 真模型不需要这个机制（它自己会填），所以替换逻辑只服务于脚本化测试。
# ---------------------------------------------------------------------------

ITEMS = [{"field": "fixed_salary", "old_value": "18500", "new_value": "19000",
          "effective_date": "2026-11-01"}]
MATERIALS = ["approval_form", "adjust_basis"]


def script_happy_path(employee_no: str = "E1001") -> list[Step]:
    """
    合法路径：查档案 → 查参数 → 校验 → 用返回的凭证建单 → 停下来请人确认。

    注意 get_employee 放在最前面：它是 `region_code` 的来源，
    没有它 lookup_param 会返回 missing（地区的参数在 310000 名下）。
    """
    return [
        ("tool", "get_employee", {"employee_no": employee_no}),
        ("tool", "lookup_param", {"keys": ["freeze_start_day", "diff_l2_max"]}),
        ("tool", "check_rules", {
            "change_type": "salary_adjust", "employee_no": employee_no,
            "items": ITEMS, "materials": MATERIALS}),
        ("tool", "create_draft", {
            "validation_token": "$last.validation_token",
            "change_type": "salary_adjust", "employee_no": employee_no,
            "items": ITEMS, "materials": MATERIALS,
            "idempotency_key": "happy-1"}),
        ("text", "已为爱丽丝建好调薪草稿（18500 → 19000，L2）。"
                 "提交需要您在界面上点确认，确认后才会进入审批流。"),
    ]


def script_skip_gate(employee_no: str = "E1001") -> list[Step]:
    """
    绕过剧本：模型跳过校验，直接建单。

    这条是最该测的 —— 模型"想省一步"是最常见的行为，
    而它必须失败，且不能留下任何单据。
    """
    return [
        ("tool", "create_draft", {
            "validation_token": "", "change_type": "salary_adjust",
            "employee_no": employee_no, "items": ITEMS,
            "idempotency_key": "skip-1"}),
        ("text", "好的，已为您建单。"),
    ]


def script_infinite_retry(employee_no: str = "E1001") -> list[Step]:
    """无限重试剧本：每次都用错误的参数建单，用来验证预算刹车。"""
    return [
        ("repeat", ("tool", "create_draft", {
            "validation_token": "", "change_type": "salary_adjust",
            "employee_no": employee_no,
            "items": [{"field": "fixed_salary", "old_value": "18500",
                       "new_value": "19000"}],
            "idempotency_key": "loop-1"})),
    ]


def script_bad_args(employee_no: str = "E1001") -> list[Step]:
    """坏参数剧本：模型生成的 arguments 不是合法 JSON。"""
    return [
        ("raw", "check_rules", "{\"change_type\": \"salary_adjust\", broken"),
        ("text", "抱歉，我需要重新组织一下参数。"),
    ]
