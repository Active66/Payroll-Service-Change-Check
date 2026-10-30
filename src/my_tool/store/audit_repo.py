"""对话审计的查询与标记。

    审计要能回答三类问题，所以列表默认按"回答"为行（而不是按会话）：
      ① 某个员工什么时候问过什么；
      ② 哪几条回答看起来没依据（规则预筛 + 人工标记）；
      ③ 谁在什么时候把它标成了违规（message_audits 留痕，可撤销）。
"""

import sqlite3
from typing import Any

from .db import immediate, new_id, now_iso


def _filters(user_id: str, date_from: str, date_to: str, status: str,
             keyword: str) -> tuple[str, list[Any]]:
    """
    拼 WHERE 片段。所有值都走参数绑定 —— 审计页的关键词直接来自输入框，
    拼字符串就是给自己开一个 SQL 注入。
    """
    sql = ["m.role = 'assistant'", "m.answer_status != 'streaming'"]
    args: list[Any] = []
    if user_id:
        sql.append("s.user_id = ?")
        args.append(user_id)
    if date_from:
        sql.append("m.created_at >= ?")
        args.append(date_from + "T00:00:00")
    if date_to:
        sql.append("m.created_at <= ?")
        args.append(date_to + "T23:59:59")
    if status:
        sql.append("m.answer_status = ?")
        args.append(status)
    if keyword:
        # 问题也要能搜到：用 EXISTS 而不是再 JOIN 一次，避免把"取上一轮问题"的
        # 连接条件搅乱（两个 join 都基于 seq，混在一起很难读）
        sql.append("""(m.content LIKE ?
                       OR EXISTS (SELECT 1 FROM chat_messages q
                                   WHERE q.session_id = m.session_id AND q.role = 'user'
                                     AND q.seq < m.seq AND q.content LIKE ?))""")
        like = f"%{keyword}%"
        args.extend([like, like])
    return " AND ".join(sql), args


def list_answers(conn: sqlite3.Connection, *, user_id: str = "", date_from: str = "",
                 date_to: str = "", status: str = "", keyword: str = "",
                 page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    """
    返回 (当前页, 总数)。

    问题文本用相关子查询取"该回答之前最近的一条 user 消息"：
    比在 Python 里拼会话更省事，也不会因为 seq 断档而错位。
    """
    where, args = _filters(user_id, date_from, date_to, status, keyword)
    base = f"""
        FROM chat_messages m
        JOIN chat_sessions s ON s.id = m.session_id
        LEFT JOIN users u ON u.id = s.user_id
        LEFT JOIN chat_messages p
               ON p.session_id = m.session_id AND p.role = 'user'
              AND p.seq = (SELECT MAX(seq) FROM chat_messages
                            WHERE session_id = m.session_id AND role = 'user' AND seq < m.seq)
        WHERE {where}
    """
    total = int(conn.execute(f"SELECT COUNT(*) AS n {base}", args).fetchone()["n"])

    offset = max(0, (max(1, page) - 1) * page_size)
    rows = conn.execute(
        f"""SELECT m.id, m.session_id, m.seq, m.content, m.citations, m.answer_status,
                   m.token_in, m.token_out, m.latency_ms, m.model, m.prompt_version,
                   m.feedback, m.created_at,
                   s.user_id, u.display_name,
                   p.content AS question
            {base}
            ORDER BY m.created_at DESC, m.id DESC
            LIMIT ? OFFSET ?""",
        [*args, page_size, offset],
    ).fetchall()
    return [dict(r) for r in rows], total


def latest_flags(conn: sqlite3.Connection, message_ids: list[int]) -> dict[int, dict[str, Any]]:
    """
    批量取每条消息"最新的有效标记"。

    一次查完而不是逐行查：列表页 20 行如果每行一次查询，
    这个页面在数据多一点之后就会变成最慢的一页。
    """
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"""SELECT message_id, flag, note, reason, auditor_id, created_at
              FROM message_audits
             WHERE message_id IN ({marks}) AND revoked_at IS NULL
             ORDER BY created_at ASC""",
        message_ids,
    ).fetchall()
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        out[int(r["message_id"])] = dict(r)     # 后写的覆盖前面的 → 留最后一条
    return out


def add_flag(conn: sqlite3.Connection, *, message_id: int, session_id: str, flag: str,
             reason: str = "manual", note: str = "",
             auditor_id: str | None = None) -> dict[str, Any]:
    """
    追加一条标记。把之前的同 message 标记设为"已撤销"，保证"最新有效标记"唯一。

    为什么不 UPDATE 原记录：标记是事件，不是状态。谁在什么时候改过判断，
    事后追责和复盘都靠这条链。
    """
    ts = now_iso()
    with immediate(conn):
        conn.execute(
            """UPDATE message_audits SET revoked_at = ?, revoked_by = ?
                WHERE message_id = ? AND revoked_at IS NULL""",
            (ts, auditor_id, message_id),
        )
        mid = new_id()
        conn.execute(
            """INSERT INTO message_audits
               (id, message_id, session_id, flag, reason, note, auditor_id, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (mid, message_id, session_id, flag, reason, note, auditor_id, ts),
        )
    return {"id": mid, "message_id": message_id, "flag": flag, "created_at": ts}


def flag_history(conn: sqlite3.Connection, message_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT flag, reason, note, auditor_id, created_at, revoked_at
             FROM message_audits WHERE message_id = ?
            ORDER BY created_at DESC""",
        (message_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_session_messages(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """会话详情页：按 seq 正序，带引用原文与标记。"""
    rows = conn.execute(
        """SELECT id, seq, role, content, citations, answer_status, token_in, token_out,
                  latency_ms, model, prompt_version, feedback, trace_id, created_at
             FROM chat_messages WHERE session_id = ? ORDER BY seq ASC""",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def session_owner(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT s.id, s.user_id, s.title, s.created_at, s.message_count,
                  s.last_message_at, s.token_used, u.display_name
             FROM chat_sessions s LEFT JOIN users u ON u.id = s.user_id
            WHERE s.id = ?""",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


def list_employees(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """筛选下拉框用。只列出现过会话的人，避免下拉里塞满从未使用的人。"""
    rows = conn.execute(
        """SELECT DISTINCT u.id, u.display_name, u.username
             FROM users u
             JOIN chat_sessions s ON s.user_id = u.id
            ORDER BY u.display_name""",
    ).fetchall()
    return [dict(r) for r in rows]
