"""`check_rules` —— Agent 与制度规则之间的唯一接口。

    **只算不写**。它不建单、不改单、不消费凭证，唯一副作用是签发一个 token
    和写一条轨迹。这个界限必须守死，否则"校验"会慢慢变成"顺便把单建了"，
    而那条路径没有人工确认。

    返回结构里最重要的两个字段：
      · `blocking[].final` —— True 表示制度第二十七条的"不得人工放行"，
        这一位会一路传到 create_draft 再被检查一次（双保险）；
      · `validation_token` —— 只在 verdict=pass 时出现，绑定本次内容哈希。

    `next_allowed` 按 verdict 分叉，这是"顺序约束"第 2 层的落点：
      pass        → ["create_draft", "escalate_to_human"]
      need_review → ["escalate_to_human"]        （L3/L4 必须人审，Agent 不能自己核准）
      block       → ["escalate_to_human", "ask_clarification"]
"""

from __future__ import annotations

from typing import Any

from ....store import db
from .. import gate, rules
from ..registry import ToolContext, ToolResult, ToolSpec


def _check_rules(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    settings = ctx.settings
    change_type = str(args.get("change_type") or "")
    employee_no = str(args.get("employee_no") or "").strip()
    items = args.get("items") or []
    materials = [str(m) for m in (args.get("materials") or [])]
    is_urgent = bool(args.get("is_urgent"))

    # 工号缺省取当前用户 —— 员工给自己改银行卡时不必再报工号
    if not employee_no:
        employee_no = str(ctx.workspace.get("employee", {}).get("employee_no") or "")
    if not employee_no:
        return ToolResult(
            ok=False, error_code="AGT_007",
            error_message="缺少 employee_no；请先调用 get_employee 确认在职员工")

    if not isinstance(items, list) or not items:
        return ToolResult(ok=False, error_code="AGT_007",
                          error_message="items 不能为空：至少要有一个明细项")

    conn = db.connect(settings.chat_db_path)
    try:
        emp = conn.execute("SELECT region_code, legal_entity_id FROM employees "
                           "WHERE employee_no = ?", (employee_no,)).fetchone()
        region = str(emp["region_code"] or "") if emp else ""
        entity = str(ctx.settings.legal_entity_id or "")
        verdict = rules.check(
            conn, change_type=change_type, employee_no=employee_no, items=items,
            materials=materials, is_urgent=is_urgent, region_code=region,
            legal_entity_id=entity)
    finally:
        conn.close()

    payload = verdict.as_dict()
    payload["employee_no"] = employee_no
    payload["change_type"] = change_type

    if verdict.verdict == "pass":
        # 内容哈希在签发时算一次、在 create_draft 时再算一次。
        # 两次之间只要 items 有任何改动，哈希就对不上 —— 这是防"校验 A 建 B"的关键
        chash = gate.order_hash(change_type, employee_no, items, materials)
        token, ttl = gate.issue_validation(settings, content_hash=chash,
                                           level=verdict.level)
        payload["validation_token"] = token
        payload["token_expires_in"] = ttl
        # 把哈希也回给模型：不是让它算，而是让它知道"改了内容凭证就作废"
        payload["content_hash"] = chash[:16]
        next_allowed = ["create_draft", "escalate_to_human"]
        summary = f"pass {verdict.level}"
    elif verdict.verdict == "need_review":
        # 不签 token：L3/L4 必须人审，Agent 拿不到凭证就建不了单
        next_allowed = ["escalate_to_human"]
        summary = f"need_review {verdict.level}"
    else:
        next_allowed = ["escalate_to_human", "ask_clarification"]
        summary = f"block {len(verdict.blocking)} 项"

    ctx.workspace["last_verdict"] = payload
    return ToolResult(ok=True, data=payload, next_allowed=next_allowed,
                      summary=summary)


def register(registry: Any) -> None:
    registry.register(ToolSpec(
        name="check_rules",
        description=(
            "对一份变更草稿逐条跑制度核对规则，返回阻断项、风险等级、所需材料，"
            "以及通过时签发的一次性校验凭证。这是建单前的强制步骤："
            "不调用本工具、或本工具判定为阻断或需复核，create_draft 一律会被服务端拒绝。"
            "本工具不写任何业务数据，可以反复调用；每次调用都会记审计。"
            "注意：改动了明细项的任何字段后必须重新调用，旧凭证会失效。"),
        parameters={
            "type": "object",
            "properties": {
                "change_type": {"type": "string", "enum": list(rules.CHANGE_TYPES)},
                "employee_no": {"type": "string",
                                "description": "工号；不传则用上次 get_employee 查到的员工"},
                "items": {
                    "type": "array",
                    "minItems": 1, "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string",
                                      "description": "字段名，如 bank_account / fixed_salary / si_base"},
                            "old_value": {"type": "string"},
                            "new_value": {"type": "string"},
                            "effective_date": {"type": "string",
                                               "description": "生效日 YYYY-MM-DD"},
                        },
                        "required": ["field", "new_value"],
                        "additionalProperties": False,
                    },
                    "description": "明细项：核对与审计的最小判定单位（第三条）",
                },
                "materials": {
                    "type": "array", "items": {"type": "string"},
                    "description": "已上传的材料类型码，见制度第三十六条",
                },
                "is_urgent": {"type": "boolean",
                              "description": "是否走第三十三条紧急通道"},
            },
            "required": ["change_type", "items"],
            "additionalProperties": False,
        },
        handler=_check_rules,
        # 静态兜底：handler 会按 verdict 返回更精确的列表，这里只在异常路径生效
        next_allowed=["escalate_to_human"],
    ))
