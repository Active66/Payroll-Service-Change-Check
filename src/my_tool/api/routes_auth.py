"""登录 / 登出页面。

    这是唯一允许匿名访问的页面路由。登录成功的跳转目标是**按角色**决定的：
      admin    → /admin（概览）
      employee → /（员工问答）
"""

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from starlette.responses import HTMLResponse

from ..core import security
from . import auth
from .errors import ApiError
from .templating import render

router = APIRouter()

# =============================================================================
# 【模块 21 · 登录】目标文件：src/my_tool/api/routes_auth.py
# =============================================================================


def _safe_next(raw: str) -> str:
    """
    只接受站内相对路径，防止开放重定向（`?next=https://evil.com`）。

    两个必查的条件：
      · 必须以 "/" 开头；
      · 不能以 "//" 或 "/\\" 开头 —— 浏览器会把 "//evil.com" 当成协议相对地址。
    """
    if not raw or not raw.startswith("/"):
        return ""
    if raw.startswith("//") or raw.startswith("/\\"):
        return ""
    return raw


def _home_for(role: str) -> str:
    return "/admin" if role == "admin" else "/"


@router.get("/login")
def login_page(request: Request, next: str = "") -> Response:
    """登录页。已登录直接送回目标页，避免"登录后又看到登录页"的困惑。"""
    user = auth.get_optional_user(request)
    if user is not None:
        return RedirectResponse(_safe_next(next) or _home_for(user.get("role", "employee")),
                                status_code=303)
    request.state.user = None
    return render(request, "login.html", {"next": _safe_next(next), "username": ""})


@router.post("/login")
async def login_submit(request: Request) -> Response:
    """
    提交登录。

    失败时返回 **401 + 重新渲染登录页**（带中文错误条），而不是 JSON：
      它是页面，跳去一个 JSON 错误体等于把用户丢在荒野。
      不带 WWW-Authenticate 头，所以浏览器不会弹原生的密码输入框。
    """
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    next_url = _safe_next(str(form.get("next", "")))

    def _back(error: str, status_code: int) -> HTMLResponse:
        request.state.user = None
        return render(request, "login.html",
                      {"next": next_url, "username": username, "err": error},
                      status_code=status_code)

    if not username or not password:
        # 空值不消耗登录次数：这只是没填，不是猜密码
        return _back("请输入用户名和密码", 400)

    try:
        auth.verify_csrf(request, str(form.get("_csrf", "")))
        user = auth.authenticate(request, username, password)
    except ApiError as ex:
        return _back(ex.message, ex.status)

    token = auth.issue_token(request, user)
    response = RedirectResponse(next_url or _home_for(user["role"]), status_code=303)
    auth.set_auth_cookies(request, response, token, security.new_csrf_token())
    return response


@router.post("/logout")
async def logout(request: Request) -> object:
    """
    退出。用 POST 而不是 GET：GET 会被浏览器预取、被工单系统里的链接预览误触发，
    表现为"点开一条聊天记录，人就被登出了"。
    """
    form = await request.form()
    # 退出不校验 CSRF 失败就罢工：这里最坏的结果只是"被登出"，不值得让人卡住。
    # 但仍然校验一次，保持规则一致。
    try:
        auth.verify_csrf(request, str(form.get("_csrf", "")))
    except ApiError:
        pass
    response = RedirectResponse("/login", status_code=303)
    auth.clear_auth_cookies(response)
    return response
