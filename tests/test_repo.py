"""仓储层测试：表结构、seq 分配、历史窗口、并发判定、僵尸状态清理。

这些用例全部用临时 SQLite 文件，不碰 data/chat.db。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from my_tool.store import chat_repo, db


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "chat.db"
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


@pytest.fixture()
def session_id(conn) -> str:
    return chat_repo.create_session(conn, user_id="u1", legal_entity_id="e1")["session_id"]


def test_init_db_is_idempotent(tmp_path):
    """建表可以反复跑：每次启动都执行，不能因为"表已存在"就炸。"""
    path = tmp_path / "x.db"
    db.init_db(path)
    db.init_db(path)
    c = db.connect(path)
    try:
        names = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()
    assert {"chat_sessions", "chat_messages"} <= names


def test_create_session_fields(conn):
    r = chat_repo.create_session(conn, user_id="u1", legal_entity_id="e1", scene="employee")
    assert len(r["session_id"]) == 32          # uuid4().hex
    row = chat_repo.get_session(conn, r["session_id"])
    assert row["scene"] == "employee"
    assert row["message_count"] == 0
    assert row["deleted_at"] is None


def test_begin_turn_assigns_continuous_seq(conn, session_id):
    t1 = chat_repo.begin_turn(conn, session_id=session_id, question="第一问")
    assert (t1["user_seq"], t1["assistant_seq"]) == (1, 2)

    t2 = chat_repo.begin_turn(conn, session_id=session_id, question="第二问")
    assert (t2["user_seq"], t2["assistant_seq"]) == (3, 4)

    # 占位行必须是 streaming，这是"会话正忙"判定的依据
    assert chat_repo.has_streaming(conn, session_id) is True
    assert chat_repo.get_session(conn, session_id)["message_count"] == 4


def test_finish_turn_writes_result(conn, session_id):
    t = chat_repo.begin_turn(conn, session_id=session_id, question="冻结期内能改银行账户吗")
    chat_repo.finish_turn(
        conn, t["assistant_message_id"], content="可以受理，但不参与当期发放。[1]",
        citations=[{"index": 1, "chunk_id": "c1", "snippet": "…"}],
        answer_status="answered", token_in=10, token_out=20, latency_ms=123, model="mock",
    )
    assert chat_repo.has_streaming(conn, session_id) is False

    rows = chat_repo.list_messages(conn, session_id)
    assert len(rows) == 2
    assert rows[1]["answer_status"] == "answered"
    assert rows[1]["citations"][0]["chunk_id"] == "c1"      # JSON 字符串已还原成对象
    assert rows[1]["latency_ms"] == 123
    assert chat_repo.get_session(conn, session_id)["token_used"] == 30


def test_recent_history_returns_last_six_turns(conn, session_id):
    """造 8 轮，只应拿到最近 6 轮（12 条），且保持正序、排除当前这条。"""
    for i in range(8):
        t = chat_repo.begin_turn(conn, session_id=session_id, question=f"问题{i}")
        chat_repo.finish_turn(conn, t["assistant_message_id"], content=f"回答{i}",
                              answer_status="answered")

    current = chat_repo.begin_turn(conn, session_id=session_id, question="当前问题")
    hist = chat_repo.recent_history(conn, session_id, current["user_seq"],
                                   max_turns=6, max_chars=10000, msg_max_chars=400)

    assert len(hist) == 12
    assert hist[0] == {"role": "user", "content": "问题2"}     # 第 2 轮，最旧的被丢掉
    assert hist[-1] == {"role": "assistant", "content": "回答7"}
    assert all(h["content"] != "当前问题" for h in hist)        # 自己不进历史


def test_recent_history_respects_char_budget(conn, session_id):
    """字符预算从最旧开始丢，保证"最近一轮一定在"。"""
    for i in range(4):
        t = chat_repo.begin_turn(conn, session_id=session_id, question=f"Q{i}" + "x" * 50)
        chat_repo.finish_turn(conn, t["assistant_message_id"], content=f"A{i}" + "y" * 50,
                              answer_status="answered")

    current = chat_repo.begin_turn(conn, session_id=session_id, question="当前")
    hist = chat_repo.recent_history(conn, session_id, current["user_seq"],
                                   max_turns=6, max_chars=220, msg_max_chars=400)
    assert 0 < len(hist) < 8
    assert hist[-1]["content"].startswith("A3")                # 最新一条必须在


def test_recent_history_truncates_long_message(conn, session_id):
    t = chat_repo.begin_turn(conn, session_id=session_id, question="Q" * 1000)
    chat_repo.finish_turn(conn, t["assistant_message_id"], content="答", answer_status="answered")
    current = chat_repo.begin_turn(conn, session_id=session_id, question="当前")

    hist = chat_repo.recent_history(conn, session_id, current["user_seq"],
                                   max_turns=6, max_chars=99999, msg_max_chars=100)
    assert len(hist[0]["content"]) == 102                      # 100 字 + "……"


def test_cleanup_stale_streaming(tmp_path, conn, session_id):
    """进程被杀会留下 streaming 行，启动清理必须把它收尾，否则该会话永远 409。"""
    t = chat_repo.begin_turn(conn, session_id=session_id, question="会中断的问题")
    old = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(timespec="seconds")
    conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?",
                 (old, t["assistant_message_id"]))

    cleaned = db.cleanup_stale_streaming(tmp_path / "chat.db", older_than_minutes=10)
    assert cleaned == 1

    row = [r for r in chat_repo.list_messages(conn, session_id)
           if r["id"] == t["assistant_message_id"]][0]
    assert row["answer_status"] == "error"
    assert "服务中断" in row["content"]
    assert chat_repo.has_streaming(conn, session_id) is False


def test_set_feedback(conn, session_id):
    t = chat_repo.begin_turn(conn, session_id=session_id, question="q")
    chat_repo.finish_turn(conn, t["assistant_message_id"], content="a", answer_status="answered")

    assert chat_repo.set_feedback(conn, t["assistant_message_id"], -1) is True
    row = chat_repo.list_messages(conn, session_id)[-1]
    assert row["feedback"] == -1
    assert chat_repo.set_feedback(conn, 999999, 1) is False     # 不存在 → 让 api 层报 404


def test_citations_json_roundtrip(conn, session_id):
    """citations 列存 JSON 字符串，读出来必须是对象数组，不能是字符串。"""
    t = chat_repo.begin_turn(conn, session_id=session_id, question="q")
    cites = [{"index": 1, "doc_title": "员工薪酬变更核对制度", "source_file": "x.md"}]
    chat_repo.finish_turn(conn, t["assistant_message_id"], content="a",
                          citations=cites, answer_status="answered")
    rows = chat_repo.list_messages(conn, session_id)
    assert isinstance(rows[-1]["citations"], list)
    assert json.dumps(rows[-1]["citations"], ensure_ascii=False).find("员工薪酬") > 0
