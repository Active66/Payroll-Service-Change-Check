"""外部系统 API Key 的签发与校验。

    为什么不复用 JWT：
      JWT 是**人**的凭证（8 小时过期、绑在浏览器 cookie 里），
      API Key 是**系统**的凭证（长期有效、绑在服务端配置里）。
      混用会造成两个具体问题：员工能把自己的 token 拿给外部系统用（责任边界糊掉），
      以及"禁用某人"和"停用某个集成"变成同一件事。

    存储：只存 sha256 哈希，明文仅在创建时返回一次。
      Key 是高熵随机串（32 字节），不存在"被爆破"的问题，所以不需要加盐慢哈希 ——
      pbkdf2 是为了对抗低熵口令，用在这里只会让每次请求多花 100ms。
"""

import hashlib
import secrets
import sqlite3
from typing import Any

from .db import immediate, new_id, now_iso

KEY_PREFIX = "pcc_"


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_key(conn: sqlite3.Connection, *, name: str, owner: str = "",
               rate_limit: int = 60) -> tuple[str, dict[str, Any]]:
    """签发一个 Key。返回 (明文, 记录)。明文**只能从这里拿到一次**。"""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    key_id = new_id()
    ts = now_iso()
    with immediate(conn):
        conn.execute(
            """INSERT INTO api_keys
               (id, name, owner, key_hash, prefix, rate_limit, status, created_at)
               VALUES (?,?,?,?,?,?, 'active', ?)""",
            (key_id, name, owner, hash_key(raw), raw[:12], int(rate_limit), ts),
        )
    return raw, {"id": key_id, "name": name, "prefix": raw[:12], "rate_limit": rate_limit}


def find_active(conn: sqlite3.Connection, raw: str) -> dict[str, Any] | None:
    """
    按明文找 Key。查不到返回 None —— 调用方对外统一回 AUTH_001，
    不区分"没带 Key / Key 错 / Key 被停用"，否则就成了一个枚举接口。
    """
    if not raw:
        return None
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash = ? AND status = 'active'",
        (hash_key(raw),)).fetchone()
    return dict(row) if row else None


def touch(conn: sqlite3.Connection, key_id: str) -> None:
    """记录最近使用时间。失败不影响请求：这是统计信息，不是业务数据。"""
    try:
        with immediate(conn):
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                         (now_iso(), key_id))
    except sqlite3.Error:
        pass


def revoke(conn: sqlite3.Connection, key_id: str) -> bool:
    with immediate(conn):
        cur = conn.execute(
            """UPDATE api_keys SET status = 'revoked', revoked_at = ?
                WHERE id = ? AND status = 'active'""", (now_iso(), key_id))
        return cur.rowcount > 0


def list_keys(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM api_keys ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]
