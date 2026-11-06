# -*- coding: utf-8 -*-
"""跑 3 个典型对话场景，把**真实执行轨迹**导出成 JSON，供文档与配图使用。

为什么用脚本化模型（MockToolCallingModel）而不是真模型：
  这份产物是"测试过程文档"的证据。真模型输出有随机性，同一份文档两次跑出来的
  轨迹会不一样，读者无法复现；脚本化模型给出确定性轨迹，且**经过的是同一套
  AgentLoop / 工具 / 闸门代码**——被验证的是系统行为，不是模型文采。

三个场景（对应 docs/agent-traces.md）：
  A 正常调薪     → 校验通过 → 建单被拦截 → 员工确认 → 落库
  B 信息缺失     → ask_clarification → 补充信息 → 校验通过 → 再次被拦截
  C 降薪缺确认   → check_rules 阻断 → 工具集收窄 → escalate_to_human

输出：
  docs/assets/scenario-traces.json   原始轨迹（给配图脚本与文档引用）
  docs/assets/scenario-summary.md    人读的摘要（排查用）

用法：python scripts/scenario_traces.py
"""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from my_tool.core.agent import seed                              # noqa: E402
from my_tool.core.agent.loop import AgentLoop                    # noqa: E402
from my_tool.core.agent.registry import ToolContext              # noqa: E402
from my_tool.core.agent.toolset import build_registry            # noqa: E402
from my_tool.core.config import Settings                          # noqa: E402
from my_tool.core.llm.mock_agent import MockToolCallingModel      # noqa: E402
from my_tool.core.registry import register_document_file          # noqa: E402
from my_tool.store import db, order_repo, trace_repo              # noqa: E402

OUT_DIR = ROOT / "docs" / "assets"

EMPLOYEE = "E1001"

# 场景 A / B 的明细项（写死成常量，避免两处不一致导致校验内容哈希对不上）
SALARY_ITEMS = [{"field": "fixed_salary", "old_value": "18500",
                 "new_value": "19000", "effective_date": "2026-12-01"}]
BANK_ITEMS = [{"field": "bank_account", "old_value": "6222021234567890",
               "new_value": "6217001234567890", "effective_date": "2026-12-01"}]


def build_env() -> tuple[Settings, ToolContext]:
    """隔离环境：临时库 + 参数/员工种子 + hash 索引（不联网、可重复）。"""
    base = Settings.from_env()
    tmp = Path(tempfile.mkdtemp(prefix="scn-doc-"))
    s = dataclasses.replace(
        base,
        chat_db_path=tmp / "chat.db", index_dir=tmp / "index", upload_dir=tmp / "up",
        embedding_provider="hash", gate_secret="scenario-doc-secret",
        llm_provider="mock", agent_enabled=True, agent_max_turns=5,
        agent_max_tool_calls=8, agent_token_budget=30000, agent_timeout_seconds=60.0,
    )
    db.init_db(s.chat_db_path)
    conn = db.connect(s.chat_db_path)
    try:
        seed.seed_params(conn)
        seed.seed_employees(conn)
    finally:
        conn.close()

    doc = Path(base.doc_path)
    if not doc.exists():
        doc = ROOT / "data" / "policy" / "员工薪酬变更核对制度_v1.0.md"
    register_document_file(s, doc)

    from my_tool.api.app import AppContext
    from my_tool.core.index_manager import IndexManager
    app_ctx = AppContext(settings=s)
    IndexManager(s, app_ctx).rebuild_now(reason="scenario-doc")

    return s, app_ctx.orchestrator.retriever


def new_ctx(s: Settings, retriever: object, sid: str) -> ToolContext:
    """每个场景一个独立 workspace。

    共用同一个 ctx 会让上一个场景的 workspace["draft"] 漏到下一个场景，
    表现为"只做澄清的一轮"也被判成 drafted —— 排查时极容易误判成循环的状态机有 bug。
    """
    return ToolContext(settings=s, user={"id": "doc-user", "role": "admin",
                                         "display_name": "演示员工"},
                       session_id=sid, retriever=retriever)


