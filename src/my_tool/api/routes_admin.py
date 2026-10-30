"""后台概览页。

    这个页面存在的意义不是"好看"，而是让管理员一眼回答三个问题：
      ① 索引是不是好的（坏了的话后面所有功能都白搭）；
      ② 今天有多少人问、有多少答不出来；
      ③ 有没有待处理的疑似违规。
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from ..store import db, org_repo, policy_repo, stats_repo, user_repo
from . import auth
from .templating import render

router = APIRouter()

# =============================================================================
# 【模块 22 · 后台页面】目标文件：src/my_tool/api/routes_admin.py
#   页面路由只做三件事：取数、渲染、把异常交出去。业务判断一律在 core/ 里。
# =============================================================================


def _index_state(request: Request) -> dict[str, Any]:
    """
    索引状态。三种情况都要能显示，因为它们的处置动作完全不同：
      ready    —— 正常
      error    —— 启动时加载失败（多半是没跑 build）
      building —— 后台正在重建（此时 /chat 仍用旧索引）
    """
    ctx = request.app.state.ctx
    manifest = ctx.manifest or {}
    rebuild = getattr(ctx, "rebuild_state", None)
    state = "ready" if ctx.orchestrator is not None and not ctx.index_error else "error"
    if rebuild is not None and rebuild.get("status") == "building":
        state = "building"
    return {
        "state": state,
        "error": ctx.index_error,
        "chunk_count": manifest.get("chunk_count", 0),
        "doc_title": manifest.get("doc_title", ""),
        "doc_version": manifest.get("doc_version", ""),
        "embedding_model": manifest.get("embedding_model", ""),
        "built_at": manifest.get("built_at", ""),
        "rebuild": rebuild or {},
    }


@router.get("/admin")
def dashboard(request: Request, user: dict = Depends(auth.require_admin)) -> Response:
    """概览。所有数字都来自库，不缓存 —— 内部后台的查询量根本不值得做缓存。"""
    ctx = request.app.state.ctx
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        doc_count = stats_repo.count_documents(conn)
        counts = stats_repo.today_counts(conn)
        pending = stats_repo.pending_audits(conn)
        users_total = user_repo.count_users(conn)
        recent = stats_repo.recent_sessions(conn, limit=5)
        # 部门分布默认看最近 30 天：全量口径会被"上线头一个月的测试数据"长期主导，
        # 越用越看不出问题
        since_30d = (datetime.now().astimezone() - timedelta(days=30)).isoformat(
            timespec="seconds")
        by_dept = org_repo.usage_by_department(conn, since=since_30d)
        totals = org_repo.total_stats(conn, since=since_30d)
        pending_docs = policy_repo.list_pending(conn, datetime.now().strftime("%Y-%m-%d"))
    finally:
        conn.close()

    by_dept = [d for d in by_dept if d["questions"]]
    dept_sum = sum(int(d["questions"] or 0) for d in by_dept)

    return render(request, "dashboard.html", {
        "doc_count": doc_count,
        "counts": counts,
        "refusal_pct": round(stats_repo.refusal_rate(counts) * 100),
        "pending_audits": pending,
        "users_total": users_total,
        "recent": recent,
        "index": _index_state(request),
        "auth_enabled": ctx.settings.auth_required,
        "by_dept": by_dept,
        "dept_sum": dept_sum,
        "dept_total": int(totals["answers"] or 0),
        "pending_docs": pending_docs,
    })
