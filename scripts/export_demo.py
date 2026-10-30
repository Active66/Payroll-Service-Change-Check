"""把服务端真实渲染的页面导出成静态快照（`demo/`），供离线预览与评审。

    **存在的理由**：这个环境里后台进程无法常驻（工具会话结束即回收进程树），
    所以"启动服务让人打开浏览器看"这条路走不通。又不能手绘效果图 ——
    手绘的永远比真实页面好看，也就永远看不出真问题（列宽挤成一团、徽标换行、
    空态文案没写、部门统计串行）。所以走第三条路：真模板渲染 → 落成静态 HTML。

    用法：
        python scripts/export_demo.py             # 导出到 demo/
        python scripts/export_demo.py --out D:\\x # 指定目录

    产出：
        demo/index.html      带左侧导航的外壳（iframe 切换各页）
        demo/*.html          各页面真实渲染结果
        demo/static/         web/ 目录副本（CSS/JS）

    已知取舍：
      · 表单提交、实时问答、语音输入在静态快照下不可用 —— 这是快照的固有边界，
        外壳顶部有明确标注，不做假交互；
      · 站内链接被改写成指向本地文件，未识别的路径退化为 `#`，
        避免预览时点出一个 404 让人误以为页面坏了。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient          # noqa: E402

from my_tool.api.app import create_app             # noqa: E402
from my_tool.core.config import Settings           # noqa: E402
from my_tool.store import db                       # noqa: E402

DEMO_PASSWORD = "Demo12345"

# 站内绝对路径 → 本地文件。值里带 query 的在 localize() 里拼回去。
LINK = {
    "/": "chat.html",
    "/admin": "overview.html",
    "/admin/kb": "kb_list.html",
    "/admin/prompts": "prompts.html",
    "/admin/audit": "audit_list.html",
    "/login": "login.html",
}

# 页面清单。顺序即侧边栏顺序，key 是文件名，值是路由。
# 固定 doc_id 而不是"第一份文档"：快照要可复现，随机取会让两次导出对不上。
DOC_ID = "POL-CMP-2026-001"


def localize(html: str) -> str:
    """把 `/static/...` 与站内绝对链接改写成本地相对路径。"""
    html = (html.replace('src="/static/', 'src="static/')
                .replace('href="/static/', 'href="static/')
                .replace("url('/static/", "url('static/"))

    def sub(m: re.Match[str]) -> str:
        attr, url = m.group(1), m.group(2)
        path, _, query = url.partition("?")
        # 带 id 的详情页全部收敛到同一个已导出的文件上
        if path.startswith("/admin/kb/") and path != "/admin/kb":
            return f'{attr}="kb_detail.html"'
        if path.startswith("/admin/audit/"):
            return f'{attr}="audit_detail.html"'
        if path in LINK:
            suffix = f"?{query}" if query else ""
            return f'{attr}="{LINK[path]}{suffix}"'
        if path.startswith("/admin/kb"):
            return f'{attr}="kb_list.html"'
        return f'{attr}="#"'

    return re.sub(r'(href|src)="(/[^"]*)"', sub, html)


def latest_session_id(settings: Settings) -> str | None:
    conn = db.connect(settings.chat_db_path)
    try:
        row = conn.execute(
            "SELECT id FROM chat_sessions ORDER BY created_at DESC LIMIT 1").fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出页面静态快照")
    ap.add_argument("--out", default=str(ROOT / "demo"), help="输出目录")
    ap.add_argument("--password", default=DEMO_PASSWORD, help="登录用密码")
    args = ap.parse_args(argv)

    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copytree(ROOT / "web", out / "static")

    settings = Settings.from_env()
    client = TestClient(create_app(settings), base_url="http://test",
                        follow_redirects=False)

    def login(username: str) -> None:
        client.cookies.clear()
        client.get("/login")                      # 种下 csrf cookie
        r = client.post("/login", data={
            "username": username, "password": args.password,
            "_csrf": client.cookies.get("pcc_csrf")})
        if r.status_code != 303:
            raise SystemExit(f"登录 {username} 失败：{r.status_code} "
                             f"（密码不对？用 user reset 重设，或传 --password）")

    sid = latest_session_id(settings)
    pages: dict[str, str] = {
        "overview.html": "/admin",
        "kb_list.html": "/admin/kb",
        "kb_list_history.html": "/admin/kb?status=superseded",
        "prompts.html": "/admin/prompts",
        "audit_list.html": "/admin/audit",
    }
    if (ROOT / "data" / "policy").exists():
        pages["kb_detail.html"] = f"/admin/kb/{DOC_ID}"
    if sid:
        pages["audit_detail.html"] = f"/admin/audit/{sid}"

    # —— 顺序很重要 ——
    # 必须先把所有"需要登录"的页面取完，最后才清 cookie 取登录页。
    # 反过来做会把自己的登录态清掉，后面全部 303（第一版就栽在这）。
    login("admin")
    for name, path in pages.items():
        r = client.get(path)
        if r.status_code != 200:
            print(f"  跳过 {name}（{path} → {r.status_code}）")
            continue
        (out / name).write_text(localize(r.text), encoding="utf-8")
        print(f"  {name:22} {len(r.text):>7}B  ← {path}")

    client.cookies.clear()
    (out / "login.html").write_text(localize(client.get("/login").text), encoding="utf-8")

    login("alice" if _has_user(settings, "alice") else "admin")
    (out / "chat.html").write_text(localize(client.get("/").text), encoding="utf-8")

    # 外壳页单独存一份在 scripts/ 下：这样每次导出（含 rmtree）都能把它带回来，
    # 不用让它成为"第一次手工写过、后来就丢了"的孤儿文件
    shell = ROOT / "scripts" / "demo_shell.html"
    if shell.exists():
        shutil.copy(shell, out / "index.html")
    else:
        print(f"  警告：找不到外壳页 {shell}，只导出了散页面")

    print(f"\n已导出到 {out}")
    print(f"打开：{out / 'index.html'}")
    return 0


def _has_user(settings: Settings, username: str) -> bool:
    conn = db.connect(settings.chat_db_path)
    try:
        return conn.execute("SELECT 1 FROM users WHERE username = ?",
                            (username,)).fetchone() is not None
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
