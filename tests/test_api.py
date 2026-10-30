"""接口层测试：不起端口，用 ASGITransport 直接打 ASGI 应用。

覆盖三块：
  · 正常链路：事件顺序、引用字段、落库结果；
  · 错误契约：每个错误码都断言 code + 中文文案 + HTTP 状态；
  · 多轮与并发：历史窗口真的被用上、同一会话并发被拦。

索引在临时目录里现建，不依赖 data/index（CI 里不会先跑 build）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from my_tool.core.config import Settings


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def api_env(tmp_path_factory):
    """建一份独立索引 + 独立会话库，返回 (app, settings)。

    auth_required=False：这一组测的是**问答链路**本身，不是鉴权。
    让它们带着登录态跑，等于给每个用例加噪音，还会让失败原因变模糊
    （"答不出" 和 "没登录" 都表现成 4xx）。鉴权单独在 test_auth.py 里测。
    """
    from my_tool.api.app import create_app
    from my_tool.core.pipeline import build_index

    tmp = tmp_path_factory.mktemp("api")
    base = Settings.from_env()
    s = replace(base, index_dir=tmp / "index", chat_db_path=tmp / "chat.db",
                auth_required=False)
    build_index(s)                      # 20 条切片，hash 向量，约 0.3s
    return create_app(s), s


@pytest.fixture(scope="module")
def app(api_env):
    return api_env[0]


@pytest.fixture(scope="module")
def settings(api_env) -> Settings:
    return api_env[1]


def _run(coro):
    """用 asyncio.run 包一层，避免为几个用例引入 pytest-asyncio 插件。"""
    return asyncio.run(coro)


def _parse_block(block: str):
    """解析一个 SSE 帧。返回 (event_name, data) 或 None。"""
    name = "message"
    data: list[str] = []
    for line in block.split("\n"):
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if not data:
        return None
    return name, json.loads("\n".join(data))


async def _collect_chat(client: httpx.AsyncClient, payload: dict):
    """发一次提问并收集全部事件。返回 (status_code, events)。"""
    events = []
    async with client.stream("POST", "/chat", json=payload) as resp:
        if resp.status_code != 200:
            await resp.aread()
            return resp.status_code, []
        buf = ""
        async for text in resp.aiter_text():
            buf += text
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                ev = _parse_block(block)
                if ev:
                    events.append(ev)
    return 200, events


def _events_by_name(events, name):
    return [d for n, d in events if n == name]


# --------------------------------------------------------------------------- #
# 正常链路
# --------------------------------------------------------------------------- #
def test_healthz_reports_index_and_db(app):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            return await client.get("/healthz")

    resp = _run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"]["index"]["ready"] is True
    assert body["data"]["index"]["chunk_count"] == 20
    assert body["data"]["db"]["ready"] is True
    assert body["data"]["llm"]["provider"] == "mock"


def test_chat_event_order_and_citations(app):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            return await _collect_chat(client, {"question": "冻结期内能改银行账户吗？"})

    status, events = _run(scenario())
    assert status == 200
    names = [n for n, _ in events]

    # 顺序契约：session → status(retrieving) → citations → status(generating) → delta… → done
    assert names[0] == "session"
    assert names[1] == "status"
    assert _events_by_name(events, "status")[0]["stage"] == "retrieving"
    assert names.index("citations") > 1
    assert names.index("citations") < names.index("delta")
    assert names[-1] == "done"

    cites = _events_by_name(events, "citations")[0]["citations"]
    assert 1 <= len(cites) <= 3
    first = cites[0]
    # 前端要展示"文件名 + 片段"，这两个字段缺一不可
    assert first["source_file"].endswith(".md")
    assert first["doc_title"]
    assert first["clause_no"]
    assert first["snippet"]
    assert first["chunk_id"].startswith("POL-")

    deltas = _events_by_name(events, "delta")
    assert len(deltas) >= 2                       # 至少两帧，证明是逐段吐的
    assert "".join(d["text"] for d in deltas).strip()

    done = _events_by_name(events, "done")[0]
    assert done["status"] in ("answered", "partial")
    assert done["usage"]["latency_ms"] >= 0
    assert done["trace_id"].startswith("tr_")
    assert done["citations_count"] == len(cites)


def test_chat_persists_messages_and_citations(app):
    """一轮问答后能从库里取回 2 条消息，且 assistant 的引用不是空数组。"""
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            _, events = await _collect_chat(client, {"question": "单次补发最多覆盖几个月？"})
            sid = _events_by_name(events, "session")[0]["session_id"]
            resp = await client.get(f"/sessions/{sid}/messages")
            return sid, resp

    sid, resp = _run(scenario())
    assert resp.status_code == 200
    data = resp.json()["data"]
    msgs = data["messages"]
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user" and msgs[0]["seq"] == 1
    assert msgs[1]["role"] == "assistant" and msgs[1]["seq"] == 2
    assert msgs[1]["answer_status"] in ("answered", "partial")
    assert isinstance(msgs[1]["citations"], list) and msgs[1]["citations"]


def test_refused_question_has_no_citations(app):
    """越界问题：不调模型、没有引用，但用 delta 吐一句固定话术（前端不必多写分支）。"""
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            return await _collect_chat(client, {"question": "报销流程怎么走？"})

    _, events = _run(scenario())
    assert _events_by_name(events, "citations") == []
    done = _events_by_name(events, "done")[0]
    assert done["status"] == "refused"
    assert done["citations_count"] == 0

    text = "".join(d["text"] for d in _events_by_name(events, "delta"))
    assert "无法确认" in text


def test_history_is_injected_on_second_turn(app):
    """
    第二轮要把第一轮带进 prompt，且短追问会触发"检索侧合并"。

    用 spy 包住编排器直接看实参，比断言最终答案更稳（mock 回答与历史无关）。
    """
    orch = app.state.ctx.orchestrator
    captured: list[dict] = []
    orig = orch.answer_stream

    def spy(question, **kw):
        captured.append({"question": question, "history": list(kw.get("history") or [])})
        yield from orig(question, **kw)

    orch.answer_stream = spy
    try:
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as client:
                _, first = await _collect_chat(client, {"question": "冻结期内能改银行账户吗？"})
                sid = _events_by_name(first, "session")[0]["session_id"]
                _, second = await _collect_chat(
                    client, {"session_id": sid, "question": "那超过 30 天呢？"})
                return first, second

        first, second = _run(scenario())
    finally:
        orch.answer_stream = orig

    assert len(captured) == 2
    # 第一轮没有历史；第二轮应该拿到 2 条（user + assistant）
    assert captured[0]["history"] == []
    assert [h["role"] for h in captured[1]["history"]] == ["user", "assistant"]
    # 生成时用的是当前问题原文，不是合并后的检索 query
    assert captured[1]["question"] == "那超过 30 天呢？"
    merged = _events_by_name(second, "done")[0]["query_used"]
    assert "银行账户" in merged and "30 天" in merged


# --------------------------------------------------------------------------- #
# 错误契约
# --------------------------------------------------------------------------- #
def _post_chat_status(app, payload):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            resp = await client.post("/chat", json=payload)
            return resp

    return _run(scenario())


@pytest.mark.parametrize("payload,code,status,keyword", [
    ({"question": "   "}, "REQ_003", 400, "不能为空"),
    ({"question": "问" * 501}, "REQ_004", 400, "过长"),
    ({"question": "正常问题", "session_id": "不是合法id"}, "REQ_005", 400, "会话标识"),
    ({"question": "正常问题", "session_id": "0" * 32}, "SES_001", 404, "会话不存在"),
    ({}, "REQ_002", 400, "question"),
])
def test_chat_error_contract(app, payload, code, status, keyword):
    """所有错误都必须是 {ok:false, error:{code,message,trace_id}} + 中文文案。"""
    resp = _post_chat_status(app, payload)
    assert resp.status_code == status
    body = resp.json()
    assert body["ok"] is False
    err = body["error"]
    assert err["code"] == code
    assert keyword in err["message"]
    assert err["trace_id"].startswith("tr_")
    # 文案里不能出现英文异常类名/表名这类内部信息
    for bad in ("Traceback", "sqlite", "KeyError", "NoneType"):
        assert bad not in err["message"]


def test_invalid_json_body(app):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            return await client.post("/chat", content=b"{not json",
                                     headers={"Content-Type": "application/json"})

    resp = _run(scenario())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "REQ_001"


def test_busy_session_returns_409(app, settings):
    """同一会话有 streaming 行时，第二个提问必须被拦住，而不是写出交错的 seq。"""
    from my_tool.store import chat_repo, db

    conn = db.connect(settings.chat_db_path)
    try:
        sid = chat_repo.create_session(conn, user_id="u", legal_entity_id="e")["session_id"]
        chat_repo.begin_turn(conn, session_id=sid, question="正在生成中的问题")
    finally:
        conn.close()

    resp = _post_chat_status(app, {"session_id": sid, "question": "插队的问题"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "SES_004"


def test_index_not_ready_returns_503(tmp_path):
    """索引缺失时 /chat 报 503，但 /healthz 仍返回 200 并说明原因。"""
    from my_tool.api.app import create_app

    s = replace(Settings.from_env(), index_dir=tmp_path / "missing",
                chat_db_path=tmp_path / "chat.db", auth_required=False)
    bad_app = create_app(s)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bad_app),
                                     base_url="http://t") as client:
            chat_resp = await client.post("/chat", json={"question": "任意问题"})
            health = await client.get("/healthz")
            return chat_resp, health

    chat_resp, health = _run(scenario())
    assert chat_resp.status_code == 503
    assert chat_resp.json()["error"]["code"] == "IDX_001"

    assert health.status_code == 200
    assert health.json()["data"]["index"]["ready"] is False
    assert health.json()["data"]["index"]["error"]


def test_unknown_route_uses_same_contract(app):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            return await client.get("/no-such-endpoint")

    resp = _run(scenario())
    assert resp.status_code == 404
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "REQ_006"


# --------------------------------------------------------------------------- #
# 会话与反馈
# --------------------------------------------------------------------------- #
def test_create_session_endpoint(app):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            return await client.post("/sessions", json={"title": "手工建的会话"})

    resp = _run(scenario())
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len(data["session_id"]) == 32
    assert data["title"] == "手工建的会话"


def test_feedback_ok_and_not_found(app):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as client:
            _, events = await _collect_chat(client, {"question": "降薪需要员工书面确认吗？"})
            mid = _events_by_name(events, "done")[0]["message_id"]
            good = await client.post(f"/messages/{mid}/feedback", json={"feedback": -1})
            missing = await client.post("/messages/999999/feedback", json={"feedback": 1})
            return good, missing

    good, missing = _run(scenario())
    assert good.status_code == 200 and good.json()["data"]["feedback"] == -1
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "MSG_001"