def run_turn(s: Settings, ctx: ToolContext, question: str,
             steps: list, history: list[dict] | None = None) -> dict:
    """跑一轮，收集事件流水。返回可 JSON 化的轨迹片段。

    用 run(on_event=...) 而不是 stream()：需要保留 AgentResult 本体——
    拦截时的 `draft_preview.validation_token` 只存在结果对象上，事件里只带了展示字段。
    """
    model = MockToolCallingModel(steps, final_text="（本轮结束）")
    loop = AgentLoop(registry=build_registry(), model=model, settings=s,
                     retriever=ctx.retriever, fallback=None)

    events: list[dict] = []
    t0 = time.perf_counter()

    def on_event(ev) -> None:
        payload = dict(ev.data or {})
        if ev.kind == "tool_result":
            payload = {k: payload.get(k) for k in
                       ("name", "ok", "error_code", "summary", "next_allowed")}
        elif ev.kind == "tool_call":
            payload = {"name": payload.get("name"), "args": payload.get("args")}
        elif ev.kind == "confirmation_required":
            draft = payload.get("draft") or {}
            payload = {"status": "awaiting_confirmation",
                       "draft": {k: draft.get(k) for k in
                                 ("employee_no", "change_type", "risk_level",
                                  "items", "warnings", "blocking", "draft_hash")}}
        elif ev.kind == "final":
            payload = {"status": payload.get("status"),
                       "answer": payload.get("answer"),
                       "turns": payload.get("turns")}
        events.append({"kind": ev.kind, "data": payload,
                       "t_ms": int((time.perf_counter() - t0) * 1000)})

    result = loop.run(question, ctx=ctx, history=history, on_event=on_event)
    return {"question": question, "events": events,
            "final": {"status": result.status, "answer": result.answer,
                      "turns": result.turns, "degraded": result.degraded},
            "draft_preview": result.draft_preview,
            "tool_calls": result.tool_calls,
            "seen_tools": [list(x) for x in model.seen_tools],
            "elapsed_ms": int((time.perf_counter() - t0) * 1000)}


