"""FastAPI 装配：加载索引、建库、注册中间件与异常处理器、挂载前端静态目录。

    uvicorn my_tool.api.app:app --reload          # 开发
    payroll-check serve                            # CLI 包装（见 main.py）

两条装配原则：
  ① 索引加载失败**不阻塞启动** —— 服务能起来，才能进后台修；
  ② 异常处理器按请求类型分流 —— API 返 JSON，页面返 HTML（401 转 302 去登录）。
"""

import json
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..core import security
from ..core.config import PROJECT_ROOT, Settings
from ..core.index_manager import IndexManager
from ..core.llm import build_chat_model
from ..core.orchestrator import Orchestrator
from ..core.pipeline import build_bm25, load_index
from ..core.prompt import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_TEMPLATE
from ..core.prompt_provider import PromptProvider
from ..core.retrieval.embedding import build_embedding_model
from ..store import db, policy_repo, prompt_repo, user_repo
from ..utils.console import log
from . import auth
from .errors import (INTERNAL, MISSING_JSON, NO_SUCH_ROUTE, AUTH_REQUIRED_ERR,
                     ApiError, error_body)
from .routes_admin import router as admin_router
from .routes_audit import router as audit_router
from .routes_auth import router as auth_router
from .routes_chat import router as chat_router
from .routes_draft import router as draft_router
from .routes_external import router as external_router
from .routes_kb import router as kb_router
from .routes_prompts import router as prompts_router
from .templating import render

WEB_DIR = PROJECT_ROOT / "web"


# =============================================================================
# 【模块 18 · 服务装配】目标文件：src/my_tool/api/app.py
#   启动顺序不能换：
#     ① 加载索引（失败不阻塞，原因记在 ctx.index_error，让 /healthz 与后台看得见）；
#     ② 建库 + 迁移（幂等）；
#     ③ 清理"卡住的流式占位行"（上次进程被杀留下的）；
#     ④ 准备认证密钥 + 首次启动种子管理员。
# =============================================================================


@dataclass
class AppContext:
    """进程级共享状态。刻意只有一个实例：索引与 BM25 全在内存，不重复加载。"""

    settings: Settings
    orchestrator: Orchestrator | None = None
    manifest: dict[str, Any] | None = None
    index_error: str | None = None
    stats: dict[str, int] = field(default_factory=dict)
    # JWT 密钥。必须持久化（见 core/security.load_or_create_secret），
    # 否则重启一次所有人都被踢下线。
    auth_secret: str = ""
    # 索引重建状态，由 IndexManager 维护；这里先给个默认值，
    # 让概览页在"功能还没上线"时也能正常渲染。
    rebuild_state: dict[str, Any] = field(
        default_factory=lambda: {"status": "idle", "error": None, "started_at": None,
                                 "finished_at": None, "chunks": 0, "docs": 0})
    # 索引的唯一写入口。放在 ctx 上而不是 app.state，是因为它要回写
    # ctx.orchestrator（热切换），两者必须绑在一起才不会出现"换了索引但
    # 有人还拿着旧 orchestrator"的错位。
    index_manager: Any = None
    # 提示词提供者。Orchestrator 每个请求都要问它"当前用哪版模板"。
    prompt_provider: Any = None


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def _wants_html(request: Request) -> bool:
    """
    这个请求该返回页面还是 JSON？

    优先看路径（后台与登录页永远是页面），再看 Accept ——
    因为 curl 与前端 fetch 默认 Accept 不是 text/html，
    而浏览器地址栏访问一定是。
    """
    path = request.url.path
    if path.startswith("/admin") or path in ("/", "/login", "/logout"):
        return True
    return "text/html" in request.headers.get("accept", "").lower()


def _login_redirect(request: Request) -> RedirectResponse:
    """未登录访问页面 → 带上原地址去登录，登录后能回到原位。"""
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse("/login?next=" + quote(target, safe="/?=&"), status_code=303)


def _seed_agent_data(s: Settings) -> None:
    """
    参数表与人事档案的种子。

    只在**参数表为空**时写入：参数是业务数据，管理员会在后台改（基数每年 7 月变，
    第十九条），每次启动都覆盖回去等于把人的修改抹掉。
    档案同理 —— 生产环境接 HR 主数据，这里只是让演示与测试跑得起来。
    """
    from ..core.agent import seed
    from ..store import employee_repo, param_repo

    conn = db.connect(s.chat_db_path)
    try:
        if param_repo.count_params(conn) == 0:
            n = seed.seed_params(conn, updated_by="bootstrap")
            log(f"已写入 {n} 条制度参数（来自制度第十八条等条款）", "ok")
        if employee_repo.count(conn) == 0:
            n = seed.seed_employees(conn)
            log(f"已写入 {n} 条演示人事档案（生产环境应接 HR 主数据）", "ok")
    except Exception as ex:                        # noqa: BLE001
        # 种子失败不该阻塞启动：服务能起来，才能进后台排查
        log(f"写入 Agent 种子数据失败：{ex}", "warn")
    finally:
        conn.close()


