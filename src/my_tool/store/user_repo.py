"""用户表读写。与 chat_repo 同一条铁律：**全系统只有 store/ 写 SQL**。

    auth 层只负责"把密码变成哈希、把哈希变回判断"，不碰 SQL；
    CLI 与 Web 走同一套仓储函数，行为才不会分叉。
"""

import sqlite3
from typing import Any

from .db import immediate, new_id, now_iso

# =============================================================================
# 【模块 16b · 用户仓储】目标文件：src/my_tool/store/user_repo.py
#   为什么单独一个文件而不是塞进 chat_repo：会话是"员工用的数据"，
#   用户是"后台管的数据"，生命周期与变更权限完全不同（用户表只有 admin 能写）。
# =============================================================================


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    role: str,
    password_hash: str,
    employee_no: str | None = None,
    department: str | None = None,
) -> dict[str, Any]:
    """新建用户。username 冲突时由 SQLite 抛 IntegrityError，调用方翻译成中文提示。"""
    uid = new_id()
    ts = now_iso()
    with immediate(conn):
        conn.execute(
            """INSERT INTO users
               (id, username, display_name, employee_no, department, role,
                password_hash, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, 'active', ?, ?)""",
            (uid, username, display_name, employee_no, department, role,
             password_hash, ts, ts),
        )
    return {"id": uid, "username": username, "display_name": display_name,
            "role": role, "created_at": ts}


def get_by_username(conn: sqlite3.Connection, username: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_by_id(conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, username, display_name, employee_no, department, role,
                  status, last_login_at, created_at
             FROM users ORDER BY role DESC, username ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def update_password(conn: sqlite3.Connection, user_id: str, password_hash: str) -> bool:
    with immediate(conn):
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (password_hash, now_iso(), user_id),
        )
        return cur.rowcount > 0


def update_role(conn: sqlite3.Connection, user_id: str, role: str) -> bool:
    with immediate(conn):
        cur = conn.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
            (role, now_iso(), user_id),
        )
        return cur.rowcount > 0


def update_status(conn: sqlite3.Connection, user_id: str, status: str) -> bool:
    with immediate(conn):
        cur = conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_iso(), user_id),
        )
        return cur.rowcount > 0


def touch_login(conn: sqlite3.Connection, user_id: str) -> None:
    """记录最近登录时间。登录页会展示上次登录，异常时间点是个便宜的告警信号。"""
    with immediate(conn):
        conn.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
                     (now_iso(), now_iso(), user_id))


def count_users(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"]) if row else 0


def count_by_role(conn: sqlite3.Connection, role: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM users WHERE role = ? AND status = 'active'",
                       (role,)).fetchone()
    return int(row["n"]) if row else 0
