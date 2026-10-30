"""Jinja2 渲染封装：统一注入当前用户、CSRF、一次性提示。

    为什么手写 render 而不直接用 Jinja2Templates.TemplateResponse：
      模板里要用 csrf_token，而 csrf cookie 可能需要"这次响应才种下"。
      先定值 → 再渲染 → 再决定是否 Set-Cookie，顺序必须能控制。
"""

from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..core.config import PROJECT_ROOT
from . import auth

TEMPLATE_DIR = PROJECT_ROOT / "src" / "my_tool" / "templates"
# 前端资源统一放仓库根的 web/：员工端与管理后台共用同一个静态目录，
# 只用一个 mount（/static），避免出现 /static 与 /assets 两套路径，
# 也避免"改了 CSS 却发现浏览器加载的是另一个目录"的排查成本。
STATIC_DIR = PROJECT_ROOT / "web"


def _fmt_time(value: str | None, fmt: str = "%m-%d %H:%M") -> str:
    """把库里的 ISO 字符串格式化成页面显示用。解析失败就原样返回，不抛异常。"""
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(str(value)).strftime(fmt)
    except (ValueError, TypeError):
        return str(value)


env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),   # 默认转义，防 XSS
    trim_blocks=True,
    lstrip_blocks=True,
)
env.filters["dt"] = _fmt_time


def render(request: Request, template: str, context: dict[str, Any] | None = None,
           status_code: int = 200) -> HTMLResponse:
    """
    渲染一个页面。

    注入的公共变量：
      user        当前登录用户（模板里判空、显示姓名与角色）
      csrf_token  写进表单隐藏字段
      ok / err    一次性提示（走 query 参数而不是 session，简单且无损）
      path        当前路径，给侧边栏做高亮
    """
    ctx: dict[str, Any] = {
        "user": getattr(request.state, "user", None),
        "csrf_token": "",
        "ok": request.query_params.get("ok", ""),
        "err": request.query_params.get("err", ""),
        "path": request.url.path,
        "year": datetime.now().year,
    }

    # CSRF：先定值（可能这次才种），渲染时用同一个值，响应里再回写 cookie
    csrf = request.cookies.get(auth.COOKIE_CSRF, "")
    fresh = not csrf
    if fresh:
        from ..core import security
        csrf = security.new_csrf_token()
    ctx["csrf_token"] = csrf

    ctx.update(context or {})

    # 用 getattr 而非 get_template 抛异常：模板缺失属于部署问题，
    # 让它冒成 500 并留下 trace_id，比返回半张页面好排查。
    html = env.get_template(template).render(**ctx)
    response = HTMLResponse(html, status_code=status_code)

    if fresh:
        attrs = auth.cookie_attrs(request)
        response.set_cookie(auth.COOKIE_CSRF, csrf, max_age=8 * 3600,
                            **{**attrs, "httponly": False})
    return response
