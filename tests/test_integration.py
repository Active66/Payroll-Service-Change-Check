"""阶段 4：版本台账、待生效、部门聚合、外部接口。

这些用例的共同点是"都测一个能出真事故的边界"，不是测覆盖率：
  · 换版不能出现"没有制度"的空窗；
  · 待生效版本不能被检索到；
  · 同 doc_id + 同版本重复上传必须拒绝；
  · 部门聚合的分组之和必须等于总量（差一条就说明有人掉出了统计）；
  · 外部接口没有 Key 必须 401，而且不区分"Key 错"和"没带 Key"。
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import date, timedelta

import httpx
import pytest

from my_tool.core.config import Settings
from my_tool.store import db, org_repo, policy_repo


# ---------------------------------------------------------------------------
# 夹具：所有测试在自己的临时目录里跑，不碰 data/chat.db
# ---------------------------------------------------------------------------

@pytest.fixture()
def s(tmp_path) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(
        base,
        chat_db_path=tmp_path / "chat.db",
        index_dir=tmp_path / "index",
        upload_dir=tmp_path / "uploads",
        auth_required=True,
    )


@pytest.fixture()
def conn(s):
    db.init_db(s.chat_db_path)
    c = db.connect(s.chat_db_path)
    yield c
    c.close()


def _mk_doc(conn, doc_id="POL-X", version=1, status="active",
            effective_from=None, title=None):
    """造一条文档记录。文件不必真存在 —— 这里测的是台账语义，不是摄入。"""
    row = policy_repo.publish_version(
        conn, doc_id=doc_id, title=title or f"制度 v{version}",
        source_file=f"{doc_id}-v{version}.md", storage_path=f"/tmp/{doc_id}-v{version}.md",
        checksum=f"ck-{version}", version=version, version_label=str(version),
        effective_from=effective_from)
    if status != "active":
        conn.execute("UPDATE policy_documents SET status = ? WHERE id = ?",
                     (status, row["id"]))
    return row


# ---------------------------------------------------------------------------
# 版本台账
# ---------------------------------------------------------------------------

def test_publish_new_version_supersedes_old(conn):
    _mk_doc(conn, version=1)
    _mk_doc(conn, version=2)

    versions = policy_repo.list_versions(conn, "POL-X")
    assert [v["version"] for v in versions] == [2, 1]
    status = {v["version"]: v["status"] for v in versions}
    assert status == {2: "active", 1: "superseded"}
    # 旧版留档，不是删掉：审计要能回答"上一版怎么写的"
    assert versions[1]["superseded_at"] is not None


def test_only_one_active_per_doc(conn):
    for v in (1, 2, 3):
        _mk_doc(conn, version=v)
    active = [d for d in policy_repo.list_versions(conn, "POL-X") if d["status"] == "active"]
    assert len(active) == 1
    assert active[0]["version"] == 3


def test_list_active_excludes_pending_effective(conn):
    future = (date.today() + timedelta(days=10)).isoformat()
    _mk_doc(conn, doc_id="POL-NOW", effective_from=None)
    _mk_doc(conn, doc_id="POL-FUTURE", effective_from=future)

    today = date.today().isoformat()
    active = policy_repo.list_active(conn, today=today)
    assert {d["doc_id"] for d in active} == {"POL-NOW"}
    # 但"待生效"必须能被后台看见，否则"传了却答不出来"无法排查
    assert [d["doc_id"] for d in policy_repo.list_pending(conn, today)] == ["POL-FUTURE"]


def test_rollback_reactivates_old_version(conn):
    _mk_doc(conn, version=1)
    _mk_doc(conn, version=2)

    assert policy_repo.reactivate(conn, "POL-X", 1) is True
    versions = {v["version"]: v for v in policy_repo.list_versions(conn, "POL-X")}
    assert versions[1]["status"] == "active"
    assert versions[2]["status"] == "superseded"
    # 已经 active 的版本再回滚一次是空操作，不该报错也不该产生新状态
    assert policy_repo.reactivate(conn, "POL-X", 1) is False


def test_delete_one_version_keeps_others(conn):
    _mk_doc(conn, version=1)
    _mk_doc(conn, version=2)                      # v1 变 superseded
    policy_repo.reactivate(conn, "POL-X", 1)      # 回滚：v1 active、v2 superseded

    assert policy_repo.soft_delete(conn, "POL-X", version=1) is True
    versions = {v["version"]: v for v in policy_repo.list_versions(conn, "POL-X")}
    assert versions[1]["status"] == "deleted"
    assert versions[2]["status"] == "superseded"   # 不因删 v1 而复活
    assert policy_repo.list_active(conn, today=date.today().isoformat()) == []


def test_get_document_prefers_active_version(conn):
    _mk_doc(conn, version=1)
    _mk_doc(conn, version=2)
    got = policy_repo.get_document(conn, "POL-X")
    assert got is not None and got["version"] == 2
    assert policy_repo.get_document(conn, "POL-X", 1)["status"] == "superseded"


def test_migration_from_single_pk_schema(tmp_path):
    """
    老库（doc_id 单主键）必须能原地迁到 (doc_id, version)。
    这是本次唯一"动已有数据"的改动，迁移写错的代价是整库制度丢失。
    """
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(str(path))
    c.executescript("""
    CREATE TABLE policy_documents (
        doc_id TEXT PRIMARY KEY, title TEXT NOT NULL,
        doc_type TEXT NOT NULL DEFAULT 'policy', version INTEGER NOT NULL DEFAULT 1,
        region_code TEXT NOT NULL DEFAULT '', effective_from TEXT, effective_to TEXT,
        source_file TEXT NOT NULL, storage_path TEXT NOT NULL, checksum TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active', chunk_count INTEGER NOT NULL DEFAULT 0,
        char_count INTEGER NOT NULL DEFAULT 0, uploaded_by TEXT, indexed_at TEXT,
        deleted_at TEXT, deleted_by TEXT, created_at TEXT NOT NULL
    );
    INSERT INTO policy_documents
      (doc_id,title,version,source_file,storage_path,checksum,status,chunk_count,created_at)
    VALUES ('POL-OLD','旧制度',1,'a.md','/tmp/a.md','ck','active',20,'2026-01-01T00:00:00+08:00');
    """)
    c.commit()
    c.close()

    db.init_db(path)
    c = db.connect(path)
    try:
        rows = [dict(r) for r in c.execute("SELECT * FROM policy_documents")]
        assert len(rows) == 1
        row = rows[0]
        assert (row["doc_id"], row["version"], row["chunk_count"]) == ("POL-OLD", 1, 20)
        assert row["version_label"] == "1" and row["id"]
        # 迁移后可以插同 doc_id 的新版本，说明唯一键确实换掉了
        policy_repo.publish_version(
            c, doc_id="POL-OLD", title="旧制度", source_file="b.md",
            storage_path="/tmp/b.md", checksum="ck2", version=2)
        assert len(policy_repo.list_versions(c, "POL-OLD")) == 2
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 组织
# ---------------------------------------------------------------------------

def test_department_tree_two_levels(conn):
    top = org_repo.create_department(conn, name="华东区域", code="EC")
    org_repo.create_department(conn, name="SSC上海", parent_id=top["id"])
    nodes = org_repo.tree(conn)
    assert len(nodes) == 1
    assert nodes[0]["name"] == "华东区域"
    assert [c["name"] for c in nodes[0]["children"]] == ["SSC上海"]


def test_usage_by_department_sums_to_total(conn):
    """分组之和必须等于总量。差一条就说明有会话掉出了统计视野。"""
    d1 = org_repo.create_department(conn, name="A部")
    org_repo.create_department(conn, name="B部")

    people = [
        ("u1", d1["id"]), ("u2", d1["id"]), ("u3", None), ("u4", "no-such-dept"),
    ]
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for uid, dept in people:
        conn.execute(
            """INSERT INTO users (id, username, display_name, role, password_hash,
                                  status, department_id, created_at, updated_at)
               VALUES (?,?,?, 'employee', 'x', 'active', ?, ?, ?)""",
            (uid, uid, uid, dept, ts, ts))
        conn.execute(
            """INSERT INTO chat_sessions (id, user_id, created_at, updated_at)
               VALUES (?,?,?,?)""", (f"s-{uid}", uid, ts, ts))
        for seq, status in ((1, "user"), (2, "answered"), (3, "user"), (4, "refused")):
            conn.execute(
                """INSERT INTO chat_messages (session_id, seq, role, content,
                                              answer_status, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (f"s-{uid}", seq, "user" if status == "user" else "assistant", "x",
                 "answered" if status == "user" else status, ts))

    rows = org_repo.usage_by_department(conn)
    total = org_repo.total_stats(conn)
    assert sum(r["questions"] for r in rows) == total["answers"] == 8
    assert sum(r["refused"] for r in rows) == total["refused"] == 4
    # 未归属（None 与无效 id）合成一组，名字明确写出来
    unassigned = [r for r in rows if not r["dept_id"]]
    assert len(unassigned) == 1, "未归属的问答必须并成一组，散成两行说明分组键不统一"
    assert unassigned[0]["dept_name"] == "未归属部门"
    assert unassigned[0]["questions"] == 4
    a_dept = next(r for r in rows if r["dept_id"] == d1["id"])
    assert a_dept["refuse_rate"] == 0.5


