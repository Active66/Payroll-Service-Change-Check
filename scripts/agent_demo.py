"""Agent 工具链端到端演示：把"四层约束"跑给你看。

    为什么需要这个脚本

      Agent 循环（loop.py）要等批次 4，而且依赖支持 function calling 的模型。
      但"闸门到底拦不拦得住"这件事，**不需要模型就能验证** —— 因为闸门是
      纯服务端逻辑，模型再聪明也绕不过去。

      这个脚本按"一个理想的 Agent 会走的顺序"依次调用工具，
      再故意做几次越权尝试，把每一步的实际返回打出来。

      跑完之后你会看到：合法的路径走得通，越权的路径全部被拒，
      而且拒绝信息明确说了"不要重试"。

    用法：
        python scripts/agent_demo.py            # 用临时库，不污染 data/
        python scripts/agent_demo.py --real     # 用 data/chat.db（会留下演示单据）
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from my_tool.core.agent import gate, seed                      # noqa: E402
from my_tool.core.agent.registry import ToolContext            # noqa: E402
from my_tool.core.agent.toolset import build_registry          # noqa: E402
from my_tool.core.config import Settings                       # noqa: E402
from my_tool.store import db, order_repo                       # noqa: E402


def hr(title: str) -> None:
    print(f"\n{'─' * 76}\n{title}\n{'─' * 76}")


def show(label: str, res, *, keys: tuple[str, ...] = ()) -> None:
    """打印工具调用结果。只挑关键字段，原始 JSON 太吵。"""
    if res.ok:
        data = res.data if isinstance(res.data, dict) else {}
        detail = "  ".join(f"{k}={data.get(k)}" for k in keys if k in data)
        print(f"  ✓ {label}  {detail}")
        if res.next_allowed:
            print(f"     → 下一步只允许：{'、'.join(res.next_allowed)}")
    else:
        print(f"  ✗ {label}  [{res.error_code}] {res.error_message}")
        if res.next_allowed:
            print(f"     → 下一步只允许：{'、'.join(res.next_allowed)}")


def build(tmp: Path | None) -> tuple[Settings, object]:
    base = Settings.from_env()
    if tmp is None:
        s = base
    else:
        s = dataclasses.replace(
            base, chat_db_path=tmp / "demo.db", index_dir=tmp / "index",
            upload_dir=tmp / "up", llm_provider="mock", gate_secret="demo-gate-secret")
    db.init_db(s.chat_db_path)
    conn = db.connect(s.chat_db_path)
    try:
        seed.seed_params(conn)
        seed.seed_employees(conn)
    finally:
        conn.close()
    return s, build_registry()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agent 工具链演示")
    ap.add_argument("--real", action="store_true", help="用 data/chat.db")
    args = ap.parse_args(argv)

    import tempfile
    tmp = None if args.real else Path(tempfile.mkdtemp(prefix="agent-demo-"))
    s, reg = build(tmp)

    ctx = ToolContext(settings=s, user={"id": "u-demo", "role": "admin",
                                        "display_name": "演示管理员"},
                      session_id="demo-session")
    items = [{"field": "fixed_salary", "old_value": "18500", "new_value": "19000",
              "effective_date": "2026-11-01"}]
    materials = ["approval_form", "adjust_basis"]

    print(f"工具集（{len(reg.names())} 个）：{'、'.join(reg.names())}")
    print(f"数据库：{s.chat_db_path}")

    # =====================================================================
    hr("场景 A · 合法路径：查档案 → 校验 → 建单 → 提交（缺人工确认）")
    show("get_employee(E1001)",
         reg.call("get_employee", {"employee_no": "E1001"}, ctx),
         keys=("employee_no", "display_name", "hire_date", "fixed_salary"))

    show("lookup_param(基数上下限/冻结期)",
         reg.call("lookup_param", {"keys": ["si_base_cap", "si_base_floor",
                                            "freeze_start_day"]}, ctx))

    chk = reg.call("check_rules", {
        "change_type": "salary_adjust", "employee_no": "E1001",
        "items": items, "materials": materials}, ctx)
    show("check_rules", chk, keys=("verdict", "level"))
    token = chk.data.get("validation_token", "")
    print(f"     凭证：{token[:46]}…（{chk.data.get('token_expires_in')} 秒有效）")
    print(f"     内容指纹：{chk.data.get('content_hash')}…")

    draft = reg.call("create_draft", {
        "validation_token": token, "change_type": "salary_adjust",
        "employee_no": "E1001", "items": items, "materials": materials,
        "idempotency_key": "demo-A"}, ctx)
    show("create_draft", draft, keys=("order_no", "status", "risk_level", "created"))
    draft_id = draft.data["draft_id"]

    show("submit_draft（不带 confirm_token）",
         reg.call("submit_draft", {"draft_id": draft_id}, ctx))

    # 人的确认：服务端用 issue_confirm 模拟"用户在界面上点了确认"
    conn = db.connect(s.chat_db_path)
    try:
        order = order_repo.get(conn, order_id=draft_id)
        chash = gate.order_hash(order["change_type"], order["employee_no"],
                                order["items"], order["materials"])
        ctoken, _ = gate.issue_confirm(s, draft_id=draft_id, content_hash=chash)
    finally:
        conn.close()
    show("submit_draft（带人工确认凭证）",
         reg.call("submit_draft", {"draft_id": draft_id, "confirm_token": ctoken}, ctx),
         keys=("order_no", "status"))

    # =====================================================================
    hr("场景 B · 绕过尝试 1：跳过校验，直接建单")
    show("create_draft（无凭证）",
         reg.call("create_draft", {
             "validation_token": "", "change_type": "salary_adjust",
             "employee_no": "E1001", "items": items, "materials": materials,
             "idempotency_key": "demo-B1"}, ctx))

    show("create_draft（伪造凭证）",
         reg.call("create_draft", {
             "validation_token": "eyJmYWtlIjoxfQ.ZmFrZXNpZw", "change_type": "salary_adjust",
             "employee_no": "E1001", "items": items, "materials": materials,
             "idempotency_key": "demo-B2"}, ctx))

    # =====================================================================
    hr("场景 C · 绕过尝试 2：校验 A、建 B（最隐蔽的一种）")
    # 换一个员工：否则会和场景 A 的单撞上"24 小时重复提交"，测不到想测的东西
    c_items = [{"field": "fixed_salary", "old_value": "26800", "new_value": "27500",
                "effective_date": "2026-11-01"}]
    chk2 = reg.call("check_rules", {
        "change_type": "salary_adjust", "employee_no": "E1002",
        "items": c_items, "materials": materials}, ctx)
    token2 = chk2.data.get("validation_token", "")
    print(f"  · 校验「26800 → 27500」：{chk2.data.get('verdict')} "
          f"{chk2.data.get('level')}，取得凭证 {'是' if token2 else '否'}")
    show("create_draft（改成 26800 → 40000）",
         reg.call("create_draft", {
             "validation_token": token2, "change_type": "salary_adjust",
             "employee_no": "E1002",
             "items": [{"field": "fixed_salary", "old_value": "26800",
                        "new_value": "40000", "effective_date": "2026-11-01"}],
             "materials": materials, "idempotency_key": "demo-C"}, ctx))

    # =====================================================================
    hr("场景 D · 制度阻断：降薪且没有员工书面确认")
    cut_items = [{"field": "fixed_salary", "old_value": "18500", "new_value": "15000",
                  "effective_date": "2026-11-01"}]
    show("check_rules", reg.call("check_rules", {
        "change_type": "salary_adjust", "employee_no": "E1001",
        "items": cut_items, "materials": materials}, ctx),
         keys=("verdict", "level"))
    print(f"     阻断项：{ctx.workspace['last_verdict']['blocking']}")
    show("create_draft（想硬建）",
         reg.call("create_draft", {
             "validation_token": "", "change_type": "salary_adjust",
             "employee_no": "E1001", "items": cut_items, "materials": materials,
             "idempotency_key": "demo-D"}, ctx))

    # =====================================================================
    hr("场景 E · 需要人审：L4（金额差异 > 5000）")
    big = [{"field": "fixed_salary", "old_value": "18500", "new_value": "26000",
            "effective_date": "2026-11-01"}]
    res = reg.call("check_rules", {
        "change_type": "salary_adjust", "employee_no": "E1001",
        "items": big, "materials": materials}, ctx)
    show("check_rules", res, keys=("verdict", "level"))
    print(f"     是否签发凭证：{'是' if 'validation_token' in (res.data or {}) else '否'}"
          f"　（L3/L4 必须人审，Agent 拿不到凭证就建不了单）")

    # =====================================================================
    hr("场景 F · 正确的收尾：转人工")
    show("escalate_to_human", reg.call("escalate_to_human", {
        "reason_code": "needs_approval", "summary": "调薪幅度 40.5% 属 L4，需经理与合规双签",
        "context": {"clause": "第二十六条", "level": "L4"}}, ctx))

    # =====================================================================
    hr("轨迹（这就是「它为什么这么判断」的答案）")
    from my_tool.store import trace_repo
    conn = db.connect(s.chat_db_path)
    try:
        rows = trace_repo.list_traces(conn, "demo-session")
        total = order_repo.count(conn)
    finally:
        conn.close()
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        print(f"  {mark} {r['tool_name']:<20} {r['result'].get('summary', '')[:48]}")
    print(f"\n最终落库的变更单：{total} 张（只有场景 A 那张，且已提交）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
