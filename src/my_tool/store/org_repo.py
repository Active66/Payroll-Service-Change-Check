"""部门表与"按部门看问答"的聚合查询。

    只做两级（parent_id 为 NULL 即一级）。SSC 要回答的是
    "华东区这个月多少人在问工资冻结期、拒答率多少"，
    两级足够；更深的组织树属于 HR 主数据，应该对接而不是自己造。

    聚合口径说明（重要，否则数字会被误读）：
      · 分母是**提问次数**，不是人数。一个人问 10 次算 10 次；
        要把"人次"和"人数"分开，得加 DISTINCT，两种口径都得能看。
      · 拒答率 = refused / (answered + partial + refused)，排除还在 streaming 的占位行。
      · 分组键用**部门表的 id**而不是 users.department_id：
        用户表里可能留着已被删掉的部门 id（悬挂引用），拿它分组会得到
        两行都叫"未归属部门"、数字还各算一半。
"""

import sqlite3
from typing import Any

from .db import immediate, new_id, now_iso


def create_department(conn: sqlite3.Connection, *, name: str, parent_id: str | None = None,
                      code: str = "", sort_order: int = 0) -> dict[str, Any]:
    dept_id = new_id()
    with immediate(conn):
        conn.execute(
            """INSERT INTO departments (id, name, parent_id, code, sort_order, created_at)
               VALUES (?,?,?,?,?,?)""",
            (dept_id, name.strip(), parent_id or None, code.strip(), sort_order, now_iso()),
        )
    return {"id": dept_id, "name": name, "parent_id": parent_id}


def list_departments(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM departments
            ORDER BY sort_order ASC, name ASC""").fetchall()
    return [dict(r) for r in rows]


def tree(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """两级树。缺父节点的挂到根上，而不是悄悄丢掉——丢了就是"这个部门的人去哪了"。"""
    depts = list_departments(conn)
    by_id = {d["id"]: {**d, "children": []} for d in depts}
    roots: list[dict[str, Any]] = []
    for d in depts:
        node = by_id[d["id"]]
        parent = by_id.get(d["parent_id"]) if d["parent_id"] else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    return roots


def assign_user(conn: sqlite3.Connection, user_id: str, department_id: str | None,
                job_title: str | None = None) -> bool:
    """把用户挂到部门上。job_title 传了才改，避免"只想调部门却把岗位清空"。"""
    with immediate(conn):
        if job_title is None:
            cur = conn.execute(
                "UPDATE users SET department_id = ?, updated_at = ? WHERE id = ?",
                (department_id or None, now_iso(), user_id))
        else:
            cur = conn.execute(
                """UPDATE users SET department_id = ?, department = ?, updated_at = ?
                    WHERE id = ?""",
                (department_id or None, job_title, now_iso(), user_id))
        return cur.rowcount > 0


def usage_by_department(conn: sqlite3.Connection, since: str | None = None
                        ) -> list[dict[str, Any]]:
    """
    按部门聚合提问与回答状态。

    用 LEFT JOIN 从 users 出发：没有账号的会话（演示模式、历史 demo-user）
    也会落在"未归属"组里，保证"各组相加 = 总量"，不接受"少了十几条"这种结果。
    """
    where = ""
    params: list[Any] = []
    if since:
        where = "AND m.created_at >= ?"
        params.append(since)

    rows = conn.execute(
        f"""SELECT
              COALESCE(d.id, '')            AS dept_id,
              COALESCE(d.name, '未归属部门')  AS dept_name,
              COUNT(DISTINCT m.session_id)   AS sessions,
              COUNT(DISTINCT s.user_id)      AS askers,
              SUM(CASE WHEN m.role = 'user'      THEN 1 ELSE 0 END) AS questions,
              SUM(CASE WHEN m.role = 'assistant' AND m.answer_status = 'answered'
                       THEN 1 ELSE 0 END) AS answered,
              SUM(CASE WHEN m.role = 'assistant' AND m.answer_status = 'partial'
                       THEN 1 ELSE 0 END) AS partial,
              SUM(CASE WHEN m.role = 'assistant' AND m.answer_status = 'refused'
                       THEN 1 ELSE 0 END) AS refused,
              SUM(CASE WHEN m.role = 'assistant' AND m.answer_status = 'error'
                       THEN 1 ELSE 0 END) AS errors
            FROM chat_messages m
            LEFT JOIN chat_sessions s ON s.id = m.session_id
            LEFT JOIN users u         ON u.id = s.user_id
            LEFT JOIN departments d   ON d.id = u.department_id
            WHERE m.answer_status <> 'streaming'
              {where}
            GROUP BY COALESCE(d.id, '')
            ORDER BY questions DESC""",
        params).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        judged = (d["answered"] or 0) + (d["partial"] or 0) + (d["refused"] or 0)
        d["refuse_rate"] = round((d["refused"] or 0) / judged, 4) if judged else 0.0
        out.append(d)
    return out


def total_stats(conn: sqlite3.Connection, since: str | None = None) -> dict[str, Any]:
    """全量口径，用来给部门聚合做"和"校验（页面底部会显示对比）。"""
    where = ""
    params: list[Any] = []
    if since:
        where = "AND m.created_at >= ?"
        params.append(since)
    row = conn.execute(
        f"""SELECT COUNT(*) AS answers,
                   SUM(CASE WHEN m.answer_status = 'refused' THEN 1 ELSE 0 END) AS refused
              FROM chat_messages m
             WHERE m.role = 'assistant' AND m.answer_status <> 'streaming'
               {where}""",
        params).fetchone()
    return {"answers": int(row["answers"] or 0), "refused": int(row["refused"] or 0)}
