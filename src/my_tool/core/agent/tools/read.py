"""读工具：`lookup_policy` / `lookup_param` / `get_employee`。

    三个工具的共同点：**无副作用、可重入、可并发**。所以它们之间不做顺序约束
    （`next_allowed` 留空即为不限制），模型可以按需自由组合。

    但"无副作用"不等于"无边界"：
      · `lookup_policy` 只能检索，不能读原始文件路径（配置泄露）；
      · `lookup_param` 缺参数必须返回 missing，**不许给默认值**；
      · `get_employee` 出口统一脱敏，且只能查自己或（管理员）查他人。
"""

from __future__ import annotations

from typing import Any

from ....store import db, employee_repo, param_repo
from ....utils.console import log
from ..registry import E_NOT_FOUND, ToolContext, ToolResult, ToolSpec

# =============================================================================
# 【模块 33 · 读工具】目标文件：src/my_tool/core/agent/tools/read.py
# =============================================================================

PARAM_KEYS = [
    "si_base_cap", "si_base_floor", "hf_ratio_min", "hf_ratio_max",
    "submit_deadline_day", "freeze_start_day", "freeze_end_day",
    "raise_cap_pct", "cut_cap_pct", "backpay_months_limit",
    "diff_l1_max", "diff_l2_max", "diff_l3_max",
    "bank_change_quota_30d", "verify_pay_min", "verify_pay_max",
]


# ---------------------------------------------------------------------------
# lookup_policy
# ---------------------------------------------------------------------------

