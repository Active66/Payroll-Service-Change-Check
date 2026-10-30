"""知识库管理页面：列表、上传、删除、重建、切片预览。

    页面里的写操作全部走 PRG（POST → 303 → GET）：
    浏览器刷新不会重复提交，也不会弹"是否重新发送表单"的对话框。
    成功/失败都通过跳转后的 query 参数显示，不依赖 session。
"""

import json
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from ..core.ingest import IngestError, ingest, save_upload
from ..core.index_manager import RebuildBusy
from ..store import db, policy_repo
from ..utils.console import log
from . import auth
from .errors import (KB_BAD_TYPE, KB_DUPLICATE, KB_NOT_FOUND, KB_PARSE_FAILED,
                     KB_TOO_LARGE, ApiError)
from .templating import render

router = APIRouter()

# =============================================================================
# 【模块 25 · 知识库页面】目标文件：src/my_tool/api/routes_kb.py
# =============================================================================


def _back(msg: str, *, ok: bool = False, to: str = "/admin/kb") -> RedirectResponse:
    """PRG 的返回。中文消息走 URL 编码，页面里直接显示在提示条上。"""
    key = "ok" if ok else "err"
    return RedirectResponse(f"{to}?{key}={quote(msg)}", status_code=303)


def _ingest_error(ex: IngestError) -> ApiError:
    """把摄入异常映射到错误码。kind 决定用户该改什么（换文件/换格式/改内容）。"""
    if ex.kind == "type":
        return ApiError(KB_BAD_TYPE, detail={"reason": ex.reason})
    if ex.kind == "size":
        return ApiError(KB_TOO_LARGE, limit=10, detail={"reason": ex.reason})
    return ApiError(KB_PARSE_FAILED, reason=ex.reason, detail={"reason": ex.reason})


def _chunks_of(index_dir, doc_id: str, version: int | None = None) -> list[dict[str, Any]]:
    """
    从当前索引产物里读某份文档的切片。

    直接读 chunks.jsonl 而不是加载整个 VectorStore：详情页只需要看切片文本，
    为了它把 FAISS 索引读进内存是浪费。

    **只在索引里有这份文件时才有内容**：已被新版替代的旧版不在索引里，
    这个函数就返回空列表，页面会显示"该版本未进入当前索引"。
    要做真正的历史切片对照，得保留旧索引产物，那是另一个设计（见 integration-design 第 9 节）。
    """
    path = index_dir / "chunks.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("doc_id") != doc_id:
                continue
            if version is not None and int(obj.get("version") or 0) != version:
                continue
            out.append(obj)
    return out


@router.get("/admin/kb")
def kb_list(request: Request, status: str = "active",
            user: dict = Depends(auth.require_admin)) -> Response:
    ctx = request.app.state.ctx
    today = time.strftime("%Y-%m-%d")
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        allowed = ("active", "superseded", "deleted")
        docs = policy_repo.list_documents(conn, status if status in allowed else None)
        # 待生效徽标：传了但还没到生效日，不进索引。不给这个提示，
        # 管理员会以为"上传成功却没生效"是 bug
        pending = {d["id"] for d in policy_repo.list_pending(conn, today)}
        # 同一 doc_id 有几个版本：列表页只显示现行版，版本数是个线索
        version_counts: dict[str, int] = {}
        for d in policy_repo.list_documents(conn):
            version_counts[d["doc_id"]] = version_counts.get(d["doc_id"], 0) + 1
    finally:
        conn.close()

    return render(request, "kb_list.html", {
        "docs": docs,
        "status": status,
        "pending_ids": pending,
        "version_counts": version_counts,
        "max_mb": ctx.settings.upload_max_mb,
        "rebuild": ctx.rebuild_state,
        "index": ctx.manifest or {},
    })