def _seed_admin(s: Settings) -> None:
    """
    首次启动创建管理员。

    密码优先级：环境变量 ADMIN_SEED_PASSWORD > 随机生成并打印到启动日志。
    随机生成时**只打印一次**，所以必须在启动日志里显眼地写出来 ——
    否则部署完没人知道密码，只能删库重来。
    """
    conn = db.connect(s.chat_db_path)
    try:
        if user_repo.count_users(conn) > 0:
            return
        password = s.admin_seed_password or secrets.token_urlsafe(9)
        user_repo.create_user(
            conn,
            username=s.admin_seed_username,
            display_name="系统管理员",
            role="admin",
            password_hash=security.hash_password(password),
            department="薪酬共享服务中心",
        )
        if s.admin_seed_password:
            log(f"已创建初始管理员：{s.admin_seed_username}（密码取自 ADMIN_SEED_PASSWORD）", "ok")
        else:
            log("=" * 62, "warn")
            log(f"已创建初始管理员：{s.admin_seed_username} / {password}", "warn")
            log("这是随机生成的一次性密码，请登录后立即修改，本条只打印一次。", "warn")
            log("=" * 62, "warn")
    finally:
        conn.close()


def _seed_prompt_template(s: Settings) -> None:
    """
    首次启动写入默认提示词模板。

    默认值就是需求里给的那段话，原样落库 —— 不在代码里"顺手优化"，
    否则管理员看到的模板和文档里写的对不上，改起来心里没底。
    """
    conn = db.connect(s.chat_db_path)
    try:
        if prompt_repo.count_versions(conn) == 0:
            prompt_repo.create_version(
                conn, system_prompt=DEFAULT_SYSTEM_PROMPT,
                user_template=DEFAULT_USER_TEMPLATE,
                name="初始版本", note="系统内置默认，可在本页编辑",
            )
            log("已写入默认提示词模板 v1", "ok")
    finally:
        conn.close()