def test_assign_user_keeps_title_when_omitted(conn):
    d = org_repo.create_department(conn, name="A部")
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    conn.execute(
        """INSERT INTO users (id, username, display_name, role, password_hash, status,
                              department, created_at, updated_at)
           VALUES ('u1','u1','U1','employee','x','active','薪酬专员',?,?)""", (ts, ts))

    org_repo.assign_user(conn, "u1", d["id"], job_title="高级专员")
    org_repo.assign_user(conn, "u1", d["id"], job_title=None)   # 只调部门不该清岗位
    row = dict(conn.execute("SELECT department, department_id FROM users WHERE id='u1'").fetchone())
    assert row["department"] == "高级专员"
    assert row["department_id"] == d["id"]


# ---------------------------------------------------------------------------
# 外部接口
# ---------------------------------------------------------------------------

@pytest.fixture()
def api(tmp_path):
    """
    起一个真应用（含索引与鉴权），只把数据目录换成临时的。

    为什么不用 mock 的 orchestrator：外部接口最容易出问题的地方是
    "鉴权过了但答案结构不对"、"没鉴权也能拿到数据"这类接线问题，
    这些只有走完整装配才测得到。
    """
    from my_tool.api.app import create_app
    from my_tool.core.registry import register_document_file

    base = Settings.from_env()
    s = dataclasses.replace(
        base, chat_db_path=tmp_path / "chat.db", index_dir=tmp_path / "index",
        upload_dir=tmp_path / "uploads", auth_required=True,
        llm_provider="mock", embedding_provider="hash",
    )
    db.init_db(s.chat_db_path)
    register_document_file(s, base.doc_path)

    app = create_app(s)
    app.state.ctx.index_manager.rebuild_now(reason="test")
    return app, s


