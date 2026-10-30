"""变更单的读写。

    状态机刻意只有五个状态，且**只允许单向流转**：

        draft ──submit──► submitted ──► approved
          │                   └──────► rejected
          └──（规则阻断时）──► blocked

    为什么不做"关闭后编辑"（第二十九条明确禁止）：一旦允许编辑，
    "这张单当时是按什么规则核准的"就不可复现了。修正必须新建单并引用原单号，
    形成追溯链 —— 这条在数据上体现为 `supersedes_order_no` 字段。

    幂等键：Agent 会因超时重试，没有幂等键就会建出两张内容相同的单。
    `idempotency_key` 上有 UNIQUE，重复插入会返回已存在的那张而不是报错。
"""

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any

from .db import immediate, new_id, now_iso


def _gen_order_no() -> str:
    """
    单号：CC + 年月 + 6 位随机。

    不用自增序号：并发下取 max+1 会撞，而加锁的代价比"单号不连续"大得多。
    业务上没人要求单号连续。
    """
    return f"CC{datetime.now():%Y%m}{uuid.uuid4().hex[:6].upper()}"


def create_draft(
    conn: sqlite3.Connection, *,
    employee_no: str,
    change_type: str,
    items: list[dict[str, Any]],
    materials: list[str] | None = None,
    risk_level: str | None = None,
    blocking: list[dict[str, Any]] | None = None,
    validation_jti: str = "",
    idempotency_key: str,
    created_by: str = "",
    status: str = "draft",
    supersedes_order_no: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """
    建单。返回 (记录, 是否新建)。

    幂等：`idempotency_key` 命中已有单时直接返回那条，`created=False`。
    调用方据此告诉模型"这张单已经建过了"，避免它以为失败又建一次。
    """
    if idempotency_key:
        existing = conn.execute(
            "SELECT * FROM change_orders WHERE idempotency_key = ?",
            (idempotency_key,)).fetchone()
        if existing:
            return _row(existing), False

    ts = now_iso()
    order_id = new_id()
    order_no = _gen_order_no()
    with immediate(conn):
        conn.execute(
            """INSERT INTO change_orders
               (id, order_no, employee_no, change_type, status, risk_level, items,
                materials, blocking, validation_jti, idempotency_key,
                supersedes_order_no, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, order_no, employee_no, change_type, status, risk_level,
             json.dumps(items, ensure_ascii=False),
             json.dumps(materials or [], ensure_ascii=False),
             json.dumps(blocking or [], ensure_ascii=False),
             validation_jti or None, idempotency_key or None,
             supersedes_order_no or None, created_by, ts, ts))
    row = conn.execute("SELECT * FROM change_orders WHERE id = ?", (order_id,)).fetchone()
    return _row(row), True


def _row(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("items", "materials", "blocking"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except json.JSONDecodeError:
            d[key] = []
    return d


def get_by_idempotency(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """
    按幂等键取单。

    **这个查询必须发生在验签之前**：Agent 重试的时候，token 可能已经被
    上一次调用消费掉了（一次性凭证的正常代价）。如果先验签，重试会得到
    "凭证已使用"，Agent 就会以为建单失败，然后换个幂等键再建一次 ——
    结果是两张一样的单。先查幂等键，重试就变成"返回上次那张"，语义正确。
    """
    if not key:
        return None
    row = conn.execute("SELECT * FROM change_orders WHERE idempotency_key = ?",
                       (key,)).fetchone()
    return _row(row) if row else None


def get(conn: sqlite3.Connection, *, order_id: str = "", order_no: str = ""
        ) -> dict[str, Any] | None:
    if order_id:
        row = conn.execute("SELECT * FROM change_orders WHERE id = ?",
                           (order_id,)).fetchone()
    elif order_no:
        row = conn.execute("SELECT * FROM change_orders WHERE order_no = ?",
                           (order_no,)).fetchone()
    else:
        return None
    return _row(row) if row else None


def submit(conn: sqlite3.Connection, order_id: str, *, by: str = "") -> bool:
    """
    草稿 → 已提交。**只允许从 draft 流转**，重复提交返回 False 而不是报错。

    用 `WHERE status = 'draft'` 做条件更新，而不是"先查再改"：
    两个并发请求同时提交，条件更新只有一个能成功。
    """
    ts = now_iso()
    with immediate(conn):
        cur = conn.execute(
            """UPDATE change_orders SET status = 'submitted', submitted_at = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'draft'""",
            (ts, ts, order_id))
        return cur.rowcount > 0


def list_by_employee(conn: sqlite3.Connection, employee_no: str,
                     change_type: str | None = None, since: str | None = None,
                     statuses: tuple[str, ...] | None = None,
                     limit: int = 20) -> list[dict[str, Any]]:
    sql = "SELECT * FROM change_orders WHERE employee_no = ?"
    params: list[Any] = [employee_no]
    if change_type:
        sql += " AND change_type = ?"
        params.append(change_type)
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if statuses:
        marks = ",".join("?" * len(statuses))
        sql += f" AND status IN ({marks})"
        params.extend(statuses)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return [_row(r) for r in conn.execute(sql, params).fetchall()]


def count(conn: sqlite3.Connection, status: str | None = None) -> int:
    if status:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM change_orders WHERE status = ?",
            (status,)).fetchone()["n"])
    return int(conn.execute("SELECT COUNT(*) AS n FROM change_orders").fetchone()["n"])
