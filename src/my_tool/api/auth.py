"""认证与授权：JWT 读取、角色依赖、CSRF 校验、登录限流。

    **这是全系统唯一判断"你是谁、你能不能做这件事"的地方。**
    handler 里不许自己解析 token，也不许自己比 role —— 只许依赖这里的两个函数。

    设计取舍：JWT 无状态，但我们仍然每请求查一次 users 表。
      标准 JWT 的卖点是"不用查库"，代价是禁用账号、改角色要等 token 过期（8 小时）。
      这个后台里管理员会因为"某人停职"而禁用账号，等 8 小时不可接受。
      单行主键查询约 0.05ms —— 在这个规模上，可撤销性远比无状态值钱。
"""

import threading
import time
from typing import Any

from fastapi import Request

from ..core import security
from ..store import db, user_repo
from .errors import (AUTH_BAD_CREDENTIALS, AUTH_CSRF, AUTH_FORBIDDEN,
                     AUTH_REQUIRED_ERR, AUTH_TOO_MANY_TRIES, ApiError)

# =============================================================================
# 【模块 20 · 认证授权】目标文件：src/my_tool/api/auth.py
# ★核心模块：改鉴权只改这里。任何 handler 都不得绕过 current_user / require_admin。
# =============================================================================

COOKIE_TOKEN = "pcc_token"
COOKIE_CSRF = "pcc_csrf"

# 演示态用户：auth_required=False 时用它，保证测试与离线演示与阶段 2 行为一致
DEMO_USER = {
    "id": "demo-user",
    "username": "demo-user",
    "display_name": "演示用户",
    "role": "admin",
    "status": "active",
}


# --------------------------------------------------------------------------- #
# 登录限流：内存计数，重启清零
# --------------------------------------------------------------------------- #
class LoginThrottle:
    """
    同账号连续失败 N 次 → 锁 M 分钟。

    刻意按 username 而不是按 IP 计数：内部后台通常在同一出口 IP 后面，
    按 IP 锁会把一个人的手误变成全公司登不上。
    内存实现的代价是多 worker 时计数不共享（各算各的），在这个规模上可以接受。
    """

    def __init__(self, max_fails: int = 5, lock_minutes: int = 5):
        self.max_fails = max(1, max_fails)
        self.lock_seconds = max(1, lock_minutes) * 60
        self._state: dict[str, list[float]] = {}      # username -> [fails, locked_until]
        self._lock = threading.Lock()

    def check(self, username: str) -> int:
        """返回剩余锁定秒数；0 表示可以尝试。"""
        now = time.time()
        with self._lock:
            fails, until = self._state.get(username, [0, 0.0])
            return max(0, int(until - now)) if until > now else 0

    def fail(self, username: str) -> None:
        now = time.time()
        with self._lock:
            fails, until = self._state.get(username, [0, 0.0])
            fails += 1
            if fails >= self.max_fails:
                until = now + self.lock_seconds
                fails = 0          # 锁定期满后重新计数，而不是永久累积
            self._state[username] = [fails, until]

    def reset(self, username: str) -> None:
        with self._lock:
            self._state.pop(username, None)


# --------------------------------------------------------------------------- #
# 取 token / 校验 token
# --------------------------------------------------------------------------- #
def _extract_token(request: Request) -> str:
    """
    优先 Cookie（浏览器），其次 Authorization: Bearer（curl / 将来的 API 调用方）。

    Cookie 用 HttpOnly 存，是为了让 XSS 偷不到 token —— 这也是为什么
    管理后台选"服务端渲染 + HttpOnly Cookie"而不是"前端存 localStorage"。
    """
    token = request.cookies.get(COOKIE_TOKEN, "")
    if token:
        return token
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _load_user(request: Request, user_id: str) -> dict[str, Any] | None:
    """按 id 取用户并校验状态。每次请求都查库，换取"禁用立即生效"。"""
    ctx = request.app.state.ctx
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        row = user_repo.get_by_id(conn, user_id)
    finally:
        conn.close()
    if row is None or row.get("status") != "active":
        return None
    return row


def get_optional_user(request: Request) -> dict[str, Any] | None:
    """
    有登录态就返回用户，没有就返回 None。**不抛异常**，供登录页判断"已登录则跳转"。

    这里是 auth_required 开关的唯一落点：关掉时所有人都被当成演示管理员。
    """
    ctx = request.app.state.ctx
    if not ctx.settings.auth_required:
        return dict(DEMO_USER)

    token = _extract_token(request)
    if not token:
        return None
    try:
        payload = security.decode_token(token, ctx.auth_secret)
    except Exception:                                  # noqa: BLE001
        # 过期、签名不对、格式坏、算法不符 —— 对调用方都是同一件事：请重新登录。
        # 具体原因不外传（帮不上忙，还可能泄露实现细节）。
        return None
    user_id = str(payload.get("sub") or "")
    if not user_id:
        return None
    return _load_user(request, user_id)


def current_user(request: Request) -> dict[str, Any]:
    """FastAPI 依赖：必须登录。页面请求由 app.py 的异常处理器转成 302 到 /login。"""
    user = get_optional_user(request)
    if user is None:
        raise ApiError(AUTH_REQUIRED_ERR)
    # 顺手挂到 request.state：模板渲染要读它，省得每个路由都再查一次
    request.state.user = user
    return user


