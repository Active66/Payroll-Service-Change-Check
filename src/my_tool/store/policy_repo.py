"""制度文档清单的读写。

    这张表是"知识库里有什么"的唯一事实源：索引是从它派生出来的，
    删文档 = 改 status + 重建，而不是去动 FAISS 文件本身。

    **唯一键是 (doc_id, version)**，不是 doc_id：
      制度每年都会改，换版若要求"先删旧版再传新版"，那两步之间的时间里
      索引里没有这份制度，员工提问会直接拒答。有了版本台账，换版是一次
      原子操作：新版入库 → 旧版置 superseded → 重建 → 热切换。

    status 三态，语义必须分清：
      active      当前在用（索引只认它，且 effective_from 已到期）
      superseded  被新版替代，留档可查、可回滚
      deleted     这份不要了（软删，物理文件保留）
"""

import sqlite3
from typing import Any

from .db import immediate, new_id, now_iso


def create_document(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    title: str,
    source_file: str,
    storage_path: str,
    checksum: str,
    doc_type: str = "policy",
    version: int = 1,
    version_label: str | None = None,
    region_code: str = "",
    effective_from: str | None = None,
    effective_to: str | None = None,
    uploaded_by: str | None = None,
    chunk_count: int = 0,
    char_count: int = 0,
) -> dict[str, Any]:
    """插入文档记录。是否已存在由调用方先查（见 routes_kb 的上传逻辑）。"""
    ts = now_iso()
    row_id = new_id()
    with immediate(conn):
        conn.execute(
            """INSERT INTO policy_documents
               (id, doc_id, title, doc_type, version, version_label, region_code,
                effective_from, effective_to, source_file, storage_path, checksum,
                status, chunk_count, char_count, uploaded_by, published_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?,?,?,?,?)""",
            (row_id, doc_id, title, doc_type, version, version_label or str(version),
             region_code, effective_from, effective_to, source_file, storage_path,
             checksum, chunk_count, char_count, uploaded_by, uploaded_by, ts),
        )
    return {"id": row_id, "doc_id": doc_id, "title": title, "version": version}


def publish_version(
    conn: sqlite3.Connection,
    *,
    doc_id: str,
    title: str,
    source_file: str,
    storage_path: str,
    checksum: str,
    doc_type: str = "policy",
    version: int = 1,
    version_label: str | None = None,
    region_code: str = "",
    effective_from: str | None = None,
    effective_to: str | None = None,
    uploaded_by: str | None = None,
    chunk_count: int = 0,
    char_count: int = 0,
) -> dict[str, Any]:
    """
    **发布一个新版本**：插入 + 同 doc_id 旧版集体下线，在同一个事务里完成。

    单独提供这个函数而不是让调用方自己串 create_document + supersede，
    是因为两个函数各自开事务，中间那一刻"两个版本都是 active"——虽然只在
    毫秒级，但只要恰好撞上一次重建，索引里就会同时出现两版条款。
    事务边界属于仓储层的职责，不该外泄给路由。
    """
    ts = now_iso()
    row_id = new_id()
    with immediate(conn):
        conn.execute(
            """UPDATE policy_documents
                  SET status = 'superseded', superseded_at = ?
                WHERE doc_id = ? AND status = 'active'""",
            (ts, doc_id),
        )
        conn.execute(
            """INSERT INTO policy_documents
               (id, doc_id, title, doc_type, version, version_label, region_code,
                effective_from, effective_to, source_file, storage_path, checksum,
                status, chunk_count, char_count, uploaded_by, published_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'active', ?,?,?,?,?)""",
            (row_id, doc_id, title, doc_type, version, version_label or str(version),
             region_code, effective_from, effective_to, source_file, storage_path,
             checksum, chunk_count, char_count, uploaded_by, uploaded_by, ts),
        )
    return {"id": row_id, "doc_id": doc_id, "title": title, "version": version}


def supersede(conn: sqlite3.Connection, doc_id: str, keep_version: int) -> int:
    """
    把同一 doc_id 下除 keep_version 之外的 active 记录全部置为 superseded。

    返回受影响行数。调用方必须在**同一个事务**里完成"插入新版 + 下线旧版"，
    否则中途崩溃会留下"两个 active 版本"——索引里同一条款出现两遍，
    RRF 融合时互相挤占名额，表现是"答案引用跳来跳去"。
    """
    with immediate(conn):
        cur = conn.execute(
            """UPDATE policy_documents
                  SET status = 'superseded', superseded_at = ?
                WHERE doc_id = ? AND version <> ? AND status = 'active'""",
            (now_iso(), doc_id, keep_version),
        )
        return cur.rowcount


def reactivate(conn: sqlite3.Connection, doc_id: str, version: int) -> bool:
    """把某个历史版本重新置为 active（回滚）。同 doc_id 其他 active 版本一并下线。"""
    with immediate(conn):
        cur = conn.execute(
            """UPDATE policy_documents SET status = 'active', superseded_at = NULL
                WHERE doc_id = ? AND version = ? AND status IN ('superseded','deleted')""",
            (doc_id, version),
        )
        hit = cur.rowcount > 0
        if hit:
            # 同一个事务里顺手把其余版本下线，理由同 publish_version
            conn.execute(
                """UPDATE policy_documents
                      SET status = 'superseded', superseded_at = ?
                    WHERE doc_id = ? AND version <> ? AND status = 'active'""",
                (now_iso(), doc_id, version),
            )
    return hit


