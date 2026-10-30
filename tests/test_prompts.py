"""提示词模板测试：seed、保存即生效、占位符校验、回滚、预览、版本落库。

这组测试的重点是"改了 prompt 之后，下一个问题真的用新版" ——
这是需求里的硬要求（存数据库，不写死在代码里），也是最容易做成
"存了但没生效"的地方。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from my_tool.core.prompt import BUILTIN_TEMPLATE, DEFAULT_SYSTEM_PROMPT, validate_template
from my_tool.core.config import Settings
from my_tool.store import prompt_repo

ADMIN_PWD = "Admin12345"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from my_tool.api.app import create_app
    from my_tool.core.pipeline import build_index

    tmp = tmp_path_factory.mktemp("prompt")
    base = Settings.from_env()
    s = replace(base, index_dir=tmp / "index", chat_db_path=tmp / "chat.db",
                upload_dir=tmp / "up", auth_required=True,
                auth_secret="test-secret-for-jwt-do-not-use-in-prod",
                admin_seed_password=ADMIN_PWD, prompt_cache_seconds=0.0)
    build_index(s)
    app = create_app(s)
    return app, s


def _run(coro):
    return asyncio.run(coro)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver", timeout=60)


async def _login_admin(client: httpx.AsyncClient):
    await client.get("/login")
    csrf = client.cookies.get("pcc_csrf", "")
    r = await client.post("/login", data={"_csrf": csrf, "username": "admin",
                                          "password": ADMIN_PWD}, follow_redirects=False)
    assert r.status_code == 303
    return client.cookies.get("pcc_csrf", csrf)


# --------------------------------------------------------------------------- #
# 校验逻辑
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("system,user,ok", [
    (DEFAULT_SYSTEM_PROMPT, "【资料】{context}\n【问题】{question}", True),
    (DEFAULT_SYSTEM_PROMPT, "【资料】{context}", False),          # 缺 {question}
    (DEFAULT_SYSTEM_PROMPT, "【问题】{question}", False),        # 缺 {context}
    ("短", "【资料】{context}\n【问题】{question}", False),        # system 过短
])
def test_validate_template(system, user, ok):
    """
    缺占位符必须在保存时拦住：少了 {context}，模型只会拿到问题、没有资料，
    然后开始编 —— 而这发生在员工页，管理员完全不知道自己改坏了什么。
    """
    assert (validate_template(system, user) == "") is ok


def test_builtin_template_is_fallback():
    assert BUILTIN_TEMPLATE.version == 0
    assert BUILTIN_TEMPLATE.label.startswith("builtin")


# --------------------------------------------------------------------------- #
# seed 与页面
# --------------------------------------------------------------------------- #
def test_default_template_seeded(env):
    _, s = env
    from my_tool.store import db

    conn = db.connect(s.chat_db_path)
    try:
        active = prompt_repo.get_active(conn)
        assert active is not None
        assert active["version"] == 1
        assert "薪酬核对制度助手" in active["system_prompt"]
        assert "{context}" in active["user_template"]
    finally:
        conn.close()


def test_prompts_page_renders(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await c.get("/admin/prompts", follow_redirects=False)
            assert r.status_code == 303          # 未登录 → 跳登录

            await _login_admin(c)
            r2 = await c.get("/admin/prompts")
            assert r2.status_code == 200
            assert "系统提示词" in r2.text
            assert "系统强制追加" in r2.text       # 不可编辑那块要显示出来
            assert "{context}" in r2.text
    _run(go())


# --------------------------------------------------------------------------- #
# 保存 → 生效
# --------------------------------------------------------------------------- #
NEW_SYSTEM = """你是薪酬制度问答助手。只依据给出的制度条文回答，资料没有的就回答"制度中未找到"。
不得引述外部常识，不得编造任何金额与比例。"""


def test_save_creates_new_version(env):
    app, s = env
    from my_tool.store import db

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/prompts", data={
                "_csrf": csrf, "system_prompt": NEW_SYSTEM,
                "user_template": "【资料】\n{context}\n\n【问题】\n{question}",
                "note": "测试：换一版更短的 system",
            }, follow_redirects=False)
            assert r.status_code == 303
            assert "ok=" in r.headers["location"]
    _run(go())

    conn = db.connect(s.chat_db_path)
    try:
        active = prompt_repo.get_active(conn)
        assert active["version"] == 2
        assert active["system_prompt"] == NEW_SYSTEM
        versions = prompt_repo.list_versions(conn)
        assert len(versions) == 2
        # 旧版本仍在，只是不再生效 —— 版本不可变
        assert any(v["version"] == 1 and not v["is_active"] for v in versions)
    finally:
        conn.close()


def test_new_prompt_takes_effect_immediately(env):
    """
    保存后**下一个问题**就该用新版：答案的 prompt_version 要变成 tpl.v2。

    这是"存数据库不写死代码"的验收点。provider 有缓存，所以这里同时验证
    invalidate 起作用 —— 缓存没失效的话会一直拿到 builtin 或旧版本。
    """
    app, _ = env
    orch = app.state.ctx.orchestrator
    res = orch.answer("冻结期内可以改银行账户吗？")
    assert res.prompt_version == "tpl.v2"


def test_prompt_version_persisted_to_message(env):
    """prompt_version 要落进 chat_messages：答案出问题时必须能追溯当时用的哪版。"""
    app, s = env

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.post("/chat", json={"question": "单次补发最多覆盖几个月？"},
                             headers={"Accept": "text/event-stream"})
            assert r.status_code == 200
            return r.text
    body = _run(go())
    assert "event: done" in body
    assert '"prompt_version"' in body
    assert "tpl.v2" in body

    import sqlite3
    conn = sqlite3.connect(str(s.chat_db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT prompt_version FROM chat_messages WHERE role='assistant' "
            "AND content != '' ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        assert row["prompt_version"] == "tpl.v2"
    finally:
        conn.close()


def test_missing_placeholder_rejected(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/prompts", data={
                "_csrf": csrf, "system_prompt": NEW_SYSTEM,
                "user_template": "只放资料：{context}",      # 缺 {question}
            }, follow_redirects=False)
            assert r.status_code == 303
            assert "err=" in r.headers["location"]
    _run(go())


def test_rollback_activates_old_version(env):
    app, s = env
    from my_tool.store import db

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/prompts/1/activate", data={"_csrf": csrf},
                             follow_redirects=False)
            assert r.status_code == 303
            assert "ok=" in r.headers["location"]
    _run(go())

    conn = db.connect(s.chat_db_path)
    try:
        active = prompt_repo.get_active(conn)
        assert active["version"] == 1
    finally:
        conn.close()

    # 回滚后下一个问题就用 v1
    assert app.state.ctx.orchestrator.answer("冻结期内能改银行账户吗").prompt_version == "tpl.v1"


def test_activate_unknown_version_404(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/prompts/999/activate", data={"_csrf": csrf},
                             follow_redirects=False)
            assert r.status_code == 404
            assert "模板版本不存在" in r.text
    _run(go())


# --------------------------------------------------------------------------- #
# 预览
# --------------------------------------------------------------------------- #
def test_preview_runs_without_saving(env):
    """预览必须真跑一次检索+生成，但不能改数据库。"""
    app, s = env
    from my_tool.store import db

    conn = db.connect(s.chat_db_path)
    try:
        before = prompt_repo.count_versions(conn)
    finally:
        conn.close()

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/prompts/preview", data={
                "_csrf": csrf,
                "system_prompt": "你是薪酬制度助手，只依据资料回答，没有依据就说制度中未找到。",
                "user_template": "【资料】\n{context}\n\n【问题】\n{question}",
                "question": "冻结期内可以改银行账户吗？",
            })
            assert r.status_code == 200
            assert "预览结果" in r.text
            assert "护栏状态" in r.text or "没有召回" in r.text
    _run(go())

    conn = db.connect(s.chat_db_path)
    try:
        assert prompt_repo.count_versions(conn) == before   # 预览没有新增版本
    finally:
        conn.close()


def test_citation_rules_are_appended(env):
    """
    系统强制追加的引用规则必须进 system 消息。
    没有它，模型不会标 [1][2]，护栏会把每条答案都判成"引用缺失"。
    """
    app, _ = env
    orch = app.state.ctx.orchestrator
    hits, _ = orch.retriever.retrieve("冻结期内可以改银行账户吗", top_k=3)
    assert hits
    from my_tool.core.prompt import CITATION_RULES, build_prompt
    messages, _ctx, _ver = build_prompt("冻结期内可以改银行账户吗", hits)
    assert CITATION_RULES.strip()[:12] in messages[0]["content"]
    assert messages[0]["role"] == "system"