@router.get("/admin/kb/{doc_id}")
def kb_detail(doc_id: str, request: Request, version: int | None = None,
              user: dict = Depends(auth.require_admin)) -> Response:
    """
    version 不传时看现行版；传了就看指定历史版本的切片 ——
    "上一版到底怎么写的"是审计里最常见的一个追问。
    """
    ctx = request.app.state.ctx
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        doc = policy_repo.get_document(conn, doc_id, version)
        versions = policy_repo.list_versions(conn, doc_id)
        today = time.strftime("%Y-%m-%d")
    finally:
        conn.close()
    if doc is None:
        raise ApiError(KB_NOT_FOUND, detail={"doc_id": doc_id})

    chunks = _chunks_of(ctx.settings.index_dir, doc_id, int(doc["version"]))
    return render(request, "kb_detail.html", {
        "doc": doc, "chunks": chunks, "versions": versions,
        "is_pending": bool(doc.get("effective_from") and doc["effective_from"] > today),
    })


@router.post("/admin/kb/upload")
async def kb_upload(request: Request, user: dict = Depends(auth.require_admin)) -> Response:
    """
    上传 → 落盘 → 解析切片 → 入库 → 触发重建。

    注意：**不做同步向量化**。大 PDF 现场向量化会让请求卡几十秒，
    而且和后面"重建时统一向量化"重复。上传只负责变成切片。
    """
    ctx = request.app.state.ctx
    form = await request.form()
    auth.verify_csrf(request, str(form.get("_csrf", "")))
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return _back("请选择要上传的文件")

    try:
        data = await upload.read()
    except Exception as ex:                       # noqa: BLE001
        log(f"读取上传文件失败：{ex}", "err")
        return _back("文件读取失败，请重试")

    meta_form = {
        "doc_id": str(form.get("doc_id", "")),
        "title": str(form.get("title", "")),
        "version": str(form.get("version", "")),
        "region_code": str(form.get("region_code", "")),
        "effective_from": str(form.get("effective_from", "")),
        "effective_to": str(form.get("effective_to", "")),
    }
    version_label = str(form.get("version_label", "")).strip()

    def _work() -> tuple[str, str]:
        """跑在线程池里：解析 PDF 可能几百毫秒到几秒，不能占住事件循环。"""
        path, source_file = save_upload(ctx.settings, upload.filename, data)
        result = ingest(ctx.settings, path, source_file, meta_form)
        meta = result["meta"]
        stats = result["stats"]
        version = int(meta["version"])

        conn = db.connect(ctx.settings.chat_db_path)
        try:
            same = policy_repo.get_document(conn, meta["doc_id"], version)
            has_other_active = any(
                d["status"] == "active" and int(d["version"]) != version
                for d in policy_repo.list_versions(conn, meta["doc_id"]))
            payload = dict(
                title=meta["title"], source_file=source_file, storage_path=str(path),
                checksum=stats["checksum"], version=version,
                version_label=version_label or None,
                region_code=meta["region_code"],
                effective_from=meta["effective_from"] or None,
                effective_to=meta["effective_to"] or None,
                uploaded_by=user.get("id"), chunk_count=stats["chunks"],
                char_count=stats["chars"],
                doc_type=meta.get("doc_type", "policy"),
            )
            if same is None:
                # 新版本：同 doc_id 的旧版自动 superseded（同一事务，见 publish_version）
                policy_repo.publish_version(conn, doc_id=meta["doc_id"], **payload)
                action = "发布新版" if has_other_active else "新增"
            elif same.get("status") == "active":
                # 同 doc_id + 同版本且在用：拒绝覆盖。制度不能静默替换（架构文档 3.5），
                # 要改内容就提升版本号——否则"哪一版是当时生效的"就说不清了
                raise ApiError(KB_DUPLICATE, detail={
                    "doc_id": meta["doc_id"], "version": version})
            else:
                # 同版本但已被替代或删除：允许重新上传（内容修正 / 恢复）
                policy_repo.update_document(conn, meta["doc_id"], **payload)
                policy_repo.reactivate(conn, meta["doc_id"], version)
                action = "恢复"
        finally:
            conn.close()

        note = ""
        if stats.get("no_structure"):
            note = "（未识别到章节结构，引用只能精确到段落）"
        pending = ""
        if meta["effective_from"] and meta["effective_from"] > time.strftime("%Y-%m-%d"):
            pending = f"（{meta['effective_from']} 起生效，暂未进入索引）"
        return (f"{action}《{meta['title']}》v{version}：{stats['chunks']} 条切片 / "
                f"{stats['chars']} 字{note}{pending}"), meta["doc_id"]

    try:
        message, _doc_id = await run_in_threadpool(_work)
    except ApiError as ex:
        return _back(ex.message)
    except IngestError as ex:
        return _back(_ingest_error(ex).message)
    except Exception as ex:                       # noqa: BLE001
        log(f"上传处理失败：{ex}", "err")
        return _back(f"处理失败：{type(ex).__name__}")

    # 触发后台重建，把"新文件已生效"这件事交给索引管理器
    try:
        ctx.index_manager.trigger(reason="upload")
        message += "，正在重建索引"
    except RebuildBusy:
        message += "，索引正在重建中，完成后生效"

    return _back(message, ok=True)