def update_document(
    conn: sqlite3.Connection, doc_id: str, *,
    title: str, source_file: str, storage_path: str, checksum: str,
    version: int, region_code: str = "", effective_from: str | None = None,
    effective_to: str | None = None, uploaded_by: str | None = None,
    chunk_count: int = 0, char_count: int = 0,
) -> None:
    """
    原地更新同一 (doc_id, version)。

    只用于"同一版本重新上传"（文件内容改错、或恢复已删除的版本）。
    真正的新版本走 create_document + supersede，绝不覆盖历史行 ——
    审计要能回答"当时生效的是哪一版、内容是什么"。
    """
    with immediate(conn):
        conn.execute(
            """UPDATE policy_documents
                  SET title = ?, source_file = ?, storage_path = ?, checksum = ?,
                      region_code = ?, effective_from = ?, effective_to = ?,
                      status = 'active', chunk_count = ?, char_count = ?,
                      uploaded_by = ?, published_by = ?, deleted_at = NULL,
                      deleted_by = NULL, superseded_at = NULL, indexed_at = NULL
                WHERE doc_id = ? AND version = ?""",
            (title, source_file, storage_path, checksum, region_code,
             effective_from, effective_to, chunk_count, char_count, uploaded_by,
             uploaded_by, doc_id, version),
        )


def get_document(conn: sqlite3.Connection, doc_id: str,
                 version: int | None = None) -> dict[str, Any] | None:
    """不传 version 时返回"当前该看的那一版"：优先 active，其次最新的非删除版。"""
    if version is not None:
        row = conn.execute(
            "SELECT * FROM policy_documents WHERE doc_id = ? AND version = ?",
            (doc_id, version)).fetchone()
        return dict(row) if row else None
    row = conn.execute(
        """SELECT * FROM policy_documents WHERE doc_id = ?
            ORDER BY (status = 'active') DESC, version DESC LIMIT 1""",
        (doc_id,)).fetchone()
    return dict(row) if row else None


def list_versions(conn: sqlite3.Connection, doc_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM policy_documents WHERE doc_id = ?
            ORDER BY version DESC""", (doc_id,)).fetchall()
    return [dict(r) for r in rows]


def list_documents(conn: sqlite3.Connection, status: str | None = None
                   ) -> list[dict[str, Any]]:
    if status:
        rows = conn.execute(
            """SELECT * FROM policy_documents WHERE status = ?
                ORDER BY doc_id, version DESC""", (status,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM policy_documents ORDER BY doc_id, version DESC").fetchall()
    return [dict(r) for r in rows]


def list_active(conn: sqlite3.Connection, today: str | None = None
                ) -> list[dict[str, Any]]:
    """
    索引重建只认这些。顺序按 (doc_id, version) 稳定排序，
    保证同一份数据每次重建的切片顺序一致 —— 否则切片号会跳，
    后台"切片预览"与线上引用编号对不上。

    today 传了就顺带过滤掉"生效期未到"的版本：待生效版本不该能被检索到。
    """
    rows = conn.execute(
        """SELECT * FROM policy_documents
            WHERE status = 'active' ORDER BY doc_id ASC, version ASC""").fetchall()
    docs = [dict(r) for r in rows]
    if today:
        docs = [d for d in docs
                if not d.get("effective_from") or d["effective_from"] <= today]
    return docs


def list_pending(conn: sqlite3.Connection, today: str) -> list[dict[str, Any]]:
    """已发布但生效期未到：后台要能看见，否则"我明明传了却答不出来"会很难查。"""
    rows = conn.execute(
        """SELECT * FROM policy_documents
            WHERE status = 'active' AND effective_from IS NOT NULL AND effective_from > ?
            ORDER BY effective_from ASC""", (today,)).fetchall()
    return [dict(r) for r in rows]


def soft_delete(conn: sqlite3.Connection, doc_id: str, by: str | None = None,
                version: int | None = None) -> bool:
    """
    软删。**不删物理文件**：万一删错了，改回 status 就能恢复，
    比"从回收站找一个 uuid 命名的文件"靠谱得多。

    version 为 None 时删该 doc_id 下全部 active 版本（"这份制度整体下线"）。
    """
    ts = now_iso()
    with immediate(conn):
        if version is None:
            cur = conn.execute(
                """UPDATE policy_documents
                      SET status = 'deleted', deleted_at = ?, deleted_by = ?
                    WHERE doc_id = ? AND status = 'active'""",
                (ts, by, doc_id))
        else:
            cur = conn.execute(
                """UPDATE policy_documents
                      SET status = 'deleted', deleted_at = ?, deleted_by = ?
                    WHERE doc_id = ? AND version = ? AND status = 'active'""",
                (ts, by, doc_id, version))
        return cur.rowcount > 0


def mark_indexed(conn: sqlite3.Connection, doc_id: str, chunk_count: int,
                 char_count: int, version: int | None = None) -> None:
    with immediate(conn):
        if version is None:
            conn.execute(
                """UPDATE policy_documents
                      SET indexed_at = ?, chunk_count = ?, char_count = ?
                    WHERE doc_id = ? AND status = 'active'""",
                (now_iso(), chunk_count, char_count, doc_id))
        else:
            conn.execute(
                """UPDATE policy_documents
                      SET indexed_at = ?, chunk_count = ?, char_count = ?
                    WHERE doc_id = ? AND version = ?""",
                (now_iso(), chunk_count, char_count, doc_id, version))


def count_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM policy_documents GROUP BY status").fetchall()
    return {r["status"]: int(r["n"]) for r in rows}