def require_admin(request: Request) -> dict[str, Any]:
    """
    FastAPI 依赖：必须是管理员。

    依赖 current_user 而不是自己判断，保证"未登录 → 401"优先于"角色不足 → 403"，
    否则没登录的人访问 /admin 会得到 403，看不出该先去登录。
    """
    user = current_user(request)
    if user.get("role") != "admin":
        raise ApiError(AUTH_FORBIDDEN, detail={"path": str(request.url.path)})
    return user


# --------------------------------------------------------------------------- #
# 登录动作
# --------------------------------------------------------------------------- #
def authenticate(request: Request, username: str, password: str) -> dict[str, Any]:
    """
    校验账号密码，成功返回用户行，失败抛统一的中文错误。

    两处细节：
      · 用户不存在时也跑一次哈希校验（对随机串），让"用户不存在"与"密码错"
        的耗时接近 —— 时间差也是账号枚举的入口。
      · 失败计数只在"确实失败"时累加，成功即清零。
    """
    ctx = request.app.state.ctx
    throttle: LoginThrottle = request.app.state.throttle

    remain = throttle.check(username)
    if remain > 0:
        raise ApiError(AUTH_TOO_MANY_TRIES, minutes=max(1, remain // 60))

    conn = db.connect(ctx.settings.chat_db_path)
    try:
        row = user_repo.get_by_username(conn, username)
    finally:
        conn.close()

    if row is None:
        # 对一个固定的假哈希做校验，纯为抹平时间差
        security.verify_password(password, security.hash_password("__no_such_user__"))
        throttle.fail(username)
        raise ApiError(AUTH_BAD_CREDENTIALS)

    if not security.verify_password(password, row["password_hash"]):
        throttle.fail(username)
        raise ApiError(AUTH_BAD_CREDENTIALS)

    if row.get("status") != "active":
        # 不提示"账号已停用"：对内暴露"这个账号存在但被停了"没有好处
        throttle.fail(username)
        raise ApiError(AUTH_BAD_CREDENTIALS)

    throttle.reset(username)
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        user_repo.touch_login(conn, row["id"])
    finally:
        conn.close()
    return row


def issue_token(request: Request, user: dict[str, Any]) -> str:
    ctx = request.app.state.ctx
    return security.create_token(
        user_id=user["id"], username=user["username"], role=user["role"],
        secret=ctx.auth_secret, ttl_hours=ctx.settings.jwt_ttl_hours,
    )


# --------------------------------------------------------------------------- #
# Cookie
# --------------------------------------------------------------------------- #
def cookie_attrs(request: Request) -> dict[str, Any]:
    """统一的 Cookie 属性。SameSite=Lax 是 CSRF 的第一道防线（跨站 POST 不带 cookie）。"""
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": bool(request.app.state.ctx.settings.cookie_secure),
        "path": "/",
    }


def set_auth_cookies(request: Request, response: Any, token: str, csrf: str) -> None:
    """
    登录成功后写两个 cookie：
      · pcc_token —— HttpOnly，JS 读不到；
      · pcc_csrf  —— **必须可被 JS/表单读到**，因为要回填到表单隐藏字段（双提交模式）。
    """
    max_age = int(request.app.state.ctx.settings.jwt_ttl_hours * 3600)
    attrs = cookie_attrs(request)
    response.set_cookie(COOKIE_TOKEN, token, max_age=max_age, **attrs)
    response.set_cookie(COOKIE_CSRF, csrf, max_age=max_age,
                        **{**attrs, "httponly": False})


def clear_auth_cookies(response: Any) -> None:
    response.delete_cookie(COOKIE_TOKEN, path="/")
    response.delete_cookie(COOKIE_CSRF, path="/")


# --------------------------------------------------------------------------- #
# CSRF（双提交）
# --------------------------------------------------------------------------- #
def ensure_csrf_cookie(request: Request, response: Any) -> str:
    """页面渲染前调用：没有 csrf cookie 就种一个，返回要写进表单的值。"""
    existing = request.cookies.get(COOKIE_CSRF, "")
    if existing:
        return existing
    token = security.new_csrf_token()
    attrs = cookie_attrs(request)
    response.set_cookie(COOKIE_CSRF, token, max_age=8 * 3600,
                        **{**attrs, "httponly": False})
    return token


def verify_csrf(request: Request, form_token: str) -> None:
    """
    比对 cookie 与表单里的令牌。

    为什么在 SameSite=Lax 之外还要这一道：Lax 只挡跨站 POST，挡不住
    "同站的其他子域被攻破"以及部分老浏览器的行为差异。双提交几乎零成本。

    演示模式（auth_required=False）直接放行：那时没有登录页，也就没有
    csrf cookie 可拿，硬校验会让后台所有表单都提交不了。
    """
    if not request.app.state.ctx.settings.auth_required:
        return
    cookie_token = request.cookies.get(COOKIE_CSRF, "")
    if not security.csrf_equal(cookie_token, form_token):
        raise ApiError(AUTH_CSRF, detail={"reason": "csrf mismatch"})


def csrf_value(request: Request) -> str:
    """模板里 {{ csrf_token }} 的取值：没有 cookie 时返回空串，由页面重新种。"""
    return request.cookies.get(COOKIE_CSRF, "")
