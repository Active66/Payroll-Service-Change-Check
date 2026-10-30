"""提示词模板页面：编辑、保存（新版本）、回滚、预览。

    三个约束（都在页面上明说，避免管理员踩坑）：
      ① 保存 = 新建版本，不是覆盖。历史永远在，可回滚；
      ② user 模板必须保留 {context} 与 {question}，否则一问就 KeyError；
      ③ 引用格式由系统强制追加，模板里不用（也改不了）写角标规则。
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from urllib.parse import quote

from ..core.prompt import (BUILTIN_TEMPLATE, CITATION_RULES, DEFAULT_SYSTEM_PROMPT,
                           DEFAULT_USER_TEMPLATE, PromptTemplate, build_prompt,
                           validate_template)
from ..core.guard import run_guard
from ..store import db, prompt_repo
from ..utils.console import log
from . import auth
from .errors import PRM_NOT_FOUND, ApiError
from .templating import render

router = APIRouter()

# =============================================================================
# 【模块 27 · 提示词页面】目标文件：src/my_tool/api/routes_prompts.py
# =============================================================================

_PREVIEW_QUESTION = "冻结期内可以改银行账户吗？"


def _load_versions(request: Request) -> tuple[dict | None, PromptTemplate, list[dict]]:
    """一次把"当前生效 / 生效模板对象 / 历史列表"都取出来，页面渲染要三样都用。"""
    ctx = request.app.state.ctx
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        active = prompt_repo.get_active(conn)
        versions = prompt_repo.list_versions(conn, limit=30)
    finally:
        conn.close()

    provider = ctx.prompt_provider
    tpl = provider.active() if provider else BUILTIN_TEMPLATE
    return active, tpl, versions


@router.get("/admin/prompts")
def prompts_page(request: Request, user: dict = Depends(auth.require_admin)) -> Response:
    active, tpl, versions = _load_versions(request)
    return render(request, "prompts.html", {
        "active": active,
        "tpl": tpl,
        "versions": versions,
        "citation_rules": CITATION_RULES.strip(),
        "system_prompt": (active or {}).get("system_prompt") or tpl.system_prompt,
        "user_template": (active or {}).get("user_template") or tpl.user_template,
        "defaults": {"system": DEFAULT_SYSTEM_PROMPT, "user": DEFAULT_USER_TEMPLATE},
    })


@router.post("/admin/prompts")
async def prompts_save(request: Request, user: dict = Depends(auth.require_admin)) -> Response:
    """
    保存为新版本并立即生效。

    注意"立即"的实际含义：本进程的缓存立刻失效，后续请求都用新版；
    多 worker 部署时其他进程最多等 prompt_cache_seconds 秒。
    """
    ctx = request.app.state.ctx
    form = await request.form()
    auth.verify_csrf(request, str(form.get("_csrf", "")))

    system_prompt = str(form.get("system_prompt", "")).strip()
    user_template = str(form.get("user_template", "")).strip()
    note = str(form.get("note", "")).strip()[:200]

    problem = validate_template(system_prompt, user_template)
    if problem:
        return RedirectResponse(
            "/admin/prompts?err=" + quote(f"模板校验未通过：{problem}"), status_code=303)

    conn = db.connect(ctx.settings.chat_db_path)
    try:
        created = prompt_repo.create_version(
            conn, system_prompt=system_prompt, user_template=user_template,
            name=f"v{prompt_repo.next_version(conn)}", note=note,
            created_by=user.get("id"),
        )
    finally:
        conn.close()

    if ctx.prompt_provider:
        ctx.prompt_provider.invalidate()
    ver = created["version"]
    log(f"提示词已更新到 v{ver}（by {user.get('username')}）", "ok")
    return RedirectResponse(
        "/admin/prompts?ok=" + quote(f"已保存为 v{ver}，下一个问题起生效"),
        status_code=303)


@router.post("/admin/prompts/{version}/activate")
async def prompts_activate(version: int, request: Request,
                          user: dict = Depends(auth.require_admin)) -> Response:
    """回滚：把历史某版本重新设为生效。只切标记，不复制内容。"""
    ctx = request.app.state.ctx
    form = await request.form()
    auth.verify_csrf(request, str(form.get("_csrf", "")))

    conn = db.connect(ctx.settings.chat_db_path)
    try:
        ok = prompt_repo.activate(conn, version)
    finally:
        conn.close()
    if not ok:
        raise ApiError(PRM_NOT_FOUND, detail={"version": version})

    if ctx.prompt_provider:
        ctx.prompt_provider.invalidate()
    return RedirectResponse(
        f"/admin/prompts?ok={quote(f'已回滚到 v{version}')}", status_code=303)


@router.post("/admin/prompts/preview")
async def prompts_preview(request: Request,
                          user: dict = Depends(auth.require_admin)) -> Response:
    """
    用**当前编辑框里的草稿**跑一次真实问答（检索 + 生成 + 护栏），不落库。

    为什么必须能预览：prompt 是"改一句话、效果全变"的东西。
    只看文本框无法判断它会让答案变成什么样，保存完再去员工页试，
    等于拿生产环境做实验。
    """
    ctx = request.app.state.ctx
    form = await request.form()
    auth.verify_csrf(request, str(form.get("_csrf", "")))

    system_prompt = str(form.get("system_prompt", "")).strip()
    user_template = str(form.get("user_template", "")).strip()
    question = str(form.get("question", "")).strip() or _PREVIEW_QUESTION

    problem = validate_template(system_prompt, user_template)
    preview: dict = {"question": question, "rows": None, "error": problem}

    if not problem:
        def _run() -> dict:
            orch = ctx.orchestrator
            if orch is None:
                return {"error": "索引未就绪，无法预览（请先重建索引）"}
            draft = PromptTemplate(version=-1, name="draft",
                                   system_prompt=system_prompt,
                                   user_template=user_template)
            hits, _debug = orch.retriever.retrieve(question, top_k=ctx.settings.top_k)
            if not hits:
                return {"question": question, "refused": True, "answer": "",
                        "citations": [], "guard": None}
            messages, context, _label = build_prompt(question, hits, template=draft)
            result = orch.chat_model.chat(
                messages, temperature=ctx.settings.llm_temperature,
                max_tokens=ctx.settings.llm_max_tokens)
            guard = run_guard(result.text, context, len(hits))
            return {
                "question": question,
                "refused": False,
                "answer": result.text,
                "citations": [
                    {"index": i, "clause": h.chunk.clause_range or h.chunk.clause_no,
                     "snippet": h.chunk.content[:80]}
                    for i, h in enumerate(hits, start=1)
                ],
                "guard": {"status": guard.status, "warnings": guard.warnings,
                          "unverified": guard.unverified_numbers},
                "usage": {"tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
                          "latency_ms": result.latency_ms},
            }

        try:
            preview = await run_in_threadpool(_run)
        except Exception as ex:                    # noqa: BLE001 — 预览失败不该让页面挂掉
            log(f"提示词预览失败：{ex}", "err")
            preview = {"question": question, "rows": None,
                       "error": f"预览失败：{type(ex).__name__}"}

    active, tpl, versions = _load_versions(request)
    return render(request, "prompts.html", {
        "active": active,
        "tpl": tpl,
        "versions": versions,
        "citation_rules": CITATION_RULES.strip(),
        "system_prompt": system_prompt,
        "user_template": user_template,
        "preview": preview,
        "defaults": {"system": DEFAULT_SYSTEM_PROMPT, "user": DEFAULT_USER_TEMPLATE},
    })
