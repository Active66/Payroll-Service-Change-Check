"""会话与消息的读写。**全系统唯一写 SQL 的地方**（架构文档分层铁律：api/ 不许出现 SQL）。
"""

import json
import sqlite3
from typing import Any, Sequence

from .db import immediate, new_id, now_iso


# =============================================================================
# 【模块 16 · 会话仓储】目标文件：src/my_tool/store/chat_repo.py
# ★核心模块（阶段 3 复用）：函数签名按"业务动作"命名（begin_turn/finish_turn），
#   迁到 PG 时只换 SQL 方言，调用方不动。
#
#   一个刻意的取舍：**不引入 ORM**。这里只有 6 张表里的 2 张、10 来个查询，
#   ORM 带来的模型定义与懒加载语义，在这个规模上收益小于成本；
#   而 dataclass 实体（core/entities.py）已经承担了"类型安全"的职责。
# =============================================================================


def create_session(conn: sqlite3.Connection, *, user_id: str, legal_entity_id: str,
                   scene: str = "employee", title: str | None = None,
                   model: str = "") -> dict[str, Any]:
    """新建会话，返回 {"session_id", "created_at"}。"""
    sid = new_id()
    ts = now_iso()
    with immediate(conn):
        conn.execute(
            """INSERT INTO chat_sessions
               (id, user_id, legal_entity_id, scene, title, model,
                created_at, updated_at, last_message_at)
               VALUES (?,?,?,?,?,?,?,?,NULL)""",
            (sid, user_id, legal_entity_id, scene, title, model, ts, ts),
        )
    return {"session_id": sid, "created_at": ts, "scene": scene, "title": title}


def get_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
    r = cur.fetchone()
    return dict(r) if r else None


def has_streaming(conn: sqlite3.Connection, session_id: str) -> bool:
    """
    会话里是否有"正在生成"的回答。

    用数据本身当锁，而不是内存字典：`uvicorn --workers 4` 时字典不共享，
    进程重启锁也丢了；而这一行既能跨进程可见，又能在崩溃后被启动清理发现。
    """
    cur = conn.execute(
        "SELECT 1 FROM chat_messages WHERE session_id = ? AND answer_status = 'streaming' LIMIT 1",
        (session_id,),
    )
    return cur.fetchone() is not None


def _next_seq(conn: sqlite3.Connection, session_id: str) -> int:
    cur = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM chat_messages WHERE session_id = ?",
        (session_id,),
    )
    return int(cur.fetchone()["n"])


def begin_turn(conn: sqlite3.Connection, *, session_id: str, question: str,
               model: str = "", trace_id: str = "") -> dict[str, Any]:
    """
    一次提问的第一次落库（T1）：user 消息 + assistant 占位行 + 会话计数，同一个事务。

    为什么要先插一条空内容的 assistant 占位行：
      · 它是"该会话正在生成"的锁（并发控制）；
      · 流式过程中进程若崩溃，这一行会留在 streaming，启动清理能发现；
      · 生成失败时也有行可更新，而不是"用户看到了半截答案但库里什么都没有"。
    """
    ts = now_iso()
    with immediate(conn):
        user_seq = _next_seq(conn, session_id)
        cur = conn.execute(
            """INSERT INTO chat_messages (session_id, seq, role, content, created_at)
               VALUES (?,?,'user',?,?)""",
            (session_id, user_seq, question, ts),
        )
        user_id = cur.lastrowid

        asst_seq = user_seq + 1
        cur = conn.execute(
            """INSERT INTO chat_messages
               (session_id, seq, role, content, answer_status, model, trace_id, created_at)
               VALUES (?,?,'assistant','','streaming',?,?,?)""",
            (session_id, asst_seq, model, trace_id, ts),
        )
        asst_id = cur.lastrowid

        conn.execute(
            """UPDATE chat_sessions
                  SET message_count = message_count + 2,
                      last_message_at = ?, updated_at = ?,
                      model = COALESCE(NULLIF(model,''), ?),
                      title = COALESCE(title, ?)
                WHERE id = ?""",
            (ts, ts, model, question[:20], session_id),
        )
    return {"user_message_id": user_id, "user_seq": user_seq,
            "assistant_message_id": asst_id, "assistant_seq": asst_seq}


