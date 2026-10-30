"""Agent 轨迹的读写。

    这张表回答的是"它为什么这么判断"——Agent 最常被追问、也最难自证的问题。
    答案只存在于工具调用的序列里：查了哪些条款、拿到什么参数、规则判了什么、
    用的哪张凭证建的单。模型自己的解释不算证词，轨迹才算。

    存储取舍：`result` 只存摘要（见 registry._summarize），不存完整返回。
    切片正文动辄上千字，全存会让这张表以每天几十兆的速度膨胀，
    而"当时命中哪几条条款"用 chunk_id 就能回溯到 chunks.jsonl。
"""

import json
import sqlite3
from typing import Any

from .db import immediate, now_iso


def record(conn: sqlite3.Connection, *, session_id: str, turn_no: int,
           tool_name: str, args: dict[str, Any], result: dict[str, Any],
           ok: bool = True, error_code: str | None = None,
           latency_ms: int = 0) -> int:
    """追加一条。seq 在写入时按 (session, turn) 自增，保证时序稳定。"""
    ts = now_iso()
    with immediate(conn):
        row = conn.execute(
            """SELECT COALESCE(MAX(seq), 0) AS s FROM agent_traces
                WHERE session_id = ? AND turn_no = ?""",
            (session_id, turn_no)).fetchone()
        seq = int(row["s"]) + 1
        cur = conn.execute(
            """INSERT INTO agent_traces
               (session_id, turn_no, seq, tool_name, args, result, ok, error_code,
                latency_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (session_id, turn_no, seq, tool_name,
             json.dumps(args, ensure_ascii=False, default=str),
             json.dumps(result, ensure_ascii=False, default=str),
             1 if ok else 0, error_code, latency_ms, ts))
        return int(cur.lastrowid or 0)


def list_traces(conn: sqlite3.Connection, session_id: str,
                turn_no: int | None = None, limit: int = 200
                ) -> list[dict[str, Any]]:
    if turn_no is None:
        rows = conn.execute(
            """SELECT * FROM agent_traces WHERE session_id = ?
                ORDER BY turn_no, seq LIMIT ?""", (session_id, limit)).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM agent_traces WHERE session_id = ? AND turn_no = ?
                ORDER BY seq LIMIT ?""", (session_id, turn_no, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for key in ("args", "result"):
            try:
                d[key] = json.loads(d[key] or "{}")
            except json.JSONDecodeError:
                pass
        d["ok"] = bool(d["ok"])
        out.append(d)
    return out


def count_traces(conn: sqlite3.Connection, session_id: str | None = None) -> int:
    if session_id:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM agent_traces WHERE session_id = ?",
            (session_id,)).fetchone()["n"])
    return int(conn.execute("SELECT COUNT(*) AS n FROM agent_traces").fetchone()["n"])


def record_run(conn: sqlite3.Connection, *, session_id: str, turn_no: int,
               question: str, model: str, tokens_in: int, tokens_out: int,
               finish_reason: str = "", thought: str = "",
               chose_tools: list[str] | None = None, degraded: bool = False,
               latency_ms: int = 0) -> int:
    """
    记录一轮"模型决策"。

    `chose_tools` 记的是本轮模型**选了哪些工具**（不管执行结果）。
    与 agent_traces 的 tool_name 区分开：traces 记"实际执行了谁"，
    这里记"模型这一轮想做什么"——两者对不上时，正是分析模型的决策质量
    （它想调一个不存在的工具？还是想跳过校验？）。
    """
    ts = now_iso()
    with immediate(conn):
        cur = conn.execute(
            """INSERT INTO agent_runs
               (session_id, turn_no, question, model, tokens_in, tokens_out,
                finish_reason, thought, chose_tools, degraded, latency_ms, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, turn_no, question, model, tokens_in, tokens_out,
             finish_reason, thought[:2000], json.dumps(chose_tools or [], ensure_ascii=False),
             1 if degraded else 0, latency_ms, ts))
        return int(cur.lastrowid or 0)


def list_runs(conn: sqlite3.Connection, session_id: str,
              limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM agent_runs WHERE session_id = ?
            ORDER BY turn_no ASC LIMIT ?""", (session_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["chose_tools"] = json.loads(d.get("chose_tools") or "[]")
        except json.JSONDecodeError:
            d["chose_tools"] = []
        d["degraded"] = bool(d["degraded"])
        out.append(d)
    return out


def replay(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """
    把一轮对话的决策 + 工具调用合并成时间线，供"回放它为什么这么判断"。

    返回按 (turn_no, seq) 排序的列表，每项是 run 或 trace。
    """
    runs = {r["turn_no"]: r for r in list_runs(conn, session_id, limit=200)}
    traces = list_traces(conn, session_id, limit=2000)
    timeline: list[dict[str, Any]] = []
    for turn in sorted(runs):
        timeline.append({"kind": "decision", "turn": turn, **runs[turn]})
        for t in traces:
            if t["turn_no"] == turn:
                timeline.append({"kind": "tool", "turn": turn, **t})
    return timeline
