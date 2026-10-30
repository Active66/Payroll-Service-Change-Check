"""鉴权测试：密码哈希、JWT、登录流程、角色校验、越权防护。

这一组刻意**不用** auth_required=False，因为被检验的对象就是鉴权本身。
所有断言都落在"外部可观察的行为"上（状态码、中文文案、跳转地址、cookie），
而不是内部函数调用 —— 内部实现可以换，行为不能变。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace

import httpx
import pytest

from my_tool.core import security
from my_tool.core.config import Settings
from my_tool.store import chat_repo, db, user_repo

ADMIN_PWD = "Admin12345"
EMP_PWD = "Emp123456"


# --------------------------------------------------------------------------- #
# 夹具：一套独立索引 + 独立库 + 三个账号
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from my_tool.api.app import create_app
    from my_tool.core.pipeline import build_index

    tmp = tmp_path_factory.mktemp("auth")
    base = Settings.from_env()
    s = replace(
        base,
        index_dir=tmp / "index",
        chat_db_path=tmp / "chat.db",
        auth_required=True,
        auth_secret="test-secret-for-jwt-do-not-use-in-prod",
        admin_seed_password=ADMIN_PWD,
        login_max_fails=3,
        login_lock_minutes=5,
    )
    build_index(s)
    app = create_app(s)

    # 两个员工：用来验"员工看不到别人的会话"
    conn = db.connect(s.chat_db_path)
    try:
        for name, display in (("alice", "爱丽丝"), ("bob", "鲍勃")):
            if user_repo.get_by_username(conn, name) is None:
                user_repo.create_user(
                    conn, username=name, display_name=display, role="employee",
                    password_hash=security.hash_password(EMP_PWD),
                )
    finally:
        conn.close()

    return app, s


def _run(coro):
    return asyncio.run(coro)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver", timeout=30)


async def _login(client: httpx.AsyncClient, username: str, password: str):
    """走真实登录流程：先 GET 拿 csrf cookie，再带令牌提交。"""
    await client.get("/login")
    csrf = client.cookies.get("pcc_csrf", "")
    return await client.post("/login", data={
        "_csrf": csrf, "username": username, "password": password,
    }, follow_redirects=False)


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def test_password_roundtrip():
    h = security.hash_password("Abc12345")
    assert h.startswith("pbkdf2_sha256$")
    assert security.verify_password("Abc12345", h)
    assert not security.verify_password("Abc12346", h)


def test_password_hash_is_salted():
    """同一密码两次哈希必须不同，否则等于没加盐（彩虹表直接命中）。"""
    assert security.hash_password("Abc12345") != security.hash_password("Abc12345")


@pytest.mark.parametrize("stored", ["", "garbage", "pbkdf2_sha256$abc$x$y", "md5$1$a$b"])
def test_password_rejects_dirty_data(stored):
    """脏数据一律判失败，绝不抛异常 —— 登录接口不该因为一条坏记录 500。"""
    assert security.verify_password("anything", stored) is False


@pytest.mark.parametrize("pwd,ok", [
    ("short1", False),        # 不足 8 位
    ("abcdefgh", False),      # 没有数字
    ("12345678", False),      # 没有字母
    ("abcd1234", True),
])
def test_password_strength(pwd, ok):
    assert security.is_strong_password(pwd)[0] is ok


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def test_jwt_roundtrip():
    secret = "unit-test-hmac-key-at-least-32-bytes-long"
    token = security.create_token(user_id="u1", username="alice", role="employee",
                                  secret=secret, ttl_hours=1)
    payload = security.decode_token(token, secret)
    assert payload["sub"] == "u1"
    assert payload["role"] == "employee"
    assert payload["exp"] > payload["iat"]


def test_jwt_wrong_secret_rejected():
    token = security.create_token(user_id="u1", username="a", role="admin",
                                  secret="unit-test-hmac-key-at-least-32-bytes-long",
                                  ttl_hours=1)
    with pytest.raises(Exception):
        security.decode_token(token, "another-key-that-is-also-32-bytes-long!!")


def test_jwt_expired_rejected():
    token = security.create_token(user_id="u1", username="a", role="admin",
                                 secret="unit-test-hmac-key-at-least-32-bytes-long",
                                 ttl_hours=-1)
    with pytest.raises(Exception):
        security.decode_token(token, "unit-test-hmac-key-at-least-32-bytes-long")


def test_jwt_rejects_alg_none():
    """
    alg=none 是 JWT 最经典的绕过手法：把签名算法改成 none、签名留空。
    我们显式限定 algorithms=["HS256"]，所以必须被拒。
    """
    def b64(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    forged = "%s.%s." % (
        b64({"alg": "none", "typ": "JWT"}),
        b64({"sub": "admin", "role": "admin", "exp": int(time.time()) + 3600}),
    )
    with pytest.raises(Exception):
        security.decode_token(forged, "unit-test-hmac-key-at-least-32-bytes-long")


# --------------------------------------------------------------------------- #
# 登录流程
# --------------------------------------------------------------------------- #
def test_seed_admin_created(env):
    _, s = env
    conn = db.connect(s.chat_db_path)
    try:
        row = user_repo.get_by_username(conn, "admin")
        assert row is not None
        assert row["role"] == "admin"
        assert security.verify_password(ADMIN_PWD, row["password_hash"])
    finally:
        conn.close()


def test_login_success_redirects_to_admin(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await _login(c, "admin", ADMIN_PWD)
            assert r.status_code == 303
            assert r.headers["location"] == "/admin"
            assert c.cookies.get("pcc_token")      # token 必须落在 cookie 里
    _run(go())


def test_login_wrong_password_returns_chinese_error(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await _login(c, "admin", "WrongPass999")
            assert r.status_code == 401
            # 页面而不是 JSON：错误文案要能被人直接看到
            assert "用户名或密码不正确" in r.text
    _run(go())


def test_login_unknown_user_same_message(env):
    """用户不存在与密码错误必须同一句话，否则就是一个账号枚举接口。"""
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await _login(c, "no_such_user", "Whatever123")
            assert r.status_code == 401
            assert "用户名或密码不正确" in r.text
            assert "不存在" not in r.text
    _run(go())


def test_login_without_csrf_rejected(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await c.get("/login")
            r = await c.post("/login", data={"username": "admin", "password": ADMIN_PWD},
                             follow_redirects=False)
            assert r.status_code == 400
            assert "页面已过期" in r.text
    _run(go())


def test_login_throttled_after_repeated_failures(env):
    """连续失败到上限后返回 429 与中文提示，阻止在线撞库。"""
    app, s = env

    async def go():
        async with _client(app) as c:
            await c.get("/login")
            csrf = c.cookies.get("pcc_csrf", "")
            for _ in range(s.login_max_fails):
                await c.post("/login", data={"_csrf": csrf, "username": "bob",
                                             "password": "bad-guess-1"}, follow_redirects=False)
            r = await c.post("/login", data={"_csrf": csrf, "username": "bob",
                                             "password": EMP_PWD}, follow_redirects=False)
            assert r.status_code == 429
            assert "过于频繁" in r.text

    try:
        _run(go())
    finally:
        # 限流器挂在 app.state 上，是 module 级共享的。
        # 不清理的话，后面用 bob 登录的用例会莫名其妙拿到 429 —— 这种
        # "测试互相污染"比测试失败本身更难查。
        app.state.throttle.reset("bob")


# --------------------------------------------------------------------------- #
# 页面权限
# --------------------------------------------------------------------------- #
def test_admin_page_redirects_to_login_when_anonymous(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await c.get("/admin", follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"].startswith("/login?next=")
            assert "%2Fadmin" in r.headers["location"] or "/admin" in r.headers["location"]
    _run(go())


def test_home_page_requires_login(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await c.get("/", follow_redirects=False)
            assert r.status_code == 303
            assert "/login" in r.headers["location"]
    _run(go())


def test_employee_cannot_open_admin(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await _login(c, "alice", EMP_PWD)
            assert r.status_code == 303
            assert r.headers["location"] == "/"          # 员工登录后去问答页
            r2 = await c.get("/admin", follow_redirects=False)
            assert r2.status_code == 403
            assert "没有权限" in r2.text
    _run(go())


def test_admin_can_open_dashboard(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login(c, "admin", ADMIN_PWD)
            r = await c.get("/admin")
            assert r.status_code == 200
            assert "索引状态" in r.text
            assert "概览" in r.text
    _run(go())


def test_logout_clears_session(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login(c, "admin", ADMIN_PWD)
            csrf = c.cookies.get("pcc_csrf", "")
            r = await c.post("/logout", data={"_csrf": csrf}, follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/login"
            r2 = await c.get("/admin", follow_redirects=False)
            assert r2.status_code == 303
    _run(go())


def test_open_redirect_blocked(env):
    """
    next=//evil.com 必须被忽略。浏览器会把 //host 当成协议相对地址，
    这是钓鱼最常用的一步：链接域名是我们自己的，落点却是别人的站。
    """
    app, _ = env

    async def go():
        async with _client(app) as c:
            await c.get("/login?next=//evil.com")
            csrf = c.cookies.get("pcc_csrf", "")
            r = await c.post("/login", data={
                "_csrf": csrf, "username": "admin", "password": ADMIN_PWD,
                "next": "//evil.com",
            }, follow_redirects=False)
            assert r.status_code == 303
            assert r.headers["location"] == "/admin"      # 回到站内，而不是 evil.com
    _run(go())


# --------------------------------------------------------------------------- #
# 接口权限与越权
# --------------------------------------------------------------------------- #
def test_chat_api_requires_login(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await c.post("/chat", json={"question": "冻结期内能改账户吗"},
                             headers={"Accept": "text/event-stream"})
            assert r.status_code == 401
            body = r.json()
            assert body["ok"] is False
            assert body["error"]["code"] == "AUTH_001"
            assert "登录" in body["error"]["message"]
    _run(go())


def test_cannot_read_others_session(env):
    """
    越权读他人会话必须返回 404 而不是 403：
    403 等于确认"这个 id 存在"，攻击者可以据此枚举有效会话。
    """
    app, s = env
    conn = db.connect(s.chat_db_path)
    try:
        alice = user_repo.get_by_username(conn, "alice")
        sess = chat_repo.create_session(conn, user_id=alice["id"],
                                        legal_entity_id="e1", title="爱丽丝的对话")
        sid = sess["session_id"]
    finally:
        conn.close()

    async def go():
        async with _client(app) as c:
            await _login(c, "bob", EMP_PWD)
            r = await c.get("/sessions/%s/messages" % sid)
            assert r.status_code == 404
            assert r.json()["error"]["code"] == "SES_001"
    _run(go())


def test_owner_can_read_own_session(env):
    app, s = env
    conn = db.connect(s.chat_db_path)
    try:
        alice = user_repo.get_by_username(conn, "alice")
        sid = chat_repo.create_session(conn, user_id=alice["id"],
                                       legal_entity_id="e1")["session_id"]
    finally:
        conn.close()

    async def go():
        async with _client(app) as c:
            await _login(c, "alice", EMP_PWD)
            r = await c.get("/sessions/%s/messages" % sid)
            assert r.status_code == 200
            assert r.json()["ok"] is True
    _run(go())


def test_admin_can_read_any_session(env):
    app, s = env
    conn = db.connect(s.chat_db_path)
    try:
        alice = user_repo.get_by_username(conn, "alice")
        sid = chat_repo.create_session(conn, user_id=alice["id"],
                                       legal_entity_id="e1")["session_id"]
    finally:
        conn.close()

    async def go():
        async with _client(app) as c:
            await _login(c, "admin", ADMIN_PWD)
            r = await c.get("/sessions/%s/messages" % sid)
            assert r.status_code == 200
    _run(go())


def test_healthz_reports_auth_state(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await c.get("/healthz")           # 健康检查不需要登录
            assert r.status_code == 200
            data = r.json()["data"]
            assert data["auth"]["required"] is True
    _run(go())


# --------------------------------------------------------------------------- #
# 开关：关掉鉴权等于回到阶段 2 的行为
# --------------------------------------------------------------------------- #
def test_auth_disabled_falls_back_to_demo_user(tmp_path):
    """AUTH_REQUIRED=0 时，员工页与后台都可匿名访问 —— 这是可回退的保证。"""
    from my_tool.api.app import create_app
    from my_tool.core.pipeline import build_index

    base = Settings.from_env()
    s = replace(base, index_dir=tmp_path / "index", chat_db_path=tmp_path / "chat.db",
                auth_required=False)
    build_index(s)
    app = create_app(s)

    async def go():
        async with _client(app) as c:
            r = await c.get("/admin")
            assert r.status_code == 200
            r2 = await c.get("/")
            assert r2.status_code == 200
            assert "制度问答" in r2.text
    _run(go())
