"""把「磁盘上的制度文件」登记进 knowledge base（policy_documents）。

    存在的理由是修补一个真实的数据裂缝：
      阶段 1/2 的索引是 `build` 直接从 data/policy 下的文件切出来的，
      **没有登记表**；而阶段 3 的重建只认 policy_documents。
      于是"升级后第一次上传文件触发重建"时，原有制度会从索引里消失 ——
      表现为"上传一份新的，旧的问答全答不出来了"，而且没有任何报错。

    所以：
      · CLI build 时登记（新装也走这条路）；
      · 服务启动时若登记表为空、但存在旧索引，也补登记一次。
    两步都是幂等的（按 doc_id 判重）。
"""

from pathlib import Path
from typing import Any

from ..utils.console import log
from .config import Settings
from .ingest import ingest


def register_document_file(s: Settings, path: Path, *, uploaded_by: str | None = None,
                           force: bool = False) -> dict[str, Any] | None:
    """
    登记一个已存在的文件。返回文档元信息；已存在且未指定 force 时返回 None。

    不做向量化：切片入库即可，向量交给 IndexManager 统一算（还能命中缓存）。
    """
    from ..store import db, policy_repo

    path = Path(path)
    if not path.exists():
        return None

    try:
        result = ingest(s, path, path.name)
    except Exception as ex:                        # noqa: BLE001 — 登记失败不该挡住启动
        log(f"登记制度文件失败（{path.name}）：{ex}", "warn")
        return None

    meta, stats = result["meta"], result["stats"]
    conn = db.connect(s.chat_db_path)
    try:
        # 按 (doc_id, version) 判重：重启时不该把同一个版本反复覆盖成新记录
        existing = policy_repo.get_document(conn, meta["doc_id"], int(meta["version"]))
        if existing is not None and not force:
            return None
        payload = dict(
            title=meta["title"], source_file=path.name, storage_path=str(path),
            checksum=stats["checksum"], version=int(meta["version"]),
            region_code=meta["region_code"],
            effective_from=meta["effective_from"] or None,
            effective_to=meta["effective_to"] or None,
            uploaded_by=uploaded_by, chunk_count=stats["chunks"],
            char_count=stats["chars"], doc_type=meta.get("doc_type", "policy"),
        )
        if existing is None:
            # 走 publish_version：磁盘上这份文件是"现在的现行版"，
            # 同 doc_id 的旧版本自动下线，避免启动时留下两个 active
            policy_repo.publish_version(conn, doc_id=meta["doc_id"], **payload)
        else:
            policy_repo.update_document(conn, meta["doc_id"], **payload)
    finally:
        conn.close()
    return meta
