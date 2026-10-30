"""制度参数的读写。**数字的唯一来源。**

    为什么必须单独成表，而不是让模型从条款文本里读数字：

      条款原文里写着"上限为 36,921 元/月"，模型完全可以照抄。但照抄有三个问题：
        ① 无法校验 —— 抄错了也没有任何东西能发现（`guard.py` 只能事后发现
           "这个数字不在原文里"，但 36,921 抄成 36,9210 是"看起来在原文里"的）；
        ② 无法回答"凭什么" —— 审计要的是"这个数来自哪一条、哪一版"；
        ③ 无法按地区覆盖 —— 第三十五条要求以参保地政策为准，同一参数在不同
           region_code 下取值不同。

      所以：**数值只从这张表来，表里每一行都必须有 source_clause。**
      没有出处的参数等于没有参数。

    地区优先级的约定（第三十五条"参数缺失时不得由人工臆测填值"）：
      查某个 region_code 时，先取该地区的行；没有则回落到 region_code='' 的
      全国默认行。两者都没有 → 返回 missing，由调用方转人工，**不猜**。
"""

import sqlite3
from datetime import date
from typing import Any

from .db import immediate, now_iso


def upsert_param(
    conn: sqlite3.Connection, *,
    param_key: str,
    value: str | float | int,
    unit: str = "",
    source_clause: str,
    region_code: str = "",
    source_doc_id: str = "",
    effective_from: str | None = None,
    effective_to: str | None = None,
    note: str | None = None,
    updated_by: str | None = None,
) -> None:
    """
    写入/更新一个参数。幂等：同一 (key, region, effective_from) 覆盖。

    不做"版本历史表"：参数变更频率极低（一年一两次），而每次变更都应该
    同时更新制度（制度第十九条的年度调整本来就要发新版），历史由制度版本承载。
    真要追，policy_documents 的版本链 + 这里的 updated_at 够用。
    """
    ts = now_iso()
    with immediate(conn):
        conn.execute(
            """INSERT INTO policy_params
               (param_key, value, unit, region_code, source_clause, source_doc_id,
                effective_from, effective_to, note, updated_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (param_key, region_code, effective_from) DO UPDATE SET
                 value = excluded.value,
                 unit = excluded.unit,
                 source_clause = excluded.source_clause,
                 source_doc_id = excluded.source_doc_id,
                 effective_to = excluded.effective_to,
                 note = excluded.note,
                 updated_by = excluded.updated_by,
                 updated_at = excluded.updated_at""",
            (param_key, str(value), unit, region_code, source_clause, source_doc_id,
             effective_from, effective_to, note, updated_by, ts, ts),
        )


def _pick(rows: list[dict[str, Any]], region_code: str, as_of: str) -> dict[str, Any] | None:
    """
    从候选行里挑一条：地区精确匹配优先，其次全国默认；再按生效期筛。

    排序而不是"取第一条"，是因为 SQLite 的返回顺序不保证，而"同一参数有
    地区版和全国版"时取错会直接答错数。
    """
    def usable(r: dict[str, Any]) -> bool:
        start = r.get("effective_from") or ""
        end = r.get("effective_to") or ""
        return (not start or start <= as_of) and (not end or end >= as_of)

    ordered = sorted(
        [r for r in rows if usable(r)],
        key=lambda r: (r["region_code"] == region_code, r["effective_from"] or ""),
        reverse=True,
    )
    return ordered[0] if ordered else None


def get_params(conn: sqlite3.Connection, keys: list[str],
               region_code: str = "", as_of: str | None = None
               ) -> tuple[list[dict[str, Any]], list[str]]:
    """
    批量取参数。返回 (找到的, 缺失的 key 列表)。

    一次查完所有 key 而不是逐个查：Agent 一轮里可能要 3-5 个参数，
    逐个查就是 3-5 次往返。这里用 IN 一次拿完，内存里再挑。
    """
    if not keys:
        return [], []
    as_of = as_of or date.today().isoformat()
    placeholders = ",".join("?" * len(keys))
    rows = conn.execute(
        f"""SELECT * FROM policy_params
             WHERE param_key IN ({placeholders})
               AND (region_code = ? OR region_code = '')
             ORDER BY param_key""",
        [*keys, region_code]).fetchall()
    by_key: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_key.setdefault(r["param_key"], []).append(dict(r))

    found: list[dict[str, Any]] = []
    missing: list[str] = []
    for k in keys:
        cands = by_key.get(k)
        hit = _pick(cands, region_code, as_of) if cands else None
        if hit is None:
            missing.append(k)
            continue
        found.append({
            "key": hit["param_key"],
            "value": _coerce(hit["value"]),
            "unit": hit["unit"],
            "source_clause": hit["source_clause"],
            "source_doc_id": hit["source_doc_id"],
            "region_code": hit["region_code"],
            "effective_from": hit["effective_from"],
            "effective_to": hit["effective_to"],
            "note": hit["note"],
        })
    return found, missing


def _coerce(raw: str) -> float | int | str:
    """数值型参数返回数字，其余原样返回。

    为什么不让调用方自己 float()：单位是"元/月"还是"%"由 unit 表达，
    调用方拿到字符串再自己解析，迟早会出现"把 5% 解析成 5"的错误。
    """
    try:
        num = float(raw)
    except (TypeError, ValueError):
        return raw
    return int(num) if num.is_integer() else num


def list_params(conn: sqlite3.Connection, region_code: str | None = None
                ) -> list[dict[str, Any]]:
    if region_code is None:
        rows = conn.execute(
            "SELECT * FROM policy_params ORDER BY param_key, region_code").fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM policy_params WHERE region_code = ?
                ORDER BY param_key""", (region_code,)).fetchall()
    return [dict(r) for r in rows]


def count_params(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM policy_params").fetchone()["n"])
