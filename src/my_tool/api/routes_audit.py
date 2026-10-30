"""对话审计页面：列表、会话回放、标记。

    一个刻意的设计：**自动预筛不落库**。
    列表页对当前页的每一行现算风险提示（R1~R4），人工标记才写 message_audits。
    两者混在一起的话，"确认违规 3 条"这个数字就再也说不清了。
"""

import json
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response

from ..core.audit_rules import assess
from ..store import audit_repo, db
from . import auth
from .errors import AUD_NOT_FOUND, ApiError
from .templating import render

router = APIRouter()

# =============================================================================
# 【模块 28 · 审计页面】目标文件：src/my_tool/api/routes_audit.py
# =============================================================================

PAGE_SIZE = 20
# risk=1 时在内存里过滤，最多扫这么多行。再多的数据量该走离线任务，
# 不该让一个页面请求背包袱。
RISK_SCAN_LIMIT = 200

STATUS_LABEL = {
    "answered": "已作答",
    "partial": "有疑点",
    "refused": "拒答",
    "error": "生成失败",
}


def _parse_citations(raw: str) -> list[dict]:
    try:
        data = json.loads(raw or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def _decorate(row: dict) -> dict:
    """给一行补上"人能看"的派生字段：问题、状态中文、风险提示、标记。"""
    row = dict(row)
    cites = _parse_citations(row.get("citations", "[]"))
    row["citations_list"] = cites
    row["citation_count"] = len(cites)
    row["status_label"] = STATUS_LABEL.get(row.get("answer_status", ""), row.get("answer_status", ""))
    row["risks"] = assess(row.get("content", ""), cites, row.get("answer_status", ""))
    row["risk_text"] = "；".join(r["label"] for r in row["risks"])
    row["answer_preview"] = (row.get("content") or "").replace("\n", " ")[:120]
    row["question_preview"] = (row.get("question") or "（无对应提问）").replace("\n", " ")[:80]
    return row


@router.get("/admin/audit")
def audit_list(request: Request,
               user_id: str = "", date_from: str = "", date_to: str = "",
               status: str = "", keyword: str = "", risk: str = "", page: int = 1,
               user: dict = Depends(auth.require_admin)) -> Response:
    """
    筛选 + 分页。

    默认时间范围留空（看全部）。日期输入用 YYYY-MM-DD，服务端拼成当天的起止 ——
    让用户去记 ISO 时间戳是不合理的。
    """
    ctx = request.app.state.ctx
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        if risk == "1":
            # 规则在 Python 里算，SQL 无法预先过滤：先多取一批再筛。
            rows, _ = audit_repo.list_answers(
                conn, user_id=user_id, date_from=date_from, date_to=date_to,
                status=status, keyword=keyword, page=1, page_size=RISK_SCAN_LIMIT)
            decorated = [r for r in (_decorate(x) for x in rows) if r["risks"]]
            total = len(decorated)
            start = max(0, (max(1, page) - 1) * PAGE_SIZE)
            items = decorated[start:start + PAGE_SIZE]
            truncated = len(rows) >= RISK_SCAN_LIMIT
        else:
            rows, total = audit_repo.list_answers(
                conn, user_id=user_id, date_from=date_from, date_to=date_to,
                status=status, keyword=keyword, page=page, page_size=PAGE_SIZE)
            items = [_decorate(r) for r in rows]
            truncated = False

        flags = audit_repo.latest_flags(conn, [int(r["id"]) for r in items])
        employees = audit_repo.list_employees(conn)
    finally:
        conn.close()

    for r in items:
        r["flag"] = flags.get(int(r["id"]))

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    query = {"user_id": user_id, "date_from": date_from, "date_to": date_to,
             "status": status, "keyword": keyword, "risk": risk}

    def _page_url(target: int) -> str:
        return "/admin/audit?" + urlencode({**query, "page": target})

    return render(request, "audit_list.html", {
        "items": items,
        "total": total,
        "page": max(1, page),
        "pages": pages,
        "page_url": _page_url,
        "filters": {"user_id": user_id, "date_from": date_from, "date_to": date_to,
                    "status": status, "keyword": keyword, "risk": risk},
        "employees": employees,
        "status_label": STATUS_LABEL,
        "truncated": truncated,
        "scan_limit": RISK_SCAN_LIMIT,
        # 默认时间范围给个提示：最近 7 天是审计最常看的口径
        "default_from": (datetime.now().astimezone() - timedelta(days=7)).date().isoformat(),
    })


@router.get("/admin/audit/{session_id}")
def audit_detail(session_id: str, request: Request,
                 user: dict = Depends(auth.require_admin)) -> Response:
    """会话回放。逐条显示问题、回答、引用原文、检索分数与消耗。"""
    ctx = request.app.state.ctx
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        sess = audit_repo.session_owner(conn, session_id)
        if sess is None:
            raise ApiError(AUD_NOT_FOUND, detail={"session_id": session_id})
        messages = audit_repo.get_session_messages(conn, session_id)
        flags = audit_repo.latest_flags(
            conn, [int(m["id"]) for m in messages if m["role"] == "assistant"])
        history = {
            int(m["id"]): audit_repo.flag_history(conn, int(m["id"]))
            for m in messages if m["role"] == "assistant" and int(m["id"]) in flags
        }
    finally:
        conn.close()

    items: list[dict] = []
    for m in messages:
        item = dict(m)
        cites = _parse_citations(item.get("citations", "[]"))
        item["citations_list"] = cites
        item["status_label"] = STATUS_LABEL.get(item.get("answer_status", ""),
                                                item.get("answer_status", ""))
        item["risks"] = assess(item.get("content", ""), cites, item.get("answer_status", "")) \
            if item["role"] == "assistant" else []
        item["flag"] = flags.get(int(item["id"]))
        item["flag_history"] = history.get(int(item["id"]), [])
        items.append(item)

    return render(request, "audit_detail.html", {
        "sess": sess,
        "items": items,
    })


@router.post("/admin/audit/messages/{message_id}/flag")
async def audit_flag(message_id: int, request: Request,
                     user: dict = Depends(auth.require_admin)) -> Response:
    """
    标记/确认/清除。每个动作都追加一条记录，之前的标记自动置为已撤销。

    redirect 回到原页面（next 参数），管理员标完不用重新筛选一遍。
    """
    ctx = request.app.state.ctx
    form = await request.form()
    auth.verify_csrf(request, str(form.get("_csrf", "")))

    flag = str(form.get("flag", ""))
    note = str(form.get("note", "")).strip()[:200]
    session_id = str(form.get("session_id", ""))
    next_url = str(form.get("next", "")) or ("/admin/audit/" + session_id if session_id else "/admin/audit")
    if not next_url.startswith("/"):
        next_url = "/admin/audit"

    if flag not in ("suspect", "confirmed", "cleared"):
        raise ApiError(AUD_NOT_FOUND, detail={"flag": flag})

    conn = db.connect(ctx.settings.chat_db_path)
    try:
        row = conn.execute(
            "SELECT id, session_id FROM chat_messages WHERE id = ? AND role = 'assistant'",
            (message_id,)).fetchone()
        if row is None:
            raise ApiError(AUD_NOT_FOUND, detail={"message_id": message_id})
        audit_repo.add_flag(
            conn, message_id=message_id, session_id=row["session_id"], flag=flag,
            note=note, auditor_id=user.get("id"),
        )
    finally:
        conn.close()

    label = {"suspect": "已标记疑似违规", "confirmed": "已确认违规", "cleared": "已清除标记"}[flag]
    sep = "&" if "?" in next_url else "?"
    return RedirectResponse(f"{next_url}{sep}ok={quote(label)}", status_code=303)
