"""阶段 5 · Agent 循环（批次 4）。

这一组测的都是"刹车"与"降级"，因为循环本身很简单（调用→执行→再调用），
真正会出事故的是它能跑多久、跑飞了怎么办、被拒了会不会死循环。

四条铁律，每条至少一个用例：
  1. 工具调用次数有上限，触顶不抛异常而是降级
  2. 轮数有上限，打转的模型会被刹住
  3. 生成失败 / 超时降级到固定 RAG，用户仍能拿到带引用的答案
  4. 失败后的 next_allowed 收窄工具集，模型下一轮看不到"不该用"的工具
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from my_tool.core.agent import seed
from my_tool.core.agent.loop import AgentLoop, LoopEvent
from my_tool.core.agent.registry import ToolContext
from my_tool.core.agent.toolset import build_registry
from my_tool.core.config import Settings
from my_tool.core.llm.mock_agent import (MockToolCallingModel,
                                         script_bad_args, script_happy_path,
                                         script_infinite_retry, script_skip_gate)
from my_tool.store import db, order_repo


@pytest.fixture()
def s(tmp_path) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(
        base, chat_db_path=tmp_path / "chat.db", index_dir=tmp_path / "index",
        upload_dir=tmp_path / "up", gate_secret="test-gate-secret",
        llm_provider="mock", agent_enabled=True,
        agent_max_tool_calls=8, agent_max_turns=6, agent_token_budget=30000,
        agent_timeout_seconds=60.0)


@pytest.fixture()
def conn(s):
    db.init_db(s.chat_db_path)
    c = db.connect(s.chat_db_path)
    seed.seed_params(c)
    seed.seed_employees(c)
    yield c
    c.close()


def _ctx(s, **kw) -> ToolContext:
    return ToolContext(settings=s, user={"id": "u-admin", "role": "admin"},
                       session_id="loop-test", **kw)


def _run(s, steps, *, ctx=None, result_steps=None, **kw) -> tuple:
    model = MockToolCallingModel(steps if result_steps is None else result_steps,
                                 final_text="以上就是核对结果。")
    loop = AgentLoop(registry=build_registry(), model=model, settings=s)
    ctx = ctx or _ctx(s)
    events: list[LoopEvent] = []
    res = loop.run("帮我把固定薪资从 18500 调到 19000", ctx=ctx,
                   on_event=events.append, **kw)
    return res, model, events


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------

def test_happy_path_intercepts_before_draft(conn, s):
    """
    查档案 → 查参数 → 校验 → 模型想建单时**被强制拦截**。

    重点：create_draft 不再落库，而是返回 awaiting_confirmation + 草稿预览，
    员工确认后由确认接口落库。循环自始至终不能自己把单建出来。
    """
    res, model, events = _run(s, script_happy_path())

    assert res.degraded is False
    assert res.status == "awaiting_confirmation", f"应停在待确认，实际 {res.status}"
    assert res.draft_preview is not None
    assert res.draft_preview["change_type"] == "salary_adjust"
    assert res.draft_preview["employee_no"] == "E1001"
    assert res.draft_preview["risk_level"] == "L2"
    # 预览里回传了 validation_token，确认接口拿它去落库
    assert res.draft_preview["validation_token"]
    # 关键：被拦截后**一张单都没落库**
    assert order_repo.count(conn) == 0

    kinds = [e.kind for e in events]
    assert "confirmation_required" in kinds
    assert kinds[-1] == "final"


def test_loop_passes_tools_and_narrows_them(conn, s):
    """
    工具集逐轮收窄：第一轮给全量，check_rules 之后只剩 create_draft / escalate。

    这是"顺序约束第 2 层"的实证：模型下一轮**看不到**别的工具，
    不需要靠它记住提示词里的流程。
    """
    res, model, _ = _run(s, script_happy_path())
    first, after_check = model.seen_tools[0], model.seen_tools[3]
    assert len(first) == 9, "第一轮应给全部工具"
    assert set(after_check) == {"create_draft", "escalate_to_human"}
    assert res.tool_calls[2]["next_allowed"] == ["create_draft", "escalate_to_human"]


def test_tool_messages_are_recorded(conn, s):
    """工具调用与结果要以 assistant/tool 消息的形式回灌，否则模型看不到结果。"""
    captured: list[list[dict]] = []

    class Recorder(MockToolCallingModel):
        def chat_with_tools(self, messages, **kw):
            captured.append([dict(m) for m in messages])
            return super().chat_with_tools(messages, **kw)

    model = Recorder(script_happy_path())
    loop = AgentLoop(registry=build_registry(), model=model, settings=s)
    loop.run("调薪", ctx=_ctx(s))

    second = captured[1]
    roles = [m["role"] for m in second]
    assert "tool" in roles, "第二轮必须能看到上一轮的工具结果"
    tool_msgs = [m for m in second if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"]
    assert "employee_no" in tool_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 绕过尝试：循环层面也要挡住
# ---------------------------------------------------------------------------

def test_skip_gate_is_blocked_and_no_order_created(conn, s):
    """
    模型跳过校验直接建单 —— 这是循环最容易"帮凶"的场景。

    循环不应该替它兜底（比如自动补一次 check_rules），
    而应该让工具拒绝、把拒绝结果原样回灌，让模型自己面对失败。
    """
    res, _, _ = _run(s, script_skip_gate())
    assert res.tool_calls[0]["ok"] is False
    assert res.tool_calls[0]["error_code"] == "AGT_001"
    assert order_repo.count(conn) == 0, "被拒时一张单都不该落库"


def test_model_claiming_success_after_rejection_is_flagged(conn, s):
    """
    **模型谎报写操作**：工具被拒了，它仍然说"好的，已为您建单"。

    这不是假设 —— `script_skip_gate` 里那句"已为您建单"就是按真模型的行为写的。
    它的危害很具体：用户看到"已建单"就不会去点确认，而单根本不存在。

    处理方式是改写答复开头 + 状态标记 inconsistent，
    因为**改文案比改状态更直接**：状态给运维看，文案给用户看。
    """
    res, _, _ = _run(s, script_skip_gate())
    assert res.status == "inconsistent"
    assert res.answer.startswith("⚠ 需要说明")
    assert res.draft_id == ""
    assert order_repo.count(conn) == 0


def test_normal_answer_is_not_flagged(conn, s):
    """正常答复不能被误判 —— 只匹配完成态，不匹配"可以建单""建议提交"。"""
    res, _, _ = _run(s, [
        ("text", "按第八条，冻结期内可以受理银行账户变更，但不参与当期发放。"
                 "如果要建单，建议先准备账户变更申请。"),
    ])
    assert res.status == "answered"
    assert not res.answer.startswith("⚠")


def test_draft_preview_carries_rule_verdict(conn, s):
    """
    草稿预览里必须带**规则引擎的判定结果**（风险等级 + 提示项），
    而不是模型的"口述"。

    员工在确认卡片上看到的，应该和 check_rules 算出来的一致 ——
    否则"确认"确认的是模型转述的、可能美化过的版本。
    这里用"不填生效日"的输入：check_rules 判定 pass L2，但带
    WARN_NO_EFFECTIVE_DATE 提示；这个提示必须出现在预览里。
    """
    items = [{"field": "fixed_salary", "old_value": "18500", "new_value": "19000"}]
    res, _, _ = _run(s, [
        ("tool", "get_employee", {"employee_no": "E1001"}),
        ("tool", "check_rules", {
            "change_type": "salary_adjust", "employee_no": "E1001",
            "items": items, "materials": ["approval_form", "adjust_basis"]}),
        ("tool", "create_draft", {
            "validation_token": "$last.validation_token",
            "change_type": "salary_adjust", "employee_no": "E1001",
            "items": items, "materials": ["approval_form", "adjust_basis"],
            "idempotency_key": "preview-verdict"}),
    ])
    assert res.status == "awaiting_confirmation"
    assert res.draft_preview["risk_level"] == "L2"
    codes = [w["code"] for w in res.draft_preview["warnings"]]
    assert "WARN_NO_EFFECTIVE_DATE" in codes, "缺生效日的提示必须带进预览给员工看"
    assert order_repo.count(conn) == 0


# ---------------------------------------------------------------------------
# 刹车
# ---------------------------------------------------------------------------

def test_tool_call_budget_stops_runaway_model(conn, s):
    """无限重试必须被预算刹住，且是降级不是异常。"""
    s2 = dataclasses.replace(s, agent_max_tool_calls=3)
    res, _, events = _run(s2, script_infinite_retry())
    assert res.degraded is True
    assert res.degrade_reason == "tool_budget"
    assert len(res.tool_calls) <= 3
    assert order_repo.count(conn) == 0


def test_max_turns_stops_looping_model(conn, s):
    """轮数上限：模型一直要工具、从不肯收尾。"""
    s2 = dataclasses.replace(s, agent_max_turns=3, agent_max_tool_calls=100)
    model = MockToolCallingModel(
        [("tool", "lookup_param", {"keys": ["freeze_start_day"]})], repeat_last=True)
    loop = AgentLoop(registry=build_registry(), model=model, settings=s2)
    res = loop.run("一直查", ctx=_ctx(s2))
    assert res.degraded is True
    assert res.degrade_reason in ("max_turns", "token_budget")


def test_token_budget_degrades(conn, s):
    """token 预算耗尽 → 降级，而不是继续烧钱。"""
    s2 = dataclasses.replace(s, agent_token_budget=5, agent_max_turns=10,
                             agent_max_tool_calls=100)
    model = MockToolCallingModel(
        [("tool", "lookup_param", {"keys": ["freeze_start_day"]})], repeat_last=True)
    loop = AgentLoop(registry=build_registry(), model=model, settings=s2)
    res = loop.run("一直查", ctx=_ctx(s2))
    assert res.degraded is True
    assert res.degrade_reason in ("token_budget", "max_turns")


# ---------------------------------------------------------------------------
# 降级
# ---------------------------------------------------------------------------

class _FakeFallback:
    """假降级通道：返回一个带引用的答案，验证降级后仍有可用输出。"""

    def __init__(self, text: str = "按第八条，每月 25 日至月末为冻结期。"):
        self.text = text
        self.calls = 0

    def answer(self, question):
        from types import SimpleNamespace
        self.calls += 1
        return SimpleNamespace(
            answer=self.text, status="answered",
            citations=[SimpleNamespace(index=1, chunk_id="c1", clause_no="第八条",
                                       doc_title="员工薪酬变更核对制度",
                                       snippet="每月 25 日至月末为冻结期")])


def test_degrade_uses_fallback_and_keeps_citations(conn, s):
    """降级必须还能给出带引用的答案 —— 用户关心的是有东西看，不是哪层挂了。"""
    s2 = dataclasses.replace(s, agent_max_tool_calls=1)
    fb = _FakeFallback()
    model = MockToolCallingModel(script_infinite_retry())
    loop = AgentLoop(registry=build_registry(), model=model, settings=s2, fallback=fb)
    res = loop.run("调薪", ctx=_ctx(s2))

    assert res.degraded is True
    assert fb.calls == 1
    assert res.status == "degraded"
    assert res.citations and res.citations[0]["clause_no"] == "第八条"
    assert "冻结期" in res.answer


def test_degrade_text_has_no_internal_jargon(conn, s):
    """降级文案不能出现 Agent / 循环 / 预算 这类内部词，用户看不懂。"""
    from my_tool.core.agent.loop import AgentLoop as AL
    for reason in ("timeout", "token_budget", "tool_budget", "max_turns", "llm_error"):
        text = AL._degrade_text(reason)
        for jargon in ("Agent", "agent", "循环", "预算", "token", "LLM"):
            assert jargon not in text, f"{reason} 的文案出现了内部词 {jargon}"


def test_llm_error_degrades_not_raises(conn, s):
    """模型抛异常时降级，不能让整个请求 500。"""

    class Boom(MockToolCallingModel):
        def chat_with_tools(self, messages, **kw):
            raise RuntimeError("上游炸了")

    loop = AgentLoop(registry=build_registry(), model=Boom(), settings=s,
                     fallback=_FakeFallback())
    res = loop.run("调薪", ctx=_ctx(s))
    assert res.degraded is True
    assert res.degrade_reason == "llm_error"
    assert res.answer


# ---------------------------------------------------------------------------
# 坏参数与边界
# ---------------------------------------------------------------------------

def test_bad_arguments_do_not_crash(conn, s):
    """模型生成坏 JSON：当成一次失败的工具调用，不是 500。"""
    res, _, _ = _run(s, script_bad_args())
    assert res.status != "error"
    assert res.tool_calls[0]["ok"] is False
    assert res.tool_calls[0]["error_code"] == "AGT_007"
    assert res.answer


def test_escalate_ends_the_loop_immediately(conn, s):
    """转人工是终止信号：不必再生成一轮。"""
    res, model, _ = _run(s, [
        ("tool", "escalate_to_human", {
            "reason_code": "param_missing", "summary": "该地区参数缺失"}),
        ("text", "这段不该被用上"),
    ])
    assert res.status == "escalated"
    assert res.escalated["reason_code"] == "param_missing"
    assert model.cursor == 1, "转人工之后不该再请求模型"


def test_history_is_included_but_bounded(conn, s):
    """历史只取最近 6 条，超长内容截断 —— 否则上下文会被撑爆。"""
    captured: list[list[dict]] = []

    class Recorder(MockToolCallingModel):
        def chat_with_tools(self, messages, **kw):
            captured.append(list(messages))
            return super().chat_with_tools(messages, **kw)

    hist = [{"role": "user" if i % 2 == 0 else "assistant",
             "content": f"历史{i}" + "x" * 5000} for i in range(20)]
    loop = AgentLoop(registry=build_registry(), model=Recorder(), settings=s)
    loop.run("新问题", ctx=_ctx(s), history=hist)

    msgs = captured[0]
    body = [m for m in msgs if m["role"] != "system"]
    assert len(body) <= 7, "6 条历史 + 1 条当前问题"
    assert max(len(str(m["content"])) for m in body) <= 2000


def test_system_prompt_contains_the_hard_rules(conn, s):
    """
    提示词里必须有关键约束。这条测的是"我们以为写了、其实漏了"的情况 ——
    提示词是长字符串，改的时候很容易整段删掉而测试全绿。
    """
    captured: list[list[dict]] = []

    class Recorder(MockToolCallingModel):
        def chat_with_tools(self, messages, **kw):
            captured.append(list(messages))
            return super().chat_with_tools(messages, **kw)

    loop = AgentLoop(registry=build_registry(), model=Recorder(), settings=s)
    loop.run("调薪", ctx=_ctx(s))
    system = captured[0][0]["content"]

    for must in ("没有 validation_token，就不能调用 create_draft",
                 "不要修改参数重试",
                 "转人工是正确收尾",
                 "不得从条款文本里摘抄"):
        assert must in system, f"系统提示词缺少关键约束：{must}"
    assert date.today().isoformat()[:4] in system, "应注入当前日期（冻结期依赖它）"
