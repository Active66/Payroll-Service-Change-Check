"""请求解析与响应负载。**手写校验而不是用 pydantic**：为了完全掌控错误码与文案。

原因很实际：FastAPI 的自动校验会返回 422 + 它自己的 `{"detail":[{...}]}` 结构，
与本项目的统一错误契约（`{ok:false, error:{code,message,trace_id}}`）不一致。
要么写一个把 pydantic 错误映射成业务码的适配层（映射关系脆弱、随版本变化），
要么直接手写。这个接口的入参只有三个字段，手写更短、更可控。
"""

import re
from dataclasses import dataclass
from typing import Any

from ..core.entities import Citation
from .errors import (BAD_SESSION_ID, EMPTY_QUESTION, MISSING_FIELD, MISSING_JSON,
                     QUESTION_TOO_LONG, ApiError)

MAX_QUESTION_CHARS = 500

# 会话 id：32 位十六进制（本系统生成）或标准 UUID（兼容外部导入）
_SESSION_RE = re.compile(r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
                         r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


@dataclass
class ChatRequest:
    session_id: str | None
    question: str
    as_of: str | None = None
    mode: str = "rag"           # rag = 固定检索流程；agent = 工具调用循环


def parse_chat_request(body: Any) -> ChatRequest:
    """
    校验并归一化请求体。任何不合规都抛 ApiError（带具体错误码与中文文案），
    由全局处理器翻译成响应，路由里不再做二次判断。
    """
    if not isinstance(body, dict):
        raise ApiError(MISSING_JSON)

    if "question" not in body or body["question"] is None:
        raise ApiError(MISSING_FIELD, field="question")

    question = body["question"]
    if not isinstance(question, str):
        raise ApiError(MISSING_FIELD, field="question")
    question = question.strip()
    if not question:
        raise ApiError(EMPTY_QUESTION)
    if len(question) > MAX_QUESTION_CHARS:
        # 上限存在的意义：单条消息过长时，检索质量会下降（query 被稀释），
        # 且历史窗口的 token 预算会被一条消息吃光。
        raise ApiError(QUESTION_TOO_LONG, limit=MAX_QUESTION_CHARS)

    session_id = body.get("session_id")
    if session_id in (None, ""):
        session_id = None                      # 空 → 服务端新建会话
    elif not isinstance(session_id, str) or not _SESSION_RE.match(session_id):
        raise ApiError(BAD_SESSION_ID, detail={"session_id": str(session_id)[:64]})

    as_of = body.get("as_of")
    if as_of is not None and not isinstance(as_of, str):
        raise ApiError(MISSING_FIELD, field="as_of")

    # mode：只认两个合法值，其余一律按 rag（宁可退回固定流程，也不让一个
    # 拼错的 mode 值把请求打到未定义的路径上）
    mode = body.get("mode")
    mode = mode if mode in ("rag", "agent") else "rag"

    return ChatRequest(session_id=session_id, question=question, as_of=as_of,
                       mode=mode)


def _one_line(text: str, limit: int = 140) -> str:
    """
    把片段压成一行。切片正文里有换行与章节标题，原样丢给前端会出现
    "第三章 提交时限…\\n第七条…" 这种断行，卡片高度忽高忽低。
    压缩空白不影响内容，只是排版。
    """
    return re.sub(r"\s+", " ", (text or "")).strip()[:limit]


def citation_payload(c: Citation) -> dict[str, Any]:
    """
    引用 → 前端可渲染的结构。

    前端要展示的是"文件名 + 片段"，所以 doc_title / source_file 一定要带；
    chunk_id 不是给人看的，但保留它，用户报障时能凭它回到具体切片。
    """
    return {
        "index": c.index,
        "doc_id": c.doc_id,
        "doc_title": c.doc_title,
        "source_file": c.source_file,
        "clause_no": c.clause_no,
        "heading_path": c.heading_path,
        "chunk_id": c.chunk_id,
        "snippet": _one_line(c.snippet),
        "score": c.score,
    }


def message_payload(row: dict[str, Any]) -> dict[str, Any]:
    """chat_messages 的行 → 前端消息对象。user 消息没有 answer_status，给 null 而非空串。"""
    return {
        "id": row["id"],
        "seq": row["seq"],
        "role": row["role"],
        "content": row["content"],
        "answer_status": row.get("answer_status") if row["role"] == "assistant" else None,
        "citations": row.get("citations") or [],
        "created_at": row.get("created_at"),
    }
