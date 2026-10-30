"""知识库测试：摄入、PDF 转换、向量缓存、重建与热切换、页面与权限。

重点不是"接口返回 200"，而是三件容易坏的事：
  · 上传的切片真的能进索引、删掉的真的不再被召回；
  · 重建期间旧索引仍能服务（热切换不能有窗口期）；
  · 内容没变的切片不重复调用 embedding（缓存真的生效）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import httpx
import pytest

from my_tool.core.config import Settings
from my_tool.core.ingest import IngestError, decode_text, ingest, safe_filename, save_upload
from my_tool.core.pdf import PdfError, to_markdown
from my_tool.store import policy_repo

ADMIN_PWD = "Admin12345"

# 一份最小的"制度"，两个章节三条，够验证切片与条款识别
SAMPLE_MD = """---
doc_id: POL-TEST-999
title: 测试用加班补贴办法
version: 2
effective_from: 2026-01-01
---

# 测试用加班补贴办法

## 第一章 总则

### 第一条 适用范围

本办法适用于全体正式员工。试用期员工的加班补贴按转正后标准执行。

### 第二条 补贴标准

工作日加班每小时补贴 40 元，休息日加班每小时补贴 60 元，法定节假日每小时补贴 120 元。
补贴随当月工资一并发放，不计入年终奖基数。

## 第二章 申报

### 第三条 申报时限

加班须在次月 5 日前完成申报，逾期未申报的不予补发，特殊情况需部门负责人签批。
"""


# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def env(tmp_path_factory):
    from my_tool.api.app import create_app
    from my_tool.core.pipeline import build_index

    tmp = tmp_path_factory.mktemp("kb")
    base = Settings.from_env()
    s = replace(base, index_dir=tmp / "index", chat_db_path=tmp / "chat.db",
                upload_dir=tmp / "uploads", auth_required=True,
                auth_secret="test-secret-for-jwt-do-not-use-in-prod",
                admin_seed_password=ADMIN_PWD)
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


def _wait_rebuild(app, timeout: float = 30.0) -> dict:
    """等后台重建结束。后台线程跑的东西，测试必须显式等，不能靠 sleep 猜。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = app.state.ctx.rebuild_state
        if state.get("status") != "building":
            return state
        time.sleep(0.05)
    raise AssertionError("重建超时")


# --------------------------------------------------------------------------- #
# 单元：文件名、编码、PDF 转换
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expect", [
    ("../../etc/passwd", "passwd"),
    ("C:\\Windows\\system32\\x.md", "x.md"),
    ("正常文件名.md", "正常文件名.md"),
    ("", "unnamed"),
])
def test_safe_filename_strips_paths(raw, expect):
    """路径穿越必须被吃掉：上传接口本质上是个写文件接口。"""
    assert safe_filename(raw) == expect


def test_decode_text_gbk_fallback():
    """Windows 导出的制度常是 GBK，直接 utf-8 解码会炸掉整个上传。"""
    gbk = "第三条 申报时限".encode("gbk")
    assert decode_text(gbk) == "第三条 申报时限"


def test_decode_text_rejects_binary():
    with pytest.raises(IngestError):
        decode_text(b"\xff\xfe\x00\x01\x02\x03")


def test_pdf_to_markdown_recognises_clauses():
    text = "第三章 申报\n第五条 申报时限\n加班须在次月五日前申报。\n第六条 审批\n由部门负责人审批。"
    md, stats = to_markdown(text)
    assert stats["chapters"] == 1
    assert stats["clauses"] == 2
    assert "#### 第五条 申报时限" in md


def test_pdf_to_markdown_drops_page_numbers():
    text = "第五条 申报时限\n正文内容。\n- 12 -\n第 13 页"
    md, _ = to_markdown(text)
    assert "- 12 -" not in md
    assert "第 13 页" not in md


