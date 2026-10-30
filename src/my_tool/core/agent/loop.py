"""Agent 循环：让模型自己决定调什么工具、什么时候停。

    **这个文件的全部复杂度都在"刹车"上。**

    模型能自由决策是好事，但自由决策的失败模式很难看：无限重试、反复猜参数、
    把上下文撑爆、一直不肯收尾。所以四条硬限制都在这里强制执行，
    一条也不依赖模型自觉：

        agent_max_tool_calls   单轮最多调几次工具（默认 8）
        agent_max_turns        单次对话最多几轮生成（默认 6）
        agent_token_budget     token 预算（默认 30k）
        agent_timeout_seconds  总超时（默认 60s）

    触顶的处理**不是报错，而是降级**：退回阶段 1-4 的固定 RAG 流程，
    用 `Orchestrator.answer()` 给一个带引用的答案。用户至少能拿到东西，
    而不是一个"服务器错误"。这条是设计文档第 6 节明确要求的。

    还有一个不显眼但重要的决定：**工具集按 next_allowed 逐步收窄**。
    模型看不见的工具比"被告知不要调"的工具可靠得多 —— 后者是在赌它听话。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from ...utils.console import log
from ..llm.base import ToolCallResponse
from .registry import E_BUDGET_EXCEEDED, Registry, ToolContext, ToolResult
from .system_prompt import build_system_message

# =============================================================================
# 【模块 34 · Agent 循环】目标文件：src/my_tool/core/agent/loop.py
# =============================================================================


@dataclass
class LoopEvent:
    """
    循环对外吐的事件。与 StreamEvent 分开而不是复用：
    一个是"检索-生成"两段式，一个是"多轮工具调用"，混在一个类里
    会让前端分不清"这次的 citations 是检索到的还是工具返回的"。
    """

    kind: str                      # start / thought / tool_call / tool_result / delta / final / error
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    answer: str = ""
    status: str = "answered"       # answered / declined / escalated / degraded /
                                   # awaiting_confirmation / inconsistent / error
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    draft_id: str = ""
    order_no: str = ""
    # 建单被强制中断时，这里放草稿预览，供前端展示给员工确认
    draft_preview: dict[str, Any] | None = None
    escalated: dict[str, Any] | None = None
    turns: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    degraded: bool = False
    degrade_reason: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer, "status": self.status,
            "tool_calls": self.tool_calls, "citations": self.citations,
            "draft_id": self.draft_id, "order_no": self.order_no,
            "draft_preview": self.draft_preview,
            "escalated": self.escalated, "turns": self.turns,
            "tokens": {"in": self.tokens_in, "out": self.tokens_out},
            "latency_ms": self.latency_ms, "degraded": self.degraded,
            "degrade_reason": self.degrade_reason, "error": self.error,
        }


class AgentLoop:
    """
    一次问答的完整循环。

    构造后调用 `run()`。无状态 —— 每次请求建一个新实例，
    所以两个并发请求之间不会互相污染 workspace。
    """

    def __init__(self, *, registry: Registry, model: Any, settings: Any,
                 retriever: Any = None, fallback: Any = None):
        self.registry = registry
        self.model = model
        self.s = settings
        # 降级用：阶段 1-4 的 Orchestrator。为 None 时降级只能返回提示文本
        self.fallback = fallback
        self.retriever = retriever

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def run(self, question: str, *, ctx: ToolContext,
            history: list[dict[str, Any]] | None = None,
            on_event: Callable[[LoopEvent], None] | None = None
            ) -> AgentResult:
        """
        跑一轮问答。返回最终结果。

        `on_event` 用于流式推送；不传就静默跑完（CLI 与单测走这条）。
        """
        t0 = time.perf_counter()
        emit = on_event or (lambda e: None)
        result = AgentResult()

        emit(LoopEvent("start", {"question": question}))

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_message(
                today=time.strftime("%Y-%m-%d"),
                legal_entity_id=str(getattr(self.s, "legal_entity_id", "") or ""))},
        ]
        for h in (history or [])[-6:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": str(h["content"])[:2000]})
        messages.append({"role": "user", "content": question})

        # 当前允许的工具集。None 表示"全部"（第 1 轮不裁剪）
        allowed: list[str] | None = None
        deadline = t0 + float(getattr(self.s, "agent_timeout_seconds", 60.0))
        max_calls = int(getattr(self.s, "agent_max_tool_calls", 8))
        max_turns = int(getattr(self.s, "agent_max_turns", 6))
        budget = int(getattr(self.s, "agent_token_budget", 30000))
        n_calls = 0
        last_data: dict[str, Any] = {}

        for turn in range(1, max_turns + 1):
            result.turns = turn

            # —— 超时与预算检查放在"每次生成之前" ——
            # 放在之后检查的话，最后一次生成可能已经烧掉几十秒，
            # 用户等到的却是一个"超时"提示
            if time.perf_counter() > deadline:
                return self._degrade(result, emit, messages, question, ctx,
                                     reason="timeout", t0=t0)
            if result.tokens_in + result.tokens_out > budget:
                return self._degrade(result, emit, messages, question, ctx,
                                     reason="token_budget", t0=t0)

            try:
                resp: ToolCallResponse = self.model.chat_with_tools(
                    messages, tools=self.registry.schemas(allowed),
                    temperature=0.2,
                    max_tokens=int(getattr(self.s, "llm_max_tokens", 900)))
            except Exception as ex:                # noqa: BLE001
                # 生成失败：也走降级而不是报错。用户关心的是答案，不是哪一层挂了
                log(f"Agent 生成失败：{type(ex).__name__}: {ex}", "err")
                return self._degrade(result, emit, messages, question, ctx,
                                     reason="llm_error", t0=t0, error=str(ex))

            result.tokens_in += resp.tokens_in
            result.tokens_out += resp.tokens_out

            # —— 记录本轮决策（每轮一条，与工具轨迹按 turn_no 对齐）——
            # agent_runs 记"模型想了什么、选了谁"，agent_traces 记"工具实际做了什么"，
            # 两者拼起来才是完整可回放的决策轨迹
            self._record_run(question, ctx, resp, turn)

            # —— 没有工具调用 = 模型认为可以收尾了 ——
            if not resp.has_tools:
                result.answer = resp.text or "（未能生成答复）"
                result.status = self._status_from_workspace(ctx)
                result.citations = self._citations_of(ctx)
                result.escalated = ctx.workspace.get("escalated")
                draft = ctx.workspace.get("draft") or {}
                result.draft_id = str(draft.get("id") or "")
                result.order_no = str(draft.get("order_no") or "")
                self._check_claim_consistency(result, ctx)
                result.latency_ms = int((time.perf_counter() - t0) * 1000)
                emit(LoopEvent("final", result.as_dict()))
                return result

            # 模型可能"边说边调"，把话也带上
            if resp.text:
                emit(LoopEvent("thought", {"text": resp.text}))

            # —— 执行本轮的所有工具调用 ——
            # 一次响应里可能有多个调用（并行工具），按顺序执行：
            # 这些工具大多有依赖（先查档案才知道工号），并行反而会出错
            for call in resp.tool_calls:
                n_calls += 1
                if n_calls > max_calls:
                    emit(LoopEvent("error", {
                        "code": E_BUDGET_EXCEEDED.code,
                        "message": f"本轮工具调用已达上限（{max_calls} 次），转人工"}))
                    return self._degrade(result, emit, messages, question, ctx,
                                         reason="tool_budget", t0=t0)
                if time.perf_counter() > deadline:
                    return self._degrade(result, emit, messages, question, ctx,
                                         reason="timeout", t0=t0)

                bound = self._bind_args(call.arguments, last_data)
                emit(LoopEvent("tool_call", {
                    "id": call.id, "name": call.name, "args": bound,
                    "parse_error": call.parse_error}))

                # —— 需要人工确认的工具：调用前强制中断 ——
                # 这是"create_draft 前停下"的实现点：不是靠提示词求模型别建单，
                # 而是循环在真正执行之前拦住，把草稿发给员工，等确认后由
                # 确认接口（routes_draft）走 registry.call 落库。模型自始至终
                # 无法自己把单建出来。
                #
                # 拦截条件：只有**带了 validation_token** 才拦（说明模型已经
                # 认真走过 check_rules、真的要建单了）。没带 token 就调 create_draft
                # 属于"跳过校验"，不该拦 —— 放行给工具，让闸门用 AGT_001 拒绝它，
                # 这样两种错误的处理路径清晰不混。
                spec = self.registry.get(call.name)
                if (spec is not None and spec.requires_human_confirm
                        and bound.get("validation_token")):
                    return self._await_confirmation(result, emit, call, bound, ctx, t0)

                if call.parse_error:
                    # 参数坏了：不执行，直接把错误当成工具结果回给模型，
                    # 让它自己修正。这是"坏参数不该 500"的落点
                    res = ToolResult(ok=False, error_code="AGT_007",
                                     error_message=f"参数解析失败：{call.parse_error}",
                                     next_allowed=["escalate_to_human", "ask_clarification"])
                else:
                    res = self.registry.call(call.name, bound, ctx)

                record = {"name": call.name, "args": bound, "ok": res.ok,
                          "error_code": res.error_code,
                          "next_allowed": list(res.next_allowed),
                          "summary": res.summary}
                result.tool_calls.append(record)
                emit(LoopEvent("tool_result", record))

                if res.ok and isinstance(res.data, dict):
                    last_data = res.data

                messages.append({
                    "role": "assistant", "content": resp.text or "",
                    "tool_calls": [{
                        "id": call.id, "type": "function",
                        "function": {"name": call.name,
                                     "arguments": json.dumps(bound, ensure_ascii=False)},
                    }],
                })
                messages.append({
                    "role": "tool", "tool_call_id": call.id,
                    "name": call.name,
                    "content": json.dumps(res.to_payload(), ensure_ascii=False, default=str),
                })

                # —— 工具集收窄 ——
                # 用"最后一次调用"的 next_allowed，而不是取交集：
                # 一轮里连续调两个工具时，交集会迅速缩成空集，
                # 让模型在下一轮什么都不能用
                if res.next_allowed:
                    allowed = list(res.next_allowed)

                # 转人工是终止信号：模型已经决定不做了，不必再生成一轮
                if ctx.workspace.get("escalated"):
                    result.answer = str(ctx.workspace["escalated"].get("summary") or "")
                    result.status = "escalated"
                    result.escalated = ctx.workspace["escalated"]
                    result.citations = self._citations_of(ctx)
                    draft = ctx.workspace.get("draft") or {}
                    result.draft_id = str(draft.get("id") or "")
                    result.order_no = str(draft.get("order_no") or "")
                    result.latency_ms = int((time.perf_counter() - t0) * 1000)
                    emit(LoopEvent("final", result.as_dict()))
                    return result

        # 轮数用完还没收尾 —— 说明模型在打转
        return self._degrade(result, emit, messages, question, ctx,
                             reason="max_turns", t0=t0)

    # ------------------------------------------------------------------ #
    # 流式包装
    # ------------------------------------------------------------------ #
    def stream(self, question: str, *, ctx: ToolContext,
               history: list[dict[str, Any]] | None = None
               ) -> Iterator[LoopEvent]:
        """
        把 run() 的事件转成生成器。

        刻意**不真流式**输出最终答案：Agent 模式的最终答案是在
        "所有工具执行完之后"才产生的，此时前面已经推送了工具调用事件。
        要真流式，得在模型决定不再调工具的那一轮改用 chat_stream()，
        而那时我们已经拿到了完整响应 —— 再流一次等于重复生成一遍。
        所以这里把最终答案当作一帧发出去，前端照常做打字机效果即可。
        """
        events: list[LoopEvent] = []
        yield from self._collect(self.run(question, ctx=ctx, history=history,
                                          on_event=events.append), events)

    def _collect(self, result: AgentResult, events: list[LoopEvent]) -> Iterator[LoopEvent]:
        yield from events
        if not events or events[-1].kind != "final":
            yield LoopEvent("final", result.as_dict())

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _record_run(self, question: str, ctx: ToolContext, resp: ToolCallResponse,
                    turn: int) -> None:
        """记录一轮模型决策。失败只记日志，绝不影响主流程（审计是旁路）。"""
        from ...store import db, trace_repo

        conn = None
        try:
            conn = db.connect(ctx.settings.chat_db_path)
            trace_repo.record_run(
                conn, session_id=ctx.session_id, turn_no=turn, question=question,
                model=resp.model, tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                finish_reason=resp.finish_reason, thought=resp.text,
                chose_tools=[c.name for c in resp.tool_calls])
        except Exception as ex:                        # noqa: BLE001
            log(f"写 Agent 决策日志失败（turn={turn}）：{ex}", "warn")
        finally:
            if conn is not None:
                conn.close()

    def _await_confirmation(self, result: AgentResult, emit: Callable[[LoopEvent], None],
                            call: Any, args: dict[str, Any], ctx: ToolContext,
                            t0: float) -> AgentResult:
        """
        建单前的强制中断。

        不执行 create_draft 的落库逻辑，而是把"将要建的单"组装成预览返回，
        状态置 awaiting_confirmation。之后由员工在界面上确认，确认接口
        （`routes_draft`）拿着这份预览里回传的 validation_token 去真正落库。

        组装草稿预览时，内容来自两个地方：
          · call.arguments —— 模型填的 change_type / employee_no / items / materials
          · workspace.last_verdict —— check_rules 算出的 risk_level / blocking / warnings
        这样员工看到的不是模型"口述"的草稿，而是**规则引擎判定过的那份**。
        """
        from . import gate

        change_type = str(args.get("change_type") or "")
        employee_no = str(args.get("employee_no") or "")
        items = args.get("items") or []
        materials = [str(m) for m in (args.get("materials") or [])]
        token = str(args.get("validation_token") or "")
        verdict = ctx.workspace.get("last_verdict") or {}
        emp = ctx.workspace.get("employee") or {}

        chash = gate.order_hash(change_type, employee_no, items, materials)
        preview = {
            "change_type": change_type,
            "employee_no": employee_no,
            "employee_name": str(emp.get("display_name") or ""),
            "items": items,
            "materials": materials,
            "risk_level": verdict.get("level"),
            "blocking": verdict.get("blocking") or [],
            "warnings": verdict.get("warnings") or [],
            "draft_hash": chash,
            # 回传给确认接口：员工确认后拿它去落库，落库前还会重新验签
            "validation_token": token,
        }

        result.status = "awaiting_confirmation"
        result.draft_preview = preview
        # 拦截后直接收尾，模型这轮的 thought 没进 answer —— 补一句默认提示，
        # 否则前端气泡是空的，员工只看到一张确认卡片会摸不着头脑
        result.answer = (result.answer
                         or "已为您生成变更单草稿，请核对以下内容，确认无误后点击「确认建单」。")
        result.tool_calls.append({"name": call.name, "args": args, "ok": False,
                                  "error_code": "", "next_allowed": [],
                                  "summary": "已拦截：等待员工确认草稿"})
        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        emit(LoopEvent("confirmation_required", {"draft": preview}))
        emit(LoopEvent("final", result.as_dict()))
        return result

    def _bind_args(self, args: dict[str, Any],
                   last_data: dict[str, Any]) -> dict[str, Any]:
        """
        替换 `$last.<字段>` 占位符。

        **只服务于脚本化假 Agent**：真模型看到工具返回后会自己把 token 填进去，
        不需要这个机制。但剧本是写死的，没有占位符就测不到"拿到凭证 → 建单成功"
        这条正路。

        只处理一层（`$last.validation_token`），不做嵌套路径 ——
        剧本表达力够用就行，做得太通用反而会让测试读起来像模板引擎。
        """
        out: dict[str, Any] = {}
        for k, v in args.items():
            if isinstance(v, str) and v.startswith("$last."):
                out[k] = last_data.get(v[len("$last."):], "")
            else:
                out[k] = v
        return out

    def _status_from_workspace(self, ctx: ToolContext) -> str:
        if ctx.workspace.get("escalated"):
            return "escalated"
        if ctx.workspace.get("draft"):
            # 有草稿但没提交 —— 正常，等人工确认
            return "drafted"
        return "answered"

    # ------------------------------------------------------------------ #
    # 答案与实际状态的一致性
    # ------------------------------------------------------------------ #
    # 声称"已完成某个写操作"的说法。只收完成态，不收建议或将来时 ——
    # "可以为您建单""建议提交"是正常措辞，不该被误判
    _CLAIM_PATTERNS = (
        ("created", ("已建单", "已创建", "已为您建", "建单成功", "已生成变更单",
                     "草稿已创建", "已为您创建")),
        ("submitted", ("已提交", "提交成功", "已进入审批", "已送审")),
    )

    def _check_claim_consistency(self, result: AgentResult, ctx: ToolContext) -> None:
        """
        核对"模型说了什么"与"系统实际发生了什么"。

        为什么需要这一步：工具被拒绝之后，模型仍然完全可能说"好的，已为您建单"
        —— 它看到的是一个失败的工具结果，但生成时会倾向于"顺着用户的期待说"。
        用户看到这句话就不会再去点确认，于是这张单永远不存在，而用户以为它在审批中。

        这不是万无一失的语义检查（用关键词），但它把"最坏的那种谎"暴露出来了：
        声称做了写操作、实际没做。命中时降级状态并改写答复开头，
        因为**改文案比改状态更直接** —— 状态给运维看，文案给用户看。
        """
        has_draft = bool(ctx.workspace.get("draft"))
        text = result.answer or ""

        for kind, words in self._CLAIM_PATTERNS:
            if not any(w in text for w in words):
                continue
            if kind == "created" and has_draft:
                continue
            if kind == "submitted":
                draft = ctx.workspace.get("draft") or {}
                if str(draft.get("status") or "") == "submitted":
                    continue
            # 声称了但没证据
            result.status = "inconsistent"
            result.answer = (
                "⚠ 需要说明：本次并没有实际完成"
                + ("建单" if kind == "created" else "提交")
                + "（服务端未记录到对应操作）。\n\n" + text)
            log(f"Agent 声称{kind}但系统无对应记录，已标记为不一致", "warn")
            return

    @staticmethod
    def _citations_of(ctx: ToolContext) -> list[dict[str, Any]]:
        """把 lookup_policy 命中的条款整理成引用。没有就空列表，不编。"""
        hits = ctx.workspace.get("policy_hits") or []
        out, seen = [], set()
        for h in hits:
            cid = h.get("chunk_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append({"index": len(out) + 1, "chunk_id": cid,
                        "clause_no": h.get("clause_no"), "doc_title": h.get("doc_title"),
                        "snippet": (h.get("content") or "")[:160]})
            if len(out) >= 5:
                break
        return out

    def _degrade(self, result: AgentResult, emit: Callable[[LoopEvent], None],
                 messages: list[dict[str, Any]], question: str, ctx: ToolContext,
                 *, reason: str, t0: float, error: str = "") -> AgentResult:
        """
        降级到固定 RAG 流程。

        这是"Agent 出问题时的兜底"，不是错误处理 —— 用户应该拿到一个
        带引用的答案，哪怕它没有 Agent 那么聪明。

        为什么保留已经产生的工具调用记录：运维要能看出"它是在第 3 轮
        因为 token 预算耗尽的"，而不是只知道"降级了"。
        """
        result.degraded = True
        result.degrade_reason = reason
        result.error = error
        result.tool_calls = result.tool_calls        # 保留轨迹
        result.turns = max(1, result.turns)

        # 降级也记一条决策日志：finish_reason 就是降级原因，
        # 否则回放时"为什么第 3 轮之后就没有决策了"无从查起
        from ...store import db, trace_repo
        conn = None
        try:
            conn = db.connect(ctx.settings.chat_db_path)
            trace_repo.record_run(
                conn, session_id=ctx.session_id, turn_no=result.turns,
                question=question, model="", tokens_in=0, tokens_out=0,
                finish_reason=f"degraded:{reason}", thought="",
                chose_tools=[], degraded=True)
        except Exception as ex:                        # noqa: BLE001
            log(f"写降级决策日志失败：{ex}", "warn")
        finally:
            if conn is not None:
                conn.close()

        if self.fallback is not None:
            try:
                ans = self.fallback.answer(question)
                result.answer = ans.answer
                result.status = "degraded"
                result.citations = [
                    {"index": c.index, "chunk_id": c.chunk_id, "clause_no": c.clause_no,
                     "doc_title": c.doc_title, "snippet": c.snippet}
                    for c in (ans.citations or [])
                ]
            except Exception as ex:                    # noqa: BLE001
                log(f"降级也失败了：{ex}", "err")
                result.answer = self._degrade_text(reason)
                result.status = "error"
        else:
            result.answer = self._degrade_text(reason)
            result.status = "degraded"

        result.latency_ms = int((time.perf_counter() - t0) * 1000)
        emit(LoopEvent("final", result.as_dict()))
        return result

    @staticmethod
    def _degrade_text(reason: str) -> str:
        """降级文案。**不出现"Agent""循环""预算"这类内部词**，用户看不懂。"""
        return {
            "timeout": "这个问题处理超时了，我先把制度里相关的规定找给你，"
                       "如果需要进一步核对，请联系 SSC。",
            "token_budget": "这个问题比较复杂，我先把制度里相关的规定找给你，"
                            "细节建议联系 SSC 人工核对。",
            "tool_budget": "我尝试了几次都没能完成核对，建议转人工处理。",
            "max_turns": "这个问题需要更多轮确认，建议转人工处理。",
            "llm_error": "生成回答时出了点问题，我先把制度里相关的规定找给你。",
        }.get(reason, "暂时无法完成这个请求，建议联系 SSC。")


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------

def run_agent(question: str, *, settings: Any, registry: Registry,
              model: Any, user: dict[str, Any] | None = None,
              session_id: str = "", retriever: Any = None,
              fallback: Any = None, history: list[dict[str, Any]] | None = None,
              on_event: Callable[[LoopEvent], None] | None = None) -> AgentResult:
    """一次性调用。适合 CLI 与单测；服务端用 AgentLoop 实例以便复用。"""
    loop = AgentLoop(registry=registry, model=model, settings=settings,
                     retriever=retriever, fallback=fallback)
    ctx = ToolContext(settings=settings, user=user or {}, session_id=session_id,
                      retriever=retriever)
    return loop.run(question, ctx=ctx, history=history, on_event=on_event)
