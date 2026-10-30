"""人事档案的读写。

    为什么与 `users` 分开：

      · **生命周期不同**。账号可以禁用、可以删；人事档案要留 5 年（第三十七条），
        离职员工的档案还得能被追溯查询。塞在一张表里，删账号就等于删档案。
      · **来源不同**。账号是本系统创建的；档案来自 HR 主数据（或批量导入，第三十四条）。
        一个刚导入的员工可能还没开账号，此时 `user_id` 为空。
      · **权限边界不同**。档案里有薪资、证件号，能查的人比能登录的人少得多。

    脱敏在**这里**做，不在提示词里做。靠"告诉模型别说证件号"是防不住的，
    因为它可能只是"顺便提一句"。数据不出库，模型就没得说。
"""

import sqlite3
from typing import Any

from .db import immediate, now_iso

# 需要脱敏的字段：只留后 4 位。证件号后 4 位足够本人核对，不足以冒用
MASKED_FIELDS = ("id_card", "bank_account", "phone")


def upsert(conn: sqlite3.Connection, *, employee_no: str, display_name: str,
           hire_date: str | None = None, grade: str | None = None,
           fixed_salary: float | None = None, si_base: float | None = None,
           hf_base: float | None = None, region_code: str = "",
           legal_entity_id: str = "", user_id: str | None = None,
           status: str = "active", id_card: str | None = None,
           bank_account: str | None = None, phone: str | None = None) -> dict[str, Any]:
    ts = now_iso()
    with immediate(conn):
        conn.execute(
            """INSERT INTO employees
               (employee_no, display_name, hire_date, grade, fixed_salary, si_base,
                hf_base, region_code, legal_entity_id, status, user_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (employee_no) DO UPDATE SET
                 display_name = excluded.display_name,
                 hire_date = excluded.hire_date,
                 grade = excluded.grade,
                 fixed_salary = excluded.fixed_salary,
                 si_base = excluded.si_base,
                 hf_base = excluded.hf_base,
                 region_code = excluded.region_code,
                 legal_entity_id = excluded.legal_entity_id,
                 status = excluded.status,
                 user_id = COALESCE(excluded.user_id, employees.user_id),
                 updated_at = excluded.updated_at""",
            (employee_no, display_name, hire_date, grade, fixed_salary, si_base,
             hf_base, region_code, legal_entity_id, status, user_id, ts, ts))
    return {"employee_no": employee_no, "display_name": display_name}


def get(conn: sqlite3.Connection, employee_no: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM employees WHERE employee_no = ?",
                       (employee_no,)).fetchone()
    return dict(row) if row else None


def get_by_user(conn: sqlite3.Connection, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM employees WHERE user_id = ?",
                       (user_id,)).fetchone()
    return dict(row) if row else None


def mask(record: dict[str, Any], *, unmask: bool = False) -> dict[str, Any]:
    """
    脱敏。`unmask=True` 只有管理员能触发，且调用方要自己保证鉴权。

    证件号/账号只留后 4 位并保留原长度，这样本人能认出来、
    别人拿去也没用，同时不会因为"变成 ****"而让人以为查错了员工。
    """
    if unmask:
        return record
    out = dict(record)
    for key in MASKED_FIELDS:
        val = out.get(key)
        if not val:
            continue
        s = str(val)
        out[key] = ("*" * max(0, len(s) - 4)) + s[-4:] if len(s) > 4 else "****"
    return out


def pick(record: dict[str, Any], fields: list[str] | None) -> dict[str, Any]:
    """
    按需裁剪字段。Agent 上下文是稀缺资源，把整条档案塞进去会挤掉条款原文。

    始终带上 employee_no 与 display_name：模型据此确认"查的是对的人"，
    少了这两个字段它会在回答里用"该员工"这种模糊说法，用户无法核对。
    """
    always = ("employee_no", "display_name")
    if not fields:
        return dict(record)
    keys = list(dict.fromkeys([*always, *fields]))
    return {k: record.get(k) for k in keys if k in record}


def count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"])