# --------------------------------------------------------------------------- #
# 单元：摄入与分片
# --------------------------------------------------------------------------- #
def test_ingest_markdown_chunks(settings, tmp_path):
    """md 摄入：front matter 生效、条款进切片、引用带 doc_id。"""
    s = replace(settings, upload_dir=tmp_path / "up")
    path = s.upload_dir / "sample.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SAMPLE_MD, encoding="utf-8")

    result = ingest(s, path, "sample.md")
    assert result["meta"]["doc_id"] == "POL-TEST-999"
    assert result["meta"]["title"] == "测试用加班补贴办法"
    assert result["meta"]["version"] == 2
    assert result["stats"]["chunks"] >= 1
    joined = "".join(c.content for c in result["chunks"])
    assert "40 元" in joined
    # 每片都要能自证出处，否则引用卡片显示不出来
    assert all(c.doc_id == "POL-TEST-999" for c in result["chunks"])
    assert all(c.source_file == "sample.md" for c in result["chunks"])


def test_ingest_rejects_unsupported_ext(settings, tmp_path):
    s = replace(settings, upload_dir=tmp_path / "up")
    with pytest.raises(IngestError) as ei:
        save_upload(s, "report.docx", b"whatever")
    assert ei.value.kind == "type"


def test_ingest_rejects_oversize(settings, tmp_path):
    s = replace(settings, upload_dir=tmp_path / "up")
    big = b"x" * (s.upload_max_mb * 1024 * 1024 + 10)
    with pytest.raises(IngestError) as ei:
        save_upload(s, "big.md", big)
    assert ei.value.kind == "size"


def test_pdf_scanned_detected(tmp_path):
    """扫描件必须明确报错，而不是产出一份空文档。"""
    from pypdf import PdfWriter

    from my_tool.core.pdf import extract

    p = tmp_path / "blank.pdf"
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    with open(p, "wb") as f:
        w.write(f)

    with pytest.raises(PdfError):
        extract(p)


# --------------------------------------------------------------------------- #
# 页面与上传
# --------------------------------------------------------------------------- #
def test_kb_page_requires_admin(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await c.get("/admin/kb", follow_redirects=False)
            assert r.status_code == 303
            assert "/login" in r.headers["location"]
    _run(go())


def test_upload_creates_document_and_slices(env):
    app, s = env

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/kb/upload",
                             files={"file": ("sample.md", SAMPLE_MD.encode("utf-8"),
                                             "text/markdown")},
                             data={"_csrf": csrf}, follow_redirects=False)
            assert r.status_code == 303
            assert "ok=" in r.headers["location"]

            # 列表页能看到
            page = await c.get("/admin/kb")
            assert "测试用加班补贴办法" in page.text
    _run(go())

    state = _wait_rebuild(app)
    assert state["status"] == "succeeded", state.get("error")

    # 索引里确实有这份文档的切片
    ctx = app.state.ctx
    assert any(c.doc_id == "POL-TEST-999" for c in ctx.orchestrator.retriever.store.chunks)


def test_upload_duplicate_doc_id_rejected(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/kb/upload",
                             files={"file": ("again.md", SAMPLE_MD.encode("utf-8"),
                                             "text/markdown")},
                             data={"_csrf": csrf}, follow_redirects=False)
            assert r.status_code == 303
            assert "err=" in r.headers["location"]
            assert "%E5%B7%B2%E5%AD%98%E5%9C%A8" in r.headers["location"]  # "已存在"
    _run(go())


def test_upload_rejects_bad_type(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/kb/upload",
                             files={"file": ("x.docx", b"binary", "application/octet-stream")},
                             data={"_csrf": csrf}, follow_redirects=False)
            assert r.status_code == 303
            assert "err=" in r.headers["location"]
    _run(go())


