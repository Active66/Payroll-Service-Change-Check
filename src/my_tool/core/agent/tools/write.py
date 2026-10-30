"""写工具：`create_draft` / `submit_draft` / `escalate_to_human` / `ask_clarification`。

    **这四个工具里，前两个是本设计的价值所在。**

    `create_draft` 做五道校验（签名 / 时效 / 判定 / 内容绑定 / 一次性），
    任何一道不过就拒绝，并且拒绝信息明确说"不要换参数重试"——
    因为模型的默认反应正是重试，重试就会把一次明确的失败变成十次无意义的循环。

    `submit_draft` 需要 confirm_token，而 confirm_token 只能由人在界面上点击产生。
    **模型没有任何工具能签发它**。这是整个体系里唯一一处"模型绝对做不到"的约束，
    因为提交进审批流不可撤销：答错一句话用户再问一次，提错一张单员工这个月工资就错了。
"""

from __future__ import annotations

import json
from typing import Any

from ....store import db, order_repo
from .. import gate
from ..registry import (E_BLOCKED, E_CONFIRM_REQUIRED, E_GATE_REQUIRED,
                        E_NOT_FOUND, ToolContext, ToolResult, ToolSpec)

# 拒绝时统一附上的话术。为什么写进返回体而不是只靠提示词：
# 提示词在几十轮之后会被稀释，而这句提示紧贴着失败结果，模型一定看得到。
_NO_RETRY = "不要修改参数重试，也不要寻找其他工具绕过；请向用户说明原因。"


def _content_hash_of(order: dict[str, Any]) -> str:
    """单的内容指纹。与 check_rules 用同一个函数，才能真正做到"锁定同一份内容"。"""
    return gate.order_hash(order.get("change_type") or "", order.get("employee_no") or "",
                           order.get("items") or [], order.get("materials") or [])


