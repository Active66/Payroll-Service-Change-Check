"""对外集成接口 `/api/v1/*`：给 HR 系统、工单系统调用的一次性问答。

    设计取舍（写在这里，免得将来有人"顺手"把它做成内部 /chat 的复刻）：

    ① **非流式**。外部系统要的是"一问一答 + 引用"，流式对它们没有价值，
       反而把 no-transform、心跳、事件顺序这些内部细节变成对外契约负担。

    ② **无会话**。调用方不复用 session_id，服务端也不为它建会话 ——
       外部系统的"多轮"由它自己拼进 question，或者用 `history` 参数显式传。
       不建会话的好处是审计表干净：那里的数据是"员工问的"，这里的是"系统调的"。

    ③ **鉴权走 API Key**，不走 JWT。理由见 store/api_key_repo.py。
       按 Key 做进程内滑动窗口限流（单实例语义，与索引重建一致）。

    ④ **错误码与内部共用一套**（api/errors.py），不另造一套。
       多一套码表就多一处不一致，对调用方也没好处。
"""

import json
import threading
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..store import api_key_repo, db
from ..utils.console import log
from .errors import (AUTH_REQUIRED_ERR, INDEX_NOT_READY, MISSING_JSON,
                     QUESTION_TOO_LONG, EMPTY_QUESTION, TOO_BUSY, ApiError,
                     ok_body, translate_llm_error)
from .schemas import MAX_QUESTION_CHARS, citation_payload

router = APIRouter(prefix="/api/v1")


class KeyLimiter:
    """
    按 Key 的滑动窗口限流。

    进程内、不落库：这个限制的目的是"防止某个集成把服务打满"，
    不是计费。多实例部署时每个实例各自计数，实际上限 = N × limit，可接受；
    真要做全局配额，那是网关或 Redis 的事，不该塞在路由里。
    """

    def __init__(self, window_seconds: int = 60) -> None:
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key_id: str, limit: int) -> int:
        """放行返回 0，超限返回需要等待的秒数。"""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            q = self._hits[key_id]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                return max(1, int(self.window - (now - q[0])) + 1)
            q.append(now)
            return 0


_limiter = KeyLimiter()


def _extract_key(request: Request) -> str:
    """`Authorization: Bearer xxx`，兼容 `X-API-Key: xxx`（很多网关联调时只会配这个）。"""
    raw = request.headers.get("authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def require_api_key(request: Request) -> dict[str, Any]:
    """FastAPI 依赖：校验 Key 并限流。两条失败路径都返回 401/429，不泄露细节。"""
    ctx = request.app.state.ctx
    raw = _extract_key(request)
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        key = api_key_repo.find_active(conn, raw)
    finally:
        conn.close()
    if key is None:
        raise ApiError(AUTH_REQUIRED_ERR, detail={"reason": "invalid_api_key"})

    wait = _limiter.check(str(key["id"]), int(key.get("rate_limit") or 60))
    if wait:
        raise ApiError(TOO_BUSY, detail={"retry_after": wait})

    conn = db.connect(ctx.settings.chat_db_path)
    try:
        api_key_repo.touch(conn, str(key["id"]))
    finally:
        conn.close()
    return key


@router.post("/ask")
async def ask(request: Request, key: dict = Depends(require_api_key)) -> JSONResponse:
    """
    一次性问答。

    入参：
      {"question": "冻结期是几号到几号？", "top_k": 3, "history": [{"role":"user","content":"..."}]}
    出参：
      {"ok": true, "data": {"answer", "status", "citations": [...], "query_used", "prompt_version"}}

    `status` 必须被调用方读到：`refused` 意味着"制度里没有依据"，
    外部系统应该把它当"转人工"的信号，而不是当成一条普通答案展示给员工。
    """
    ctx = request.app.state.ctx
    if ctx.orchestrator is None:
        raise ApiError(INDEX_NOT_READY, detail={"error": ctx.index_error})

    raw = await request.body()
    if not raw:
        raise ApiError(MISSING_JSON, detail={"reason": "body 为空"})
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ApiError(MISSING_JSON, detail={"reason": str(ex)[:200]}) from ex
    if not isinstance(body, dict):
        raise ApiError(MISSING_JSON, detail={"reason": "body 必须是对象"})

    question = str(body.get("question") or "").strip()
    if not question:
        raise ApiError(EMPTY_QUESTION)
    limit = MAX_QUESTION_CHARS
    if len(question) > limit:
        raise ApiError(QUESTION_TOO_LONG, limit=limit)

    top_k = body.get("top_k")
    if isinstance(top_k, bool) or not isinstance(top_k, int):
        top_k = None
    if top_k is not None:
        top_k = max(1, min(int(top_k), 8))      # 夹在 1..8：再多只是把无关条文塞进上下文

    history = body.get("history")
    hist: list[dict[str, str]] | None = None
    if isinstance(history, list) and history:
        # 只收最近 6 条：外部系统常常把整段对话都塞过来，
        # 而编排器的历史预算本来也会裁，早裁一步省 token
        cut = [h for h in history[-6:] if isinstance(h, dict) and h.get("content")]
        hist = [{"role": str(h.get("role", "user"))[:16],
                 "content": str(h.get("content", ""))[:2000]} for h in cut] or None

    def _run() -> Any:
        return ctx.orchestrator.answer(question, top_k=top_k, history=hist)

    try:
        res = await run_in_threadpool(_run)
    except Exception as ex:                     # noqa: BLE001
        log(f"外部问答失败（key={key.get('prefix')}）：{ex}", "err")
        raise translate_llm_error(ex) from ex

    return JSONResponse(ok_body({
        "answer": res.answer,
        "status": res.status,
        "citations": [citation_payload(c) for c in res.citations],
        "query_used": res.query_used,
        "prompt_version": res.prompt_version,
        "model": res.model,
        "latency_ms": res.latency_ms,
        "meta": {
            "api_key": key.get("prefix"),
            "guard": {
                "unverified_numbers": (res.guard.unverified_numbers if res.guard else []),
                "invalid_citations": (res.guard.invalid_citations if res.guard else []),
            },
        },
    }))


@router.get("/healthz")
def healthz(request: Request, key: dict = Depends(require_api_key)) -> JSONResponse:
    """
    对外健康检查。**只暴露调用方需要判断"能不能用"的字段**：
    内部 /healthz 里的库路径、密钥来源、重建细节都不该给外部看。
    """
    ctx = request.app.state.ctx
    m = ctx.manifest or {}
    return JSONResponse(ok_body({
        "ready": ctx.orchestrator is not None,
        "chunk_count": m.get("chunk_count"),
        "doc_count": m.get("doc_count"),
        "built_at": m.get("built_at"),
        "rebuilding": getattr(ctx, "rebuild_state", {}).get("status") == "running",
    }))