def _register_legacy_document(s: Settings) -> None:
    """
    把"索引里有、但登记表里没有"的旧制度补登记。

    没有这一步，升级后第一次上传文件触发重建时，原有制度会从索引里静默消失：
    重建只认 policy_documents，而阶段 1/2 的文档从来没进过那张表。
    只在登记表为空时做一次，之后由后台上传/删除维护。
    """
    conn = db.connect(s.chat_db_path)
    try:
        if policy_repo.list_documents(conn):
            return
    finally:
        conn.close()

    mpath = s.index_dir / "manifest.json"
    if not mpath.exists():
        return
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    doc_path = Path(str(manifest.get("doc_path") or ""))
    if not doc_path.exists():
        return

    from ..core.registry import register_document_file
    info = register_document_file(s, doc_path)
    if info:
        log(f"已把原有制度文件登记进知识库：{info['doc_id']}（{doc_path.name}）", "ok")


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings.from_env()

    ctx = AppContext(settings=s)

    # ① 建库在前：后面的种子数据、提示词、鉴权都要读库
    db.init_db(s.chat_db_path)
    stale = db.cleanup_stale_streaming(s.chat_db_path)
    if stale:
        log(f"已把 {stale} 条残留的流式消息标记为中断", "warn")

    # ② 密钥与种子数据
    # 密钥优先环境变量，否则读本地文件（没有就生成）。
    ctx.auth_secret = s.auth_secret or security.load_or_create_secret(s.auth_secret_file)
    _seed_admin(s)
    _seed_prompt_template(s)
    _seed_agent_data(s)
    _register_legacy_document(s)

    # ③ 提示词提供者要在 Orchestrator 之前建：编排器构造时就要拿到它
    def _load_active_prompt() -> dict[str, Any] | None:
        conn = db.connect(s.chat_db_path)
        try:
            return prompt_repo.get_active(conn)
        finally:
            conn.close()

    ctx.prompt_provider = PromptProvider(_load_active_prompt, s.prompt_cache_seconds)

    # ④ 加载索引（失败不阻塞启动，让后台能起来修）
    try:
        store, manifest = load_index(s)
        ctx.orchestrator = Orchestrator(
            store, build_bm25(store), build_embedding_model(s), build_chat_model(s), s,
            prompt_provider=ctx.prompt_provider,
        )
        ctx.manifest = manifest
        log(f"索引就绪：{len(store.chunks)} 条切片｜模型 {s.llm_provider}", "ok")
    except Exception as ex:                      # noqa: BLE001 — 索引问题不该让服务起不来
        ctx.index_error = f"{type(ex).__name__}: {ex}"
        log(f"索引加载失败（服务仍会启动，/chat 将返回 IDX_001）：{ex}", "err")

    # ⑤ 索引管理器最后建：它接管 ctx.orchestrator 的替换权
    ctx.index_manager = IndexManager(s, ctx)

    app = FastAPI(
        title="薪酬变更核对系统",
        version="0.3.0",
        description="制度 RAG + 员工问答入口 + 管理后台。",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.ctx = ctx
    app.state.throttle = auth.LoginThrottle(s.login_max_fails, s.login_lock_minutes)

    # —— 每请求一个 trace_id：日志、错误体、chat_messages 三处共用，报障时能串起来 ——
    @app.middleware("http")
    async def _attach_trace(request: Request, call_next):     # type: ignore[no-untyped-def]
        tid = "tr_" + uuid.uuid4().hex[:8]
        request.state.trace_id = tid
        response = await call_next(request)
        response.headers["X-Trace-Id"] = tid
        return response

    # —— 异常处理器：API 返 JSON，页面返 HTML ——
    @app.exception_handler(ApiError)
    async def _on_api_error(request: Request, exc: ApiError) -> Any:
        tid = _trace_id(request)
        # 未登录访问页面：跳登录而不是甩一个 401 JSON，否则员工看到的是
        # {"ok":false,...} 这种他完全无法处理的东西
        if exc.status == 401 and _wants_html(request):
            return _login_redirect(request)
        if _wants_html(request):
            return render(request, "error.html",
                          {"status": exc.status, "message": exc.message,
                           "code": exc.code, "trace_id": tid},
                          status_code=exc.status)
        return JSONResponse(
            status_code=exc.status,
            content=error_body(exc.code, exc.message, tid, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _on_validation(request: Request, exc: RequestValidationError) -> Any:
        # 我们手写校验，理论上到不了这里；保留是为了兜住 fastapi 对 body/query 的类型校验
        tid = _trace_id(request)
        if _wants_html(request):
            return render(request, "error.html",
                          {"status": 400, "message": "请求参数格式不正确",
                           "code": MISSING_JSON.code, "trace_id": tid},
                          status_code=400)
        return JSONResponse(
            status_code=400,
            content=error_body(MISSING_JSON.code, "请求参数格式不正确，请检查字段类型",
                               tid, {"errors": str(exc.errors())[:500]}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _on_http(request: Request, exc: StarletteHTTPException) -> Any:
        tid = _trace_id(request)
        if _wants_html(request):
            msg = NO_SUCH_ROUTE.message if exc.status_code in (404, 405) else INTERNAL.message.format(trace_id=tid)
            code = NO_SUCH_ROUTE.code if exc.status_code in (404, 405) else INTERNAL.code
            return render(request, "error.html",
                          {"status": exc.status_code, "message": msg,
                           "code": code, "trace_id": tid},
                          status_code=exc.status_code)
        if exc.status_code in (404, 405):
            return JSONResponse(
                status_code=exc.status_code,
                content=error_body(NO_SUCH_ROUTE.code, NO_SUCH_ROUTE.message, tid,
                                   {"path": str(request.url.path), "method": request.method}),
            )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(INTERNAL.code, INTERNAL.message.format(trace_id=tid), tid),
        )

    @app.exception_handler(Exception)
    async def _on_unhandled(request: Request, exc: Exception) -> Any:
        tid = _trace_id(request)
        log(f"[{tid}] 未捕获异常 {type(exc).__name__}: {exc}", "err")
        if _wants_html(request):
            return render(request, "error.html",
                          {"status": 500, "message": INTERNAL.message.format(trace_id=tid),
                           "code": INTERNAL.code, "trace_id": tid},
                          status_code=500)
        return JSONResponse(
            status_code=500,
            content=error_body(INTERNAL.code, INTERNAL.message.format(trace_id=tid), tid,
                               {"type": type(exc).__name__}),
        )

    # —— 员工问答页：必须登录，所以不能用 StaticFiles 直接兜住 "/" ——
    @app.get("/", include_in_schema=False)
    def home(request: Request) -> Any:
        user = auth.get_optional_user(request)
        if user is None:
            raise ApiError(AUTH_REQUIRED_ERR)
        index = WEB_DIR / "index.html"
        if not index.exists():
            log(f"未找到前端页面 {index}", "err")
            return RedirectResponse("/admin", status_code=303)
        # no-cache：内部系统升级频繁，宁可每次校验也别让员工用着旧页面报障
        return FileResponse(str(index), headers={"Cache-Control": "no-cache"})

    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(admin_router)
    app.include_router(kb_router)
    app.include_router(prompts_router)
    app.include_router(audit_router)
    # 建单确认：员工确认草稿后落库（create_draft 被循环拦截后的另一半）
    app.include_router(draft_router)
    # 对外集成：/api/v1/*，走 API Key 而不是 JWT（见 routes_external.py 顶部说明）
    app.include_router(external_router)

    # 静态资源统一挂 /static：员工端 app.js/style.css 与后台 admin.css 同处 web/
    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
    else:
        log(f"未找到前端目录 {WEB_DIR}，仅提供 API", "warn")

    return app


app = create_app()