def finish_turn(conn: sqlite3.Connection, message_id: int, *, content: str,
                citations: Sequence[dict[str, Any]] | None = None,
                answer_status: str = "answered", token_in: int = 0, token_out: int = 0,
                latency_ms: int | None = None, model: str = "",
                prompt_version: str = "") -> None:
    """
    一次提问的第二次落库（T2）：把占位行更新成最终结果。

    这是独立短事务，放在生成结束之后 —— 生成期间不持锁，是这套设计能扛住并发的关键。

    prompt_version 记在消息上：提示词可以在后台随时改，
    一旦某条答案被质疑，必须能回答"当时用的是哪一版模板"。
    """
    # 允许直接传已序列化的 JSON 字符串：调用方从库里读出来再写回去时很容易这样用，
    # 而重复 dumps 会变成 "[{\"index\":...}]" 这样的双重编码 —— 存进去能过，
    # 读出来就是一堆字符，且只在审计页才暴露，排查成本极高。
    if isinstance(citations, str):
        citations_json = citations
    else:
        citations_json = json.dumps(list(citations or []), ensure_ascii=False)

    with immediate(conn):
        conn.execute(
            """UPDATE chat_messages
                  SET content = ?, citations = ?, answer_status = ?,
                      token_in = ?, token_out = ?, latency_ms = ?,
                      model = COALESCE(NULLIF(?,''), model),
                      prompt_version = COALESCE(NULLIF(?,''), prompt_version)
                WHERE id = ?""",
            (content, citations_json,
             answer_status, token_in, token_out, latency_ms, model, prompt_version,
             message_id),
        )
        # 会话累计 token（架构 3.3 的 token_used，用于配额）
        conn.execute(
            """UPDATE chat_sessions
                  SET token_used = token_used + ?
                WHERE id = (SELECT session_id FROM chat_messages WHERE id = ?)""",
            (token_in + token_out, message_id),
        )


def recent_history(conn: sqlite3.Connection, session_id: str, before_seq: int, *,
                   max_turns: int = 6, max_chars: int = 2400,
                   msg_max_chars: int = 400) -> list[dict[str, str]]:
    """
    取最近 N 轮对话（一轮 = user + assistant），供 prompt 使用。

    两个预算同时生效：轮数是产品语义（"最近 6 轮"），字符数是成本与窗口约束。
    实现上**从最新往最旧累计**，超预算就停 —— 这样丢掉的永远是最旧的，
    而不是"最旧的那轮被切一半"。最后反转成正序。

    `before_seq` 是当前这条用户消息的 seq，用它把自己排除掉
    （它已经作为独立 question 传给模型了，重复注入会让模型以为是两句话）。
    """
    rows = conn.execute(
        """SELECT seq, role, content, answer_status FROM chat_messages
            WHERE session_id = ? AND role IN ('user','assistant') AND seq < ?
            ORDER BY seq DESC LIMIT ?""",
        (session_id, before_seq, max(1, max_turns) * 2),
    ).fetchall()

    picked: list[dict[str, str]] = []
    total = 0
    for r in rows:
        content = (r["content"] or "").strip()
        if not content:
            continue                       # 占位行还没填内容（生成中断），不参与上下文
        if len(content) > msg_max_chars:
            content = content[:msg_max_chars] + "……"
        if picked and total + len(content) > max_chars:
            break
        total += len(content)
        picked.append({"role": r["role"], "content": content})

    picked.reverse()
    return picked


def list_messages(conn: sqlite3.Connection, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """按 seq 正序返回消息，供前端刷新页面后恢复。citations 从 JSON 字符串还原成对象。"""
    rows = conn.execute(
        """SELECT id, seq, role, content, citations, answer_status,
                  token_in, token_out, latency_ms, model, feedback, created_at
             FROM chat_messages WHERE session_id = ? ORDER BY seq ASC LIMIT ?""",
        (session_id, max(1, min(limit, 200))),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        try:
            item["citations"] = json.loads(item.get("citations") or "[]")
        except json.JSONDecodeError:
            item["citations"] = []
        out.append(item)
    return out


def set_feedback(conn: sqlite3.Connection, message_id: int, feedback: int) -> bool:
    """有用 / 无用。影响行数为 0 说明消息不存在，由 api 层翻译成 404。"""
    with immediate(conn):
        cur = conn.execute("UPDATE chat_messages SET feedback = ? WHERE id = ?",
                           (feedback, message_id))
        return cur.rowcount > 0


def message_owner(conn: sqlite3.Connection, message_id: int) -> dict[str, Any] | None:
    """
    取一条消息的归属信息（session_id + 会话归属人）。

    给"点反馈"这种只拿得到 message_id 的接口做越权校验用：
    没有它，谁都能拿一个自增 id 去改别人的反馈。
    """
    row = conn.execute(
        """SELECT m.id, m.session_id, s.user_id
             FROM chat_messages m
             JOIN chat_sessions s ON s.id = m.session_id
            WHERE m.id = ?""",
        (message_id,),
    ).fetchone()
    return dict(row) if row else None