@router.post("/admin/kb/{doc_id}/delete")
async def kb_delete(doc_id: str, request: Request,
                    user: dict = Depends(auth.require_admin)) -> Response:
    """软删 + 重建。二次确认由前端表单的 confirm 字段保证。

    `version` 留空 = 整份制度下线（所有 active 版本）；
    填了 = 只下掉这一版（比如新版发错了，退回上一版）。
    """
    ctx = request.app.state.ctx
    form = await request.form()
    auth.verify_csrf(request, str(form.get("_csrf", "")))
    if str(form.get("confirm", "")) != "1":
        return _back("删除需要勾选确认")

    raw_version = str(form.get("version", "")).strip()
    version = int(raw_version) if raw_version.isdigit() else None

    conn = db.connect(ctx.settings.chat_db_path)
    try:
        doc = policy_repo.get_document(conn, doc_id, version)
        if doc is None:
            return _back("文档不存在")
        policy_repo.soft_delete(conn, doc_id, by=user.get("id"), version=version)
    finally:
        conn.close()

    try:
        ctx.index_manager.trigger(reason="delete")
        msg = "已删除，正在重建索引"
    except RebuildBusy:
        msg = "已删除，索引正在重建中，完成后生效"
    return _back(msg, ok=True)


@router.post("/admin/kb/{doc_id}/rollback")
async def kb_rollback(doc_id: str, request: Request,
                      user: dict = Depends(auth.require_admin)) -> Response:
    """
    回滚到某个历史版本（把旧版重新置 active，当前版下线）。

    存在价值：新版上线后发现写错了，把它删掉会让制度整体消失；
    回滚则是"退回到确定能用的那一版"，期间问答始终有依据。
    """
    ctx = request.app.state.ctx
    form = await request.form()
    auth.verify_csrf(request, str(form.get("_csrf", "")))
    raw_version = str(form.get("version", "")).strip()
    if not raw_version.isdigit():
        return _back("请指定要回滚到的版本号")

    conn = db.connect(ctx.settings.chat_db_path)
    try:
        ok = policy_repo.reactivate(conn, doc_id, int(raw_version))
    finally:
        conn.close()
    if not ok:
        return _back("该版本已是当前版本，无需回滚")

    try:
        ctx.index_manager.trigger(reason="rollback")
        msg = f"已回滚到 v{raw_version}，正在重建索引"
    except RebuildBusy:
        msg = f"已回滚到 v{raw_version}，索引正在重建中，完成后生效"
    return _back(msg, ok=True)


@router.post("/admin/kb/rebuild")
async def kb_rebuild(request: Request, user: dict = Depends(auth.require_admin)) -> Response:
    """手动全量重建。改动文件内容（比如直接替换了 md）后靠它生效。"""
    ctx = request.app.state.ctx
    form = await request.form()
    auth.verify_csrf(request, str(form.get("_csrf", "")))
    try:
        ctx.index_manager.trigger(reason="manual")
    except RebuildBusy:
        return _back("索引正在重建，请等待完成")
    return _back("已开始重建索引", ok=True)