def scenario_a(s: Settings, retriever: object) -> dict:
    """A：正常调薪 —— 关键在于第 4 步被拦截，且此时库里 0 张单。"""
    ctx = new_ctx(s, retriever, "scn-a")
    steps = [
        ("tool", "get_employee", {"employee_no": EMPLOYEE}),
        ("tool", "lookup_param", {"keys": ["raise_cap_pct", "diff_l2_max", "diff_l3_max"],
                                  "region_code": "310000"}),
        ("tool", "check_rules", {"change_type": "salary_adjust", "employee_no": EMPLOYEE,
                                 "items": SALARY_ITEMS,
                                 "materials": ["approval_form", "adjust_basis"]}),
        ("tool", "create_draft", {"validation_token": "$last.validation_token",
                                  "change_type": "salary_adjust", "employee_no": EMPLOYEE,
                                  "items": SALARY_ITEMS,
                                  "materials": ["approval_form", "adjust_basis"],
                                  "idempotency_key": "scn-a"}),
        ("text", "已生成草稿，请您核对后在界面上确认。"),
    ]
    turn = run_turn(s, ctx, "帮我把固定薪资从 18500 调到 19000，12 月 1 日生效", steps)

    preview = turn.get("draft_preview") or {}
    conn = db.connect(s.chat_db_path)
    try:
        before = order_repo.count(conn)
    finally:
        conn.close()

    # 员工点"确认" → 与 /api/agent/confirm-draft 走同一条落库路径
    confirm = {}
    if preview.get("validation_token"):
        res = build_registry().call("create_draft", {
            "validation_token": preview["validation_token"],
            "change_type": "salary_adjust", "employee_no": EMPLOYEE,
            "items": SALARY_ITEMS,
            "materials": ["approval_form", "adjust_basis"],
            "idempotency_key": preview.get("draft_hash", "scn-a"),
        }, ctx)
        confirm = {"ok": res.ok, "error": res.error_message,
                   "order_no": (res.data or {}).get("order_no", "")}

    conn = db.connect(s.chat_db_path)
    try:
        after = order_repo.count(conn)
        row = conn.execute("SELECT order_no, status, risk_level FROM change_orders "
                           "ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        conn.close()

    return {"id": "A", "title": "正常调薪：建单前强制拦截",
            "turns": [turn], "orders_before": before, "orders_after": after,
            "confirmed": confirm,
            "order": dict(row) if row else {}}


def scenario_b(s: Settings, retriever: object) -> dict:
    """B：信息缺失 —— 先追问，补全后再走校验与拦截。"""
    ctx = new_ctx(s, retriever, "scn-b")
    t1 = run_turn(s, ctx, "帮我改一下银行卡", [
        ("tool", "ask_clarification", {"questions": [
            {"field": "bank_account", "question": "请提供新的银行卡号"},
            {"field": "effective_date", "question": "这笔变更希望什么时候生效？"},
        ]}),
        ("text", "请补充新卡号与生效日期，我再帮您校验。"),
    ])

    history = [{"role": "user", "content": "帮我改一下银行卡"},
               {"role": "assistant", "content": t1["final"].get("answer") or ""}]
    t2 = run_turn(s, ctx, "卡号 6217001234567890，12 月 1 日生效", [
        ("tool", "get_employee", {"employee_no": EMPLOYEE}),
        ("tool", "lookup_policy", {"query": "银行账户变更 双人复核 生效 材料"}),
        ("tool", "check_rules", {"change_type": "bank_account", "employee_no": EMPLOYEE,
                                 "items": BANK_ITEMS,
                                 "materials": ["bank_change_form", "bank_account_image"]}),
        ("tool", "create_draft", {"validation_token": "$last.validation_token",
                                  "change_type": "bank_account", "employee_no": EMPLOYEE,
                                  "items": BANK_ITEMS,
                                  "materials": ["bank_change_form", "bank_account_image"],
                                  "idempotency_key": "scn-b"}),
        ("text", "已生成草稿，请核对后确认。"),
    ], history=history)

    return {"id": "B", "title": "信息缺失：先澄清，不猜", "turns": [t1, t2]}


def scenario_c(s: Settings, retriever: object) -> dict:
    """C：降薪无书面确认 —— 命中 final 阻断，工具集收窄，转人工。"""
    ctx = new_ctx(s, retriever, "scn-c")
    steps = [
        ("tool", "get_employee", {"employee_no": EMPLOYEE}),
        ("tool", "check_rules", {"change_type": "salary_adjust", "employee_no": EMPLOYEE,
                                 "items": [{"field": "fixed_salary", "old_value": "18500",
                                            "new_value": "15000", "effective_date": "2026-12-01"}],
                                 "materials": ["approval_form", "adjust_basis"]}),
        ("tool", "escalate_to_human", {"reason_code": "blocked_final",
                                       "summary": "降薪缺少员工书面确认，属最终阻断，需人工介入",
                                       "context": {"clause": "第二十七条",
                                                   "code": "BLK_SALARY_CUT_NO_CONSENT"}}),
        ("text", "该降薪缺少员工书面确认，不能直接建单，已转人工核对。"),
    ]
    turn = run_turn(s, ctx, "把 E1001 的薪资降到 15000", steps)
    return {"id": "C", "title": "降薪缺书面确认：阻断并转人工", "turns": [turn]}


def _logs(s: Settings, session_id: str) -> dict:
    conn = db.connect(s.chat_db_path)
    try:
        return {"runs": [dict(r) for r in trace_repo.list_runs(conn, session_id)],
                "traces": [dict(r) for r in trace_repo.list_traces(conn, session_id)]}
    finally:
        conn.close()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s, retriever = build_env()

    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "employee": EMPLOYEE, "scenarios": []}

    a = scenario_a(s, retriever)
    out["scenarios"].append(a)
    print(f"A {a['title']}：拦截后单数 {a['orders_before']} → 确认后 {a['orders_after']}"
          f"，单号 {a['confirmed'].get('order_no') or '—'}")

    b = scenario_b(s, retriever)
    out["scenarios"].append(b)
    print(f"B {b['title']}：{len(b['turns'])} 轮")

    c = scenario_c(s, retriever)
    out["scenarios"].append(c)
    print(f"C {c['title']}：状态 {c['turns'][0]['final'].get('status')}")

    for sc in out["scenarios"]:
        sc["logs"] = _logs(s, f"scn-{sc['id'].lower()}")

    path = OUT_DIR / "scenario-traces.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