def _lookup_policy(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    检索制度条款。

    直接复用 `HybridRetriever`，不重写检索逻辑 —— 阶段 1-4 调了很久的
    切片与融合策略没有理由在 Agent 里再来一遍。

    与固定流程的一个差别：**这里不设拒答闸门**。固定流程里检索不到就直接拒答，
    但 Agent 需要知道"有没有检索到"，才能自己决定是换 query 再查、
    还是转人工。把判定权交给它，把事实如实返回。
    """
    retriever = ctx.retriever
    if retriever is None:
        return ToolResult(ok=False, error_code=E_NOT_FOUND.code,
                          error_message="索引未就绪，无法检索制度")

    query = str(args.get("query") or "").strip()
    top_k = int(args.get("top_k") or 3)
    hits, debug = retriever.retrieve(query, top_k=top_k)

    items = []
    for i, h in enumerate(hits, start=1):
        items.append({
            "index": i,
            "chunk_id": h.chunk.chunk_id,
            "doc_id": h.chunk.doc_id,
            "doc_title": h.chunk.doc_title,
            "version": h.chunk.version,
            "clause_no": h.chunk.clause_range or h.chunk.clause_no,
            "heading_path": h.chunk.heading_path,
            "score": round(float(h.fused_score), 4),
            # 给全文而不是 120 字片段：Agent 要靠上下文判断"这条是否适用"，
            # 片段截断会让它在"但书"上判断错（第九条、第八条都有但书）
            "content": h.chunk.content,
        })

    ctx.workspace.setdefault("policy_hits", []).extend(items)
    return ToolResult(
        ok=True,
        data={"hits": items, "refused": not items, "query_used": query,
              "debug": {"bm25": debug.get("bm25", [])[:3],
                        "vector": debug.get("vector", [])[:3]}},
        summary=f"{len(items)} 条条款",
    )


# ---------------------------------------------------------------------------
# lookup_param
# ---------------------------------------------------------------------------

def _lookup_param(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    取结构化参数。

    `missing` 是**给模型看的行动信号**：拿到 missing 它必须转人工或说明
    "这个参数本系统没有"，而不是用一个合理猜测继续算。提示词里明确要求了这点，
    但要靠返回值把话说死（"missing" 比空数组更能触发正确行为）。
    """
    keys = [str(k) for k in (args.get("keys") or [])]
    bad = [k for k in keys if k not in PARAM_KEYS]
    if bad:
        return ToolResult(
            ok=False, error_code="AGT_007",
            error_message=f"不支持的参数名：{'、'.join(bad)}；可用：{'、'.join(PARAM_KEYS)}")

    as_of = args.get("as_of") or None
    # 地区：优先显式传入（"上海社保基数"这类纯参数查询不需要员工档案），
    # 其次用已查员工（get_employee）的参保地，最后才是空（查全国默认）
    region = str(args.get("region_code") or "").strip() \
        or str(ctx.workspace.get("region_code") or "")
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        found, missing = param_repo.get_params(conn, keys, region_code=region,
                                               as_of=as_of)
    finally:
        conn.close()

    ctx.workspace.setdefault("params", {}).update({p["key"]: p for p in found})
    return ToolResult(
        ok=True,
        data={"items": found, "missing": missing, "region_code": region},
        summary=f"{len(found)} 个参数" + (f"，缺 {len(missing)} 个" if missing else ""),
    )


# ---------------------------------------------------------------------------
# get_employee
# ---------------------------------------------------------------------------

def _get_employee(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    读员工档案。

    权限判断放在**工具内部**而不是提示词里。模型看到"只能查本人"的说明
    仍然会照查——因为用户可能说"帮我查一下张三的工资"，它觉得这是善意。
    这里直接拒绝，它就只能告诉用户"没权限查别人"。
    """
    employee_no = str(args.get("employee_no") or "").strip()
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        if not employee_no:
            # 不传工号 = 查本人。员工账号与档案的关联靠 user_id
            rec = employee_repo.get_by_user(conn, ctx.user_id) if ctx.user_id else None
            if rec is None:
                return ToolResult(
                    ok=False, error_code=E_NOT_FOUND.code,
                    error_message="当前账号没有关联的人事档案，请提供工号")
        else:
            rec = employee_repo.get(conn, employee_no)
            if rec is None:
                return ToolResult(
                    ok=False, error_code=E_NOT_FOUND.code,
                    error_message=f"找不到工号 {employee_no} 的人事档案")
            # 查他人：必须管理员，或查的就是自己
            if not ctx.is_admin and str(rec.get("user_id") or "") != ctx.user_id:
                return ToolResult(
                    ok=False, error_code="AGT_007",
                    error_message="只能查询本人档案；查询他人档案需要管理员权限")
    finally:
        conn.close()

    fields = args.get("fields")
    fields = [str(f) for f in fields] if isinstance(fields, list) else None
    masked = employee_repo.mask(rec)
    payload = employee_repo.pick(masked, fields)

    # region_code / legal_entity_id 是**后续操作的上下文**，不是"展示字段"。
    # 模型为了省 token 传 fields 时往往不列它们，但 lookup_param 靠 region_code
    # 找参数、check_rules 靠 legal_entity_id 判法人 —— 裁掉就等于让后续工具瞎跑。
    # 所以这里强制补齐，不管 fields 里有没有。
    payload["region_code"] = str(rec.get("region_code") or "")
    payload["legal_entity_id"] = str(rec.get("legal_entity_id") or "")

    # 只暴露业务需要的字段。created_at/updated_at/内部列留在库里，
    # 上下文里每多一个字段，模型就多一个可以误用的东西
    allowed = {"employee_no", "display_name", "hire_date", "grade", "fixed_salary",
               "si_base", "hf_base", "region_code", "legal_entity_id", "status",
               "id_card", "bank_account", "phone"}
    payload = {k: v for k, v in payload.items() if k in allowed}

    ctx.workspace["employee"] = payload
    ctx.workspace["region_code"] = str(payload.get("region_code") or "")
    missing = [k for k in ("hire_date", "grade", "region_code", "legal_entity_id")
               if not payload.get(k)]
    return ToolResult(
        ok=True,
        data={"employee": payload, "missing_fields": missing},
        summary=f"{payload.get('employee_no')} {payload.get('display_name')}",
    )


# ---------------------------------------------------------------------------
# get_change_order
# ---------------------------------------------------------------------------

def _get_change_order(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """
    查已有变更单。

    两种用法要分清，混在一起会答错问题：
      · 按单号查 → 回答"这张单现在什么状态"
      · 按员工+类型查近期单 → 回答"有没有重复提交""补发跨了几个月"

    后者总是带时间窗（默认 24 小时，对应第六条的重复提交判定）。
    """
    from datetime import datetime, timedelta

    from ....store import order_repo

    order_no = str(args.get("order_no") or "").strip()
    employee_no = str(args.get("employee_no") or "").strip()
    change_type = args.get("change_type") or None
    within_hours = int(args.get("within_hours") or 24)

    conn = db.connect(ctx.settings.chat_db_path)
    try:
        if order_no:
            order = order_repo.get(conn, order_no=order_no)
            if order is None:
                return ToolResult(ok=False, error_code=E_NOT_FOUND.code,
                                  error_message=f"找不到单号 {order_no}")
            # 非管理员只能看自己的单
            if not ctx.is_admin:
                mine = employee_repo.get_by_user(conn, ctx.user_id)
                if mine is None or mine["employee_no"] != order["employee_no"]:
                    return ToolResult(ok=False, error_code="AGT_007",
                                      error_message="只能查看本人的变更单")
            return ToolResult(ok=True, data={"order": _order_brief(order)},
                              summary=order["order_no"])

        if not employee_no:
            employee_no = str(ctx.workspace.get("employee", {}).get("employee_no") or "")
        if not employee_no:
            return ToolResult(ok=False, error_code="AGT_007",
                              error_message="请提供 order_no，或先 get_employee 确定员工")

        since = (datetime.now() - timedelta(hours=max(1, within_hours))
                 ).isoformat(timespec="seconds")
        orders = order_repo.list_by_employee(
            conn, employee_no, change_type=change_type, since=since, limit=20)
    finally:
        conn.close()

    return ToolResult(
        ok=True,
        data={"orders": [_order_brief(o) for o in orders],
              "count": len(orders),
              "window_hours": within_hours,
              "employee_no": employee_no},
        summary=f"{len(orders)} 张单",
    )


def _order_brief(order: dict[str, Any]) -> dict[str, Any]:
    """单据摘要。不返回 items 全文：Agent 判断"是否重复"只需要字段名与新值。"""
    return {
        "order_no": order["order_no"],
        "employee_no": order["employee_no"],
        "change_type": order["change_type"],
        "status": order["status"],
        "risk_level": order.get("risk_level"),
        "items": [{"field": i.get("field"), "new_value": i.get("new_value"),
                   "effective_date": i.get("effective_date")}
                  for i in (order.get("items") or [])],
        "created_at": order.get("created_at"),
        "submitted_at": order.get("submitted_at"),
    }


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def register(registry: Any) -> None:
    registry.register(ToolSpec(
        name="lookup_policy",
        description=(
            "按自然语言检索制度条款原文，返回带条款号的片段。用于回答"
            "'制度怎么规定的'、'某情形是否允许'类问题。当问题涉及具体数值"
            "（金额、比例、基数、时限日期）时，先调本工具定位条款，"
            "再用 lookup_param 取数值，不要从条款文本里抄数字。"
            "不要用它查员工信息或已有单据。"),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "检索语句，用制度里的术语（如'冻结期'而非'不能改的时间'）"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 8,
                          "description": "返回条数，默认 3"},
                "as_of": {"type": "string",
                          "description": "按该日期生效的制度版本检索，YYYY-MM-DD"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_lookup_policy,
    ))

    registry.register(ToolSpec(
        name="lookup_param",
        description=(
            "读取制度参数表里的确定数值：社保/公积金基数上下限、缴存比例区间、"
            "提交时限、冻结期起止日、调薪幅度红线、补发上限、差异分级金额阈值。"
            "凡是要在回答里出现数字，必须来自本工具，禁止从条款文本摘抄或自行推算。"
            "返回 missing 表示该参数本系统没有，此时不得猜值，应转人工。"),
        parameters={
            "type": "object",
            "properties": {
                "keys": {
                    "type": "array",
                    "items": {"type": "string", "enum": PARAM_KEYS},
                    "description": "要取的参数名，可一次取多个",
                },
                "as_of": {"type": "string", "description": "按该日期的参数版本取，YYYY-MM-DD"},
                "region_code": {
                    "type": "string",
                    "description": "参保地区代码，如上海为 310000。查某地区的具体数值时必须传；"
                                   "不传则用已查到的员工参保地"},
            },
            "required": ["keys"],
            "additionalProperties": False,
        },
        handler=_lookup_param,
    ))

    registry.register(ToolSpec(
        name="get_employee",
        description=(
            "读取员工档案：入职日、职级、现固定薪资、社保基数、参保地、法人主体。"
            "建变更单前必须调用，用于判定生效日是否早于入职日、调薪是否超带宽。"
            "不传 employee_no 则查当前登录用户本人；只有管理员能查他人。"),
        parameters={
            "type": "object",
            "properties": {
                "employee_no": {"type": "string",
                                "description": "工号。不传则查当前登录用户本人"},
                "fields": {"type": "array", "items": {"type": "string"},
                           "description": "只取需要的字段，减少上下文占用"},
            },
            "additionalProperties": False,
        },
        handler=_get_employee,
    ))


    registry.register(ToolSpec(
        name="get_change_order",
        description=(
            "按单号查变更单，或按员工+变更类型查近期单据。用于判断'同一明细项 24 小时内"
            "重复提交'（第六条、第二十七条阻断项）以及'补发是否超过 3 个月'。"
            "不传 order_no 时按 employee_no + within_hours 查列表。"),
        parameters={
            "type": "object",
            "properties": {
                "order_no": {"type": "string"},
                "employee_no": {"type": "string"},
                "change_type": {"type": "string", "enum": [
                    "bank_account", "si_base", "hf_base", "salary_adjust",
                    "allowance", "address", "tax_special"]},
                "within_hours": {"type": "integer", "minimum": 1, "maximum": 720,
                                 "description": "时间窗（小时），默认 24"},
            },
            "additionalProperties": False,
        },
        handler=_get_change_order,
    ))


def log_registered(registry: Any) -> None:
    log(f"已注册 {len(registry.names())} 个 Agent 工具：{'、'.join(registry.names())}")