def _create_draft(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    settings = ctx.settings
    change_type = str(args.get("change_type") or "")
    employee_no = str(args.get("employee_no") or "").strip()
    if not employee_no:
        employee_no = str(ctx.workspace.get("employee", {}).get("employee_no") or "")
    items = args.get("items") or []
    materials = [str(m) for m in (args.get("materials") or [])]
    token = str(args.get("validation_token") or "")
    idem = str(args.get("idempotency_key") or "").strip()

    if not employee_no:
        return ToolResult(ok=False, error_code="AGT_007",
                          error_message="缺少 employee_no")
    if not idem:
        return ToolResult(ok=False, error_code="AGT_007",
                          error_message="缺少 idempotency_key；" + _NO_RETRY)

    chash = gate.order_hash(change_type, employee_no, items, materials)

    # ⓪ 幂等命中先行 —— 见 order_repo.get_by_idempotency 的说明。
    # 这是唯一一个"不必有凭证也能返回成功"的路径，因为它返回的是**已经建好的**
    # 那张单，没有产生任何新写入。
    conn = db.connect(settings.chat_db_path)
    try:
        existing = order_repo.get_by_idempotency(conn, idem)
    finally:
        conn.close()
    if existing is not None:
        ctx.workspace["draft"] = existing
        return ToolResult(
            ok=True,
            data={"draft_id": existing["id"], "order_no": existing["order_no"],
                  "status": existing["status"], "risk_level": existing.get("risk_level"),
                  "employee_no": existing["employee_no"],
                  "change_type": existing["change_type"], "items": existing["items"],
                  "created": False,
                  "note": "（幂等命中：该请求已建过单，返回已有草稿）"},
            next_allowed=["submit_draft", "escalate_to_human"],
            summary=f"{existing['order_no']} 幂等命中",
        )

    # ① final 阻断：连验签都不必做
    last = ctx.workspace.get("last_verdict") or {}
    blocking = last.get("blocking") or []
    has_final = any(b.get("final") for b in blocking)
    if has_final:
        codes = "、".join(b.get("code", "") for b in blocking if b.get("final"))
        return ToolResult(
            ok=False, error_code=E_BLOCKED.code,
            error_message=f"{E_BLOCKED.message}（{codes}）。制度第二十七条规定此类阻断"
                          f"不得人工放行。{_NO_RETRY}")

    info, err = gate.verify_validation(settings, token, content_hash=chash,
                                       blocking_final=has_final)
    if err is not None:
        hint = {
            E_GATE_REQUIRED.code: "请先调用 check_rules 取得凭证。",
            "AGT_002": "凭证已过期，请重新调用 check_rules。",
            "AGT_003": "提交的内容与校验时的内容不一致（明细项或材料被改动过），"
                       "请重新调用 check_rules。",
            "AGT_004": "该校验凭证已被用于建单，不能重复使用。",
        }.get(err.code, "")
        return ToolResult(ok=False, error_code=err.code,
                          error_message=f"{err.message} {hint} {_NO_RETRY}")

    # 消费凭证（一次性）。放在建单之前：万一建单成功但消费失败，
    # 下次会因"凭证已用"被拒，得到的是"少建单"而不是"多建单"——
    # 这两个错误的代价不对称，宁可少建。
    ok, cerr = gate.consume(settings, info, subject_id=chash,
                            consumed_by=ctx.user_id)
    if not ok:
        return ToolResult(ok=False, error_code=E_GATE_REQUIRED.code,
                          error_message=f"{cerr.message if cerr else ''} {_NO_RETRY}")

    conn = db.connect(settings.chat_db_path)
    try:
        order, created = order_repo.create_draft(
            conn, employee_no=employee_no, change_type=change_type, items=items,
            materials=materials, risk_level=last.get("level") or info.level,
            blocking=blocking, validation_jti=info.jti, idempotency_key=idem,
            created_by=ctx.user_id)
    finally:
        conn.close()

    ctx.workspace["draft"] = order
    note = "" if created else "（幂等命中：该请求已建过单，返回已有草稿）"
    return ToolResult(
        ok=True,
        data={
            "draft_id": order["id"],
            "order_no": order["order_no"],
            "status": order["status"],
            "risk_level": order.get("risk_level"),
            "employee_no": order["employee_no"],
            "change_type": order["change_type"],
            "items": order["items"],
            "created": created,
            "note": note,
            "next_step": "向用户复述将要提交的内容并请求确认；"
                         "提交需要用户在界面上点击确认，你无法自行提交。",
        },
        next_allowed=["submit_draft", "escalate_to_human"],
        summary=f"{order['order_no']} {'新建' if created else '幂等'}",
    )


def _submit_draft(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    settings = ctx.settings
    draft_id = str(args.get("draft_id") or "").strip()
    token = str(args.get("confirm_token") or "").strip()

    if not draft_id:
        return ToolResult(ok=False, error_code="AGT_007", error_message="缺少 draft_id")

    conn = db.connect(settings.chat_db_path)
    try:
        order = order_repo.get(conn, order_id=draft_id)
        if order is None:
            return ToolResult(ok=False, error_code=E_NOT_FOUND.code,
                              error_message=f"找不到草稿 {draft_id}")
        if order["status"] != "draft":
            return ToolResult(
                ok=False, error_code=E_BLOCKED.code,
                error_message=f"该单当前状态为 {order['status']}，只有 draft 可以提交")

        if not token:
            # 这是最常见的路径，也是设计意图：模型到这里必须停下来请人确认
            return ToolResult(
                ok=False, error_code=E_CONFIRM_REQUIRED.code,
                error_message="提交需要用户在界面上点击确认。请先向用户完整复述"
                              "变更内容、风险等级与依据条款，然后请其确认。")

        chash = _content_hash_of(order)
        info, err = gate.verify_confirm(settings, token, draft_id=draft_id,
                                        content_hash=chash)
        if err is not None:
            return ToolResult(ok=False, error_code=err.code,
                              error_message=f"{err.message} {_NO_RETRY}")

        changed = order_repo.submit(conn, draft_id, by=ctx.user_id)
        order = order_repo.get(conn, order_id=draft_id)
    finally:
        conn.close()

    if not changed:
        return ToolResult(ok=False, error_code=E_BLOCKED.code,
                          error_message="该单已被提交过（并发或重复点击）")
    return ToolResult(
        ok=True,
        data={"draft_id": draft_id, "order_no": order["order_no"],
              "status": order["status"], "submitted_at": order["submitted_at"]},
        summary=f"{order['order_no']} 已提交",
    )


def _escalate_to_human(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    转人工。**这是正常收尾，不是异常**。

    实现上刻意做得很轻：只记工作区 + 返回一段给用户看的话。
    真正的队列入队（第二十八条）需要 change_orders 表里的一张"待人工"单，
    而那时候还没有单 —— 所以本期只做"标记 + 提示"，不假装入队。
    """
    reason = str(args.get("reason_code") or "ambiguous")
    summary = str(args.get("summary") or "").strip()[:500]
    context = args.get("context") or {}

    ctx.workspace["escalated"] = {"reason_code": reason, "summary": summary,
                                  "context": context}
    return ToolResult(
        ok=True,
        data={
            "escalated": True,
            "reason_code": reason,
            "summary": summary,
            "message": "已记录转人工。请在回答里告诉用户：这个问题将转由 SSC 人工核对，"
                       "并说明具体卡在哪（依据条款或缺失材料）。",
        },
        next_allowed=[],
        summary=f"escalate {reason}",
    )


def _ask_clarification(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    澄清。返回结构化问题，由上层决定怎么呈现（本期是转成一段文本问用户）。

    `next_allowed` 留空：澄清之后用户会补充信息，下一轮从头开始，
    不应该限制它只能调某几个工具。
    """
    questions = args.get("questions") or []
    cleaned = [{"field": str(q.get("field") or ""),
                "question": str(q.get("question") or ""),
                "options": [str(o) for o in (q.get("options") or [])]}
               for q in questions if isinstance(q, dict)]
    ctx.workspace["clarification"] = cleaned
    return ToolResult(
        ok=True,
        data={"questions": cleaned,
              "message": "请把这些问句直接问用户，不要自己假设答案。"},
        summary=f"问 {len(cleaned)} 个问题",
    )


def register(registry: Any) -> None:
    registry.register(ToolSpec(
        name="create_draft",
        description=(
            "创建变更单草稿。必须携带 check_rules 签发的 validation_token，"
            "且内容必须与签发时完全一致；服务端会重新校验，任一不符即拒绝。"
            "缺 token、过期、内容被改、存在 final 阻断项，都会失败。"
            "被拒绝时不要换参数重试，应向用户说明原因。"),
        parameters={
            "type": "object",
            "properties": {
                "validation_token": {
                    "type": "string",
                    "description": "check_rules 返回的凭证。**必填**，没有它建单一定失败"},
                "change_type": {"type": "string"},
                "employee_no": {"type": "string"},
                "items": {"type": "array", "items": {"type": "object"},
                          "minItems": 1, "maxItems": 20},
                "materials": {"type": "array", "items": {"type": "string"}},
                "idempotency_key": {
                    "type": "string",
                    "description": "同一意图的唯一键；重试时传同一个值可避免建出两张单"},
                "note": {"type": "string", "maxLength": 500},
            },
            # validation_token 刻意**不放进 required**：放进去了，漏传会先被参数校验
            # 拦成 AGT_007（"缺少必填参数"），而模型真正需要知道的是
            # AGT_001（"缺凭证，去调 check_rules"）。同一个事实用两个错误码表达，
            # 模型就要写两套处理逻辑，而它只有一套提示词 —— 所以统一成 AGT_001。
            "required": ["change_type", "employee_no", "items", "idempotency_key"],
            "additionalProperties": False,
        },
        handler=_create_draft, writes=True,
        # 建单是敏感动作：循环在真正执行它之前强制中断，把草稿发给员工确认，
        # 确认后才落库。Agent 不能自己把单建出来（见 loop._intercept_confirm）。
        requires_human_confirm=True,
        next_allowed=["submit_draft", "escalate_to_human"],
    ))

    registry.register(ToolSpec(
        name="submit_draft",
        description=(
            "把草稿提交进审批流。这是不可撤销的对外动作，必须携带人工确认凭证 "
            "confirm_token。confirm_token 只能由用户在界面上点击产生，你无法自己获取；"
            "没有它请先向用户完整复述变更内容与依据条款，并请求确认。"),
        parameters={
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
                "confirm_token": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["draft_id"],
            "additionalProperties": False,
        },
        handler=_submit_draft, writes=True,
    ))

    registry.register(ToolSpec(
        name="escalate_to_human",
        description=(
            "把当前事项转人工核对队列。出现以下任一情况必须调用：制度里查不到依据；"
            "参数缺失；存在 final 阻断但用户坚持；风险等级需要人工审批；"
            "你不确定自己理解对了。这是正确的收尾方式，不是失败。"),
        parameters={
            "type": "object",
            "properties": {
                "reason_code": {"type": "string", "enum": [
                    "no_policy_basis", "param_missing", "blocked_final",
                    "needs_approval", "ambiguous", "out_of_scope"]},
                "summary": {"type": "string", "maxLength": 500,
                            "description": "一句话说明卡在哪，人工接手要看的第一句话"},
                "context": {"type": "object",
                            "description": "已查到的条款号、判定结果"},
            },
            "required": ["reason_code", "summary"],
            "additionalProperties": False,
        },
        handler=_escalate_to_human,
    ))

    registry.register(ToolSpec(
        name="ask_clarification",
        description=(
            "信息不足时向用户提问，而不是猜测。缺必填项（生效日、变更类型、新值）、"
            "同一句话有两种解释、或需要用户确认将要执行的动作时使用。一次最多 3 个问题。"),
        parameters={
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array", "minItems": 1, "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "question": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["field", "question"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
        handler=_ask_clarification,
    ))


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
