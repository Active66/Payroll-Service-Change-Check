"""审计测试：预筛规则、筛选、标记与撤销、会话回放、权限。

两条原则在这组测试里被固定下来：
  · 自动预筛只提示、不落库（否则"确认违规"这个数字不可信）；
  · 标记是事件不是状态，可以撤销，历史留痕。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx
import pytest

from my_tool.core import security
from my_tool.core.audit_rules import assess
from my_tool.core.config import Settings
from my_tool.store import audit_repo, chat_repo, db, user_repo

ADMIN_PWD = "Admin12345"


# --------------------------------------------------------------------------- #
# 规则（纯函数，最容易写错也最容易测）
# --------------------------------------------------------------------------- #
def _cite(*snippets: str) -> list[dict]:
    return [{"index": i, "snippet": s} for i, s in enumerate(snippets, start=1)]


def test_r1_unverified_number_detected():
    """回答里的金额在引用片段里找不到 → 未溯源。这是幻觉最典型的形态。"""
    hits = assess("每月补贴 50000 元，随工资发放。",
                  _cite("每月补贴 500 元，随工资发放。"), "answered")
    codes = [h["code"] for h in hits]
    assert "R1" in codes


def test_r1_no_false_positive_when_number_sourced():
    hits = assess("冻结期内不接受任何变更。",
                  _cite("每月 25 日至月末为冻结期，不接受任何变更。"), "answered")
    assert "R1" not in [h["code"] for h in hits]


def test_r1_ignores_clause_numbers_without_units():
    """'第三条' 不是金额，不该被当成未溯源数字。"""
    hits = assess("依据第三条执行。", _cite("依据第三条执行。"), "answered")
    assert all(h["code"] != "R1" for h in hits)


def test_r3_hedge_words_detected():
    hits = assess("通常来说可以这样操作。", _cite("资料内容"), "answered")
    assert "R3" in [h["code"] for h in hits]


def test_r4_answer_without_citations():
    hits = assess("根据规定可以办理。", [], "answered")
    assert "R4" in [h["code"] for h in hits]


def test_clean_answer_has_no_flags():
    hits = assess("每月 25 日至月末为冻结期。[1]",
                  _cite("每月 25 日至月末为薪酬封版冻结期。"), "answered")
    assert hits == []


def test_refused_with_amount_flagged():
    hits = assess("制度中未找到相关规定，但一般来说补发金额是 300 元每月。", [], "refused")
    codes = [h["code"] for h in hits]
    assert "R2" in codes


# --------------------------------------------------------------------------- #
# 夹具：造几条会话与消息
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from my_tool.api.app import create_app
    from my_tool.core.pipeline import build_index

    tmp = tmp_path_factory.mktemp("audit")
    base = Settings.from_env()
    s = replace(base, index_dir=tmp / "index", chat_db_path=tmp / "chat.db",
                upload_dir=tmp / "up", auth_required=True,
                auth_secret="test-secret-for-jwt-do-not-use-in-prod",
                admin_seed_password=ADMIN_PWD)
    build_index(s)
    app = create_app(s)

    conn = db.connect(s.chat_db_path)
    try:
        # 一个员工 + 一条会话 + 一问一答（回答里故意留一个未溯源数字）
        if user_repo.get_by_username(conn, "alice") is None:
            user_repo.create_user(
                conn, username="alice", display_name="爱丽丝", role="employee",
                password_hash=security.hash_password("Alice12345"),
            )
        alice = user_repo.get_by_username(conn, "alice")
        sess = chat_repo.create_session(conn, user_id=alice["id"],
                                        legal_entity_id="e1", title="冻结期咨询")
        turn = chat_repo.begin_turn(conn, session_id=sess["session_id"],
                                    question="冻结期内可以改银行账户吗？", model="mock")
        chat_repo.finish_turn(
            conn, turn["assistant_message_id"],
            content="每月 25 日至月末为冻结期，冻结期内不接受变更，可受理 50000 元的补发。",
            citations=[
                {"index": 1, "doc_title": "员工薪酬变更核对制度",
                 "source_file": "员工薪酬变更核对制度_v1.0.md",
                 "clause_no": "第七条", "chunk_id": "c1",
                 "snippet": "每月 25 日至当月月末为薪酬封版冻结期，冻结期内不接受任何变更。",
                 "score": 0.9, "doc_id": "POL-CMP-2026-001", "heading_path": "第三章"},
            ],
            answer_status="answered", token_in=10, token_out=20, latency_ms=100,
            model="mock", prompt_version="tpl.v1",
        )
        info = {"session_id": sess["session_id"],
                "message_id": turn["assistant_message_id"],
                "user_message_id": turn["user_message_id"],
                "alice_id": alice["id"]}
    finally:
        conn.close()

    app.state.test_ids = info
    return app, s


def _run(coro):
    return asyncio.run(coro)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver", timeout=30)


async def _login_admin(client):
    await client.get("/login")
    csrf = client.cookies.get("pcc_csrf", "")
    r = await client.post("/login", data={"_csrf": csrf, "username": "admin",
                                          "password": ADMIN_PWD}, follow_redirects=False)
    assert r.status_code == 303
    return client.cookies.get("pcc_csrf", csrf)


# --------------------------------------------------------------------------- #
# 列表与筛选
# --------------------------------------------------------------------------- #
def test_audit_list_shows_flagged_answer(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.get("/admin/audit")
            assert r.status_code == 200
            assert "冻结期内可以改银行账户吗" in r.text
            # 造的数据里有 50000 元但引用里是 500 元 → 系统预筛应命中
            assert "未溯源" in r.text
    _run(go())


def test_audit_filter_by_employee(env):
    app, _ = env
    alice_id = app.state.test_ids["alice_id"]

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            hit = await c.get(f"/admin/audit?user_id={alice_id}")
            assert "冻结期内可以改银行账户吗" in hit.text
            miss = await c.get("/admin/audit?user_id=nobody")
            assert "冻结期内可以改银行账户吗" not in miss.text
            assert "没有符合条件的记录" in miss.text
    _run(go())


def test_audit_filter_by_date_excludes_future(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.get("/admin/audit?date_from=2099-01-01")
            assert "没有符合条件的记录" in r.text
    _run(go())


def test_audit_filter_by_keyword(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.get("/admin/audit?keyword=冻结期")
            assert "冻结期内可以改银行账户吗" in r.text
            r2 = await c.get("/admin/audit?keyword=完全不存在的词")
            assert "没有符合条件的记录" in r2.text
    _run(go())


def test_audit_risk_filter_only_shows_flagged(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.get("/admin/audit?risk=1")
            assert r.status_code == 200
            assert "冻结期内可以改银行账户吗" in r.text
    _run(go())


# --------------------------------------------------------------------------- #
# 标记
# --------------------------------------------------------------------------- #
def test_flag_and_revoke_history(env):
    """
    标记是事件：标"疑似"再改"清除"之后，两条记录都在，
    只有最新一条有效 —— 这样才能回答"谁在什么时候改过判断"。
    """
    app, s = env
    mid = app.state.test_ids["message_id"]
    sid = app.state.test_ids["session_id"]

    async def go(action: str):
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post(f"/admin/audit/messages/{mid}/flag", data={
                "_csrf": csrf, "flag": action, "session_id": sid,
                "next": f"/admin/audit/{sid}",
            }, follow_redirects=False)
            assert r.status_code == 303
    _run(go("suspect"))
    _run(go("confirmed"))
    _run(go("cleared"))

    conn = db.connect(s.chat_db_path)
    try:
        effective = audit_repo.latest_flags(conn, [mid])
        assert effective[mid]["flag"] == "cleared"
        history = audit_repo.flag_history(conn, mid)
        assert len(history) == 3
        assert sum(1 for h in history if h["revoked_at"]) == 2   # 前两条被撤销
    finally:
        conn.close()


def test_flag_unknown_message_404(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/audit/messages/999999/flag", data={
                "_csrf": csrf, "flag": "suspect",
            }, follow_redirects=False)
            assert r.status_code == 404
            assert "消息不存在" in r.text
    _run(go())


def test_flag_requires_csrf(env):
    app, _ = env
    mid = app.state.test_ids["message_id"]

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.post(f"/admin/audit/messages/{mid}/flag",
                             data={"flag": "suspect"}, follow_redirects=False)
            assert r.status_code == 400
            assert "页面已过期" in r.text
    _run(go())


# --------------------------------------------------------------------------- #
# 会话回放与权限
# --------------------------------------------------------------------------- #
def test_session_detail_shows_turns_and_citations(env):
    app, _ = env
    sid = app.state.test_ids["session_id"]

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.get(f"/admin/audit/{sid}")
            assert r.status_code == 200
            assert "提问" in r.text
            assert "回答" in r.text
            assert "引用来源" in r.text
            assert "tpl.v1" in r.text          # prompt 版本要能看到
    _run(go())


def test_session_detail_unknown_404(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.get("/admin/audit/nope")
            assert r.status_code == 404
            assert "不存在" in r.text
    _run(go())


def test_employee_cannot_access_audit(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await c.get("/login")
            csrf = c.cookies.get("pcc_csrf", "")
            await c.post("/login", data={"_csrf": csrf, "username": "alice",
                                         "password": "Alice12345"}, follow_redirects=False)
            r = await c.get("/admin/audit", follow_redirects=False)
            assert r.status_code == 403
            assert "没有权限" in r.text
    _run(go())


def test_dashboard_counts_pending_audits(env):
    """概览页的"待处理疑似违规"要和审计页对得上。"""
    app, s = env
    mid = app.state.test_ids["message_id"]

    conn = db.connect(s.chat_db_path)
    try:
        audit_repo.add_flag(conn, message_id=mid,
                            session_id=app.state.test_ids["session_id"],
                            flag="suspect", note="测试", auditor_id=None)
    finally:
        conn.close()

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.get("/admin")
            assert r.status_code == 200
            assert "待处理疑似违规" in r.text
    _run(go())
