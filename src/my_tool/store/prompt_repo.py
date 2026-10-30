"""提示词模板的读写。版本不可变：只插入、只切换 is_active，绝不 UPDATE 历史行。

    这个约束看起来麻烦，但它换来的是"可复现"：
    上周那版答得对、这版答得不对时，把旧版重新设为 active 就能验证，
    而不是靠回忆"我当时改了哪一句"。既然 prompt 能把答对变成答错，
    没有回滚就等于没有保险。
"""

import sqlite3
from typing import Any

from .db import immediate, new_id, now_iso

DEFAULT_KEY = "rag_answer"


def get_active(conn: sqlite3.Connection, key: str = DEFAULT_KEY) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT * FROM prompt_templates
            WHERE key = ? AND is_active = 1
            ORDER BY version DESC LIMIT 1""",
        (key,),
    ).fetchone()
    return dict(row) if row else None


def get_version(conn: sqlite3.Connection, version: int,
                key: str = DEFAULT_KEY) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM prompt_templates WHERE key = ? AND version = ?",
        (key, version),
    ).fetchone()
    return dict(row) if row else None


def next_version(conn: sqlite3.Connection, key: str = DEFAULT_KEY) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS n FROM prompt_templates WHERE key = ?",
        (key,),
    ).fetchone()
    return int(row["n"]) if row else 1


def create_version(conn: sqlite3.Connection, *, system_prompt: str, user_template: str,
                   name: str = "", note: str = "", created_by: str | None = None,
                   activate: bool = True, key: str = DEFAULT_KEY) -> dict[str, Any]:
    """
    插入一个新版本。"同一 key 只有一个 active" 由同一个事务保证 ——
    分两步写（先清空再插入）如果中途失败，会留下"没有 active 模板"的库，
    表现为问答退回内置默认，而且没人知道为什么。
    """
    ts = now_iso()
    with immediate(conn):
        version = next_version(conn, key)
        if activate:
            conn.execute("UPDATE prompt_templates SET is_active = 0 WHERE key = ?", (key,))
        conn.execute(
            """INSERT INTO prompt_templates
               (id, key, version, name, system_prompt, user_template,
                is_active, note, created_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (new_id(), key, version, name or f"v{version}", system_prompt,
             user_template, 1 if activate else 0, note, created_by, ts),
        )
    return {"version": version, "created_at": ts, "is_active": activate}


def list_versions(conn: sqlite3.Connection, key: str = DEFAULT_KEY,
                  limit: int = 20) -> list[dict[str, Any]]:
    """历史版本，新的在前。列表里不需要正文全文，但页面要能预览，所以带上。"""
    rows = conn.execute(
        """SELECT id, version, name, is_active, note, created_by, created_at,
                  LENGTH(system_prompt) AS system_len,
                  LENGTH(user_template) AS user_len
             FROM prompt_templates WHERE key = ?
            ORDER BY version DESC LIMIT ?""",
        (key, max(1, min(limit, 100))),
    ).fetchall()
    return [dict(r) for r in rows]


def activate(conn: sqlite3.Connection, version: int, key: str = DEFAULT_KEY) -> bool:
    """回滚到指定版本。只切标记，不复制内容 —— 历史行永远保持原样。"""
    with immediate(conn):
        exists = conn.execute(
            "SELECT 1 FROM prompt_templates WHERE key = ? AND version = ?",
            (key, version)).fetchone()
        if not exists:
            return False
        conn.execute("UPDATE prompt_templates SET is_active = 0 WHERE key = ?", (key,))
        conn.execute(
            "UPDATE prompt_templates SET is_active = 1 WHERE key = ? AND version = ?",
            (key, version))
        return True


def count_versions(conn: sqlite3.Connection, key: str = DEFAULT_KEY) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM prompt_templates WHERE key = ?",
                       (key,)).fetchone()
    return int(row["n"]) if row else 0