def test_upload_without_csrf_rejected(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.post("/admin/kb/upload",
                             files={"file": ("x.md", b"# t", "text/markdown")},
                             follow_redirects=False)
            # CSRF 校验失败 → 页面 400（不是静默写入）
            assert r.status_code == 400
            assert "页面已过期" in r.text
    _run(go())


def test_new_document_is_retrievable(env):
    """上传 + 重建之后，新制度的条款要能被检索到 —— 这是整条链路的最终验收。"""
    app, _ = env
    orch = app.state.ctx.orchestrator
    assert orch is not None
    res = orch.answer("工作日加班每小时补贴多少钱？")
    joined = "".join(c.snippet for c in res.citations) + res.answer
    assert "40 元" in joined or "加班" in joined
    assert res.status != "refused"


def test_doc_detail_shows_chunks(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            await _login_admin(c)
            r = await c.get("/admin/kb/POL-TEST-999")
            assert r.status_code == 200
            assert "切片预览" in r.text
            assert "40 元" in r.text
    _run(go())


def test_rebuild_swaps_orchestrator_atomically(env):
    """热切换：重建前后是不同对象，且切换后仍能正常问答。"""
    app, _ = env
    ctx = app.state.ctx
    before = ctx.orchestrator
    ctx.index_manager.rebuild_now(reason="test")
    after = ctx.orchestrator
    assert after is not before
    assert after is not None
    # 切换后仍能服务
    assert after.answer("申报时限是几天内？") is not None


def test_vector_cache_hit_avoids_reembedding(env):
    """
    第二次重建时，内容没变的切片应当全部命中缓存 ——
    否则"删一份文档"就要为整个库重付一次 embedding 费用。
    """
    app, s = env
    ctx = app.state.ctx

    # 先确保缓存是热的
    ctx.index_manager.rebuild_now(reason="warm")
    from my_tool.core.index_manager import VectorCache
    from my_tool.core.retrieval.embedding import build_embedding_model
    from my_tool.utils.text import sha256_text

    dim = build_embedding_model(s).dim
    cache = VectorCache.load(s.index_dir, dim)
    chunks = ctx.orchestrator.retriever.store.chunks
    assert len(chunks) > 0
    hits = sum(1 for c in chunks if cache.get(sha256_text(c.content)[:32]) is not None)
    assert hits == len(chunks), f"缓存命中 {hits}/{len(chunks)}，说明有切片没被复用"


def test_rebuild_busy_returns_conflict(env):
    """重建期间再次触发要明确拒绝，而不是排队再跑一遍。"""
    app, _ = env
    mgr = app.state.ctx.index_manager
    from my_tool.core.index_manager import RebuildBusy

    busy_raised = False
    # 直接占用内部锁模拟"正在重建"
    assert mgr._lock.acquire(blocking=False)
    try:
        try:
            mgr.trigger(reason="test")
        except RebuildBusy:
            busy_raised = True
    finally:
        mgr._lock.release()
    assert busy_raised


def test_healthz_reports_rebuild_state(env):
    app, _ = env

    async def go():
        async with _client(app) as c:
            r = await c.get("/healthz")
            assert r.status_code == 200
            assert "rebuild" in r.json()["data"]["index"]
    _run(go())


def test_manifest_lists_all_documents(env):
    """多文档时 manifest 要能列全，否则运维看不出库里有几份制度。"""
    app, _ = env
    m = app.state.ctx.manifest
    assert "documents" in m
    assert m["doc_count"] >= 1
    assert m["chunk_count"] == len(app.state.ctx.orchestrator.retriever.store.chunks)


def test_delete_removes_from_index(env):
    """
    删除后必须不再被召回。软删 + 重建才能真正做到这一点。

    放在最后：它会把库清空，放前面会让后面依赖"库里有文档"的用例
    集体失败 —— 测试之间的顺序依赖要显式写在注释里，否则后人只会看到
    一堆莫名其妙的 assert 0 >= 1。
    """
    app, s = env

    async def go():
        async with _client(app) as c:
            csrf = await _login_admin(c)
            r = await c.post("/admin/kb/POL-TEST-999/delete",
                             data={"_csrf": csrf, "confirm": "1"},
                             follow_redirects=False)
            assert r.status_code == 303
            assert "ok=" in r.headers["location"]
    _run(go())

    state = _wait_rebuild(app)
    assert state["status"] == "succeeded", state.get("error")

    ctx = app.state.ctx
    assert not any(c.doc_id == "POL-TEST-999" for c in ctx.orchestrator.retriever.store.chunks)

    import sqlite3
    conn = sqlite3.connect(str(s.chat_db_path))
    conn.row_factory = sqlite3.Row
    try:
        doc = policy_repo.get_document(conn, "POL-TEST-999")
        assert doc["status"] == "deleted"
    finally:
        conn.close()