def _client(app) -> httpx.AsyncClient:
    # trust_env=False：本机 HTTP_PROXY 指向 127.0.0.1 的代理服务，
    # 走它会连不上 ASGI transport（阶段 2 踩过的坑）
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test", trust_env=False)


def _new_key(s) -> str:
    from my_tool.store import api_key_repo
    conn = db.connect(s.chat_db_path)
    try:
        raw, _ = api_key_repo.create_key(conn, name="test")
    finally:
        conn.close()
    return raw


def test_ask_without_key_is_401(api):
    app, _ = api

    async def go():
        async with _client(app) as c:
            r = await c.post("/api/v1/ask", json={"question": "冻结期是什么时候"})
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "AUTH_001"
            # 不带 Key 与 Key 错返回同一个码，不给枚举接口留缝
            r2 = await c.post("/api/v1/ask", json={"question": "冻结期是什么时候"},
                              headers={"Authorization": "Bearer pcc_wrong"})
            assert r2.status_code == 401
            assert r2.json()["error"]["code"] == "AUTH_001"
    asyncio.run(go())


def test_ask_returns_answer_with_citations(api):
    app, s = api
    key = _new_key(s)

    async def go():
        async with _client(app) as c:
            r = await c.post("/api/v1/ask", json={"question": "冻结期是什么时候"},
                             headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            assert data["status"] in ("answered", "partial", "refused")
            assert data["answer"]
            if data["status"] != "refused":
                assert data["citations"]
                cite = data["citations"][0]
                assert cite["doc_title"] and cite["clause_no"]
    asyncio.run(go())


def test_ask_rejects_empty_question(api):
    app, s = api
    key = _new_key(s)

    async def go():
        async with _client(app) as c:
            r = await c.post("/api/v1/ask", json={"question": "  "},
                             headers={"X-API-Key": key})
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "REQ_003"
    asyncio.run(go())


def test_revoked_key_stops_working(api):
    app, s = api
    from my_tool.store import api_key_repo
    key = _new_key(s)
    conn = db.connect(s.chat_db_path)
    try:
        rec = [k for k in api_key_repo.list_keys(conn) if k["prefix"] == key[:12]][0]
        api_key_repo.revoke(conn, rec["id"])
    finally:
        conn.close()

    async def go():
        async with _client(app) as c:
            r = await c.post("/api/v1/ask", json={"question": "冻结期"},
                             headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 401
    asyncio.run(go())


def test_rate_limit_returns_429(api):
    app, s = api
    from my_tool.store import api_key_repo
    conn = db.connect(s.chat_db_path)
    try:
        raw, rec = api_key_repo.create_key(conn, name="tight", rate_limit=1)
    finally:
        conn.close()

    async def go():
        async with _client(app) as c:
            h = {"Authorization": f"Bearer {raw}"}
            r1 = await c.get("/api/v1/healthz", headers=h)
            assert r1.status_code == 200
            r2 = await c.get("/api/v1/healthz", headers=h)
            assert r2.status_code == 429
            assert r2.json()["error"]["code"] == "SRV_002"
    asyncio.run(go())


def test_external_healthz_hides_internals(api):
    app, s = api
    key = _new_key(s)

    async def go():
        async with _client(app) as c:
            r = await c.get("/api/v1/healthz", headers={"Authorization": f"Bearer {key}"})
            data = r.json()["data"]
            assert data["ready"] is True
            assert data["chunk_count"] > 0
            # 库路径、密钥来源这种内部信息不该给外部系统看
            assert "db" not in data and "auth" not in data
    asyncio.run(go())


def test_key_hash_never_stored_in_plain(api):
    _, s = api
    from my_tool.store import api_key_repo
    raw = _new_key(s)
    conn = db.connect(s.chat_db_path)
    try:
        stored = [r["key_hash"] for r in api_key_repo.list_keys(conn)]
    finally:
        conn.close()
    assert raw not in stored
    assert all(len(h) == 64 for h in stored)
