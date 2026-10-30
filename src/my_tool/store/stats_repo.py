"""后台统计查询。只读，不写库。

    dashboard 的每个数字都要有明确定义，否则"拒答率 12%"这种数字没人敢用。
    这里把口径写在函数名与注释里，页面只负责展示。
"""

import sqlite3
from datetime import datetime, time

from .db import now_iso


def _today_start() -> str:
    """今天 00:00 的 ISO 字符串。库里 created_at 存的就是同格式，可直接比较。"""
    return datetime.combine(datetime.now().astimezone().date(), time.min).astimezone().isoformat(timespec="seconds")


def count_documents(conn: sqlite3.Connection, status: str = "active") -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM policy_documents WHERE status = ?",
                       (status,)).fetchone()
    return int(row["n"]) if row else 0


def today_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """
    今日问答口径：
      questions   用户提问条数（role='user'）
      answered    正常作答（answered + partial，partial 也算答了，只是有疑点）
      refused     拒答（没有检索到依据）
      errors      生成失败
    拒答率 = refused / (answered + refused)，分母不含 error ——
    把系统故障算进"拒答率"会让这个指标失去意义。
    """
    start = _today_start()
    rows = conn.execute(
        """SELECT role, answer_status, COUNT(*) AS n
             FROM chat_messages
            WHERE created_at >= ?
         GROUP BY role, answer_status""",
        (start,),
    ).fetchall()

    out = {"questions": 0, "answered": 0, "refused": 0, "errors": 0}
    for r in rows:
        role, status, n = r["role"], r["answer_status"], int(r["n"])
        if role == "user":
            out["questions"] += n
        elif role == "assistant":
            if status in ("answered", "partial", "streaming"):
                out["answered"] += n
            elif status == "refused":
                out["refused"] += n
            elif status == "error":
                out["errors"] += n
    return out


def refusal_rate(counts: dict[str, int]) -> float:
    """拒答率。分母为 0 时返回 0，不让页面出现 ZeroDivisionError 或 nan。"""
    base = counts.get("answered", 0) + counts.get("refused", 0)
    if base <= 0:
        return 0.0
    return counts.get("refused", 0) / base


def pending_audits(conn: sqlite3.Connection) -> int:
    """
    待处理疑似违规数。

    "待处理"的定义：最新一条有效标记是 suspect，且之后没有 confirmed / cleared。
    这样"标了又清掉"的记录不会一直挂在这个数字上。
    """
    row = conn.execute(
        """SELECT COUNT(DISTINCT a.message_id) AS n
             FROM message_audits a
            WHERE a.flag = 'suspect' AND a.revoked_at IS NULL
              AND NOT EXISTS (
                    SELECT 1 FROM message_audits b
                     WHERE b.message_id = a.message_id
                       AND b.flag IN ('confirmed','cleared')
                       AND b.revoked_at IS NULL)"""
    ).fetchone()
    return int(row["n"]) if row else 0


def recent_sessions(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """最近活跃会话，概览页给个"最近在问什么"的入口。"""
    rows = conn.execute(
        """SELECT s.id, s.user_id, s.title, s.message_count, s.last_message_at,
                  u.display_name
             FROM chat_sessions s
             LEFT JOIN users u ON u.id = s.user_id
            WHERE s.deleted_at IS NULL AND s.last_message_at IS NOT NULL
            ORDER BY s.last_message_at DESC
            LIMIT ?""",
        (max(1, min(limit, 20)),),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_indexed(conn: sqlite3.Connection, doc_id: str, chunk_count: int) -> None:
    """重建完成后回写索引时间。放在这里而不是 policy_repo，是因为它与"重建"这个动作绑定。"""
    conn.execute("UPDATE policy_documents SET indexed_at = ? WHERE doc_id = ?",
                 (now_iso(), doc_id))
