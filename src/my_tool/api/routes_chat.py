"""问答路由。只做 HTTP 层的事：解析 → 校验 → 调编排器 → 编码 SSE。

分层的三条铁律（架构文档 00 节）在这里落地：
  · 不出现 SQL —— 全部在 store/chat_repo.py；
  · 不出现 prompt / 检索逻辑 —— 全部在 core/orchestrator.py；
  · 不构造 LLM 请求 —— 全部在 core/llm/。
"""

import json
from typing import Any, Iterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..store import chat_repo, db
from ..utils.console import log
from . import auth, sse
from .errors import (INTERNAL, INDEX_NOT_READY, MESSAGE_NOT_FOUND, MISSING_JSON,
                     SESSION_BUSY, SESSION_DELETED, SESSION_NOT_FOUND, ApiError,
                     ok_body, translate_llm_error)
from .schemas import (ChatRequest, citation_payload, message_payload,
                      parse_chat_request)

router = APIRouter()


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
async def _read_json(request: Request) -> Any:
    """读请求体并解析。空体与坏 JSON 都归到 REQ_001，不暴露解析器原始异常。"""
    raw = await request.body()
    if not raw:
        raise ApiError(MISSING_JSON, detail={"reason": "empty body"})
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ApiError(MISSING_JSON, detail={"reason": str(ex)[:200]}) from ex


def _model_label(ctx: Any) -> str:
    s = ctx.settings
    return s.llm_model or f"{s.llm_provider}-default"


# --------------------------------------------------------------------------- #
# 流开始之前的准备工作（跑在线程池里，因为它要碰 SQLite）
# --------------------------------------------------------------------------- #
def _prepare_turn(ctx: Any, req: ChatRequest, trace_id: str, *,
                  user_id: str, is_admin: bool = False) -> dict[str, Any]:
    """
    校验会话、建会话、写 user 消息与 assistant 占位行、取最近 6 轮历史。

    为什么这段放在"返回 StreamingResponse 之前"而不是生成器里：
    只有这样才能在**响应头发出之前**发现"会话不存在""索引没建好"，
    从而返回真正的 404 / 503。放进生成器里就只能变成 200 + event: error，
    前端没法按状态码做区分处理。

    归属校验（本期新增）：非管理员只能操作自己的会话。越权时统一返回
    "会话不存在"而不是"无权限" —— 后者等于告诉攻击者"这个 id 确实存在"。
    """
    s = ctx.settings
    if ctx.orchestrator is None:
        # 索引没加载 → 直接告诉调用方去 build，而不是让第一个用户等到超时
        raise ApiError(INDEX_NOT_READY, detail={"reason": (ctx.index_error or "")[:300]})

    conn = db.connect(s.chat_db_path)
    try:
        sid = req.session_id
        if sid:
            sess = chat_repo.get_session(conn, sid)
            if sess is None:
                raise ApiError(SESSION_NOT_FOUND, detail={"session_id": sid})
            if sess.get("deleted_at"):
                raise ApiError(SESSION_DELETED, detail={"session_id": sid})
            owner = sess.get("user_id")
            # owner 为空是历史数据（阶段 2 之前没有登录态），按无主处理放行，
            # 否则老会话在接入鉴权后会集体"消失"，看起来像数据丢了。
            if not is_admin and owner and owner != user_id:
                raise ApiError(SESSION_NOT_FOUND, detail={"session_id": sid})
            if chat_repo.has_streaming(conn, sid):
                # 同一会话只允许一个进行中的回答：否则两段回答会交错写入 seq
                raise ApiError(SESSION_BUSY, detail={"session_id": sid})
        else:
            sid = chat_repo.create_session(
                conn, user_id=user_id, legal_entity_id=s.legal_entity_id,
                model=_model_label(ctx),
            )["session_id"]

        turn = chat_repo.begin_turn(
            conn, session_id=sid, question=req.question,
            model=_model_label(ctx), trace_id=trace_id,
        )
        history = chat_repo.recent_history(
            conn, sid, turn["user_seq"],
            max_turns=s.history_max_turns,
            max_chars=s.history_max_chars,
            msg_max_chars=s.history_msg_max_chars,
        )
    finally:
        conn.close()

    return {
        "session_id": sid,
        "question": req.question,
        "history": history,
        "trace_id": trace_id,
        **turn,
    }


def _persist_turn(ctx: Any, turn: dict[str, Any], *, content: str,
                  citations: list[dict[str, Any]], status: str,
                  token_in: int = 0, token_out: int = 0,
                  latency_ms: int | None = None, model: str = "",
                  prompt_version: str = "") -> None:
    """落库（T2）。失败只记日志、不打断流 —— 用户已经看到答案了，不能因为写库失败把气泡弄坏。"""
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        chat_repo.finish_turn(
            conn, turn["assistant_message_id"], content=content, citations=citations,
            answer_status=status, token_in=token_in, token_out=token_out,
            latency_ms=latency_ms, model=model, prompt_version=prompt_version,
        )
    except Exception as ex:                       # noqa: BLE001 — 兜底，绝不影响已发出去的流
        log(f"写入助手消息失败（message_id={turn.get('assistant_message_id')}）：{ex}", "err")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# SSE 生成器（同步生成器：Starlette 会自动放进线程池迭代）
# --------------------------------------------------------------------------- #
def _stream_turn(ctx: Any, turn: dict[str, Any]) -> Iterator[str]:
    """
    一条回答的完整事件流。

    注意这个函数是**同步生成器**：内部调用的 orchestrator / sqlite 全是阻塞 API。
    如果写成 async def 并在里面直接调用它们，会阻塞事件循环，
    一个用户的生成会卡住所有人的请求。
    """
    persisted = False

    def persist_once(**kw: Any) -> None:
        nonlocal persisted
        if persisted:
            return
        _persist_turn(ctx, turn, **kw)
        persisted = True

    yield sse.frame("session", {
        "session_id": turn["session_id"],
        "user_message_id": turn["user_message_id"],
        "seq": turn["assistant_seq"],
    })
    yield sse.frame("status", {"stage": "retrieving", "text": "正在检索制度…"})

    partial: list[str] = []
    citations: list[dict[str, Any]] = []

    # 关于心跳：真正的"生成中每 15 秒发一帧"需要把上游迭代放进工作线程 + 队列，
    # 否则 `next()` 阻塞期间生成器根本没有机会 yield。这一版的取舍是：
    # 靠 citations/delta 的持续流量保活，若上游出现长时间静默（>60s）再由 nginx 报错。
    # 触发条件是"模型思考超过 1 分钟"，在 900 token 上限下极少发生，故先不做。
    try:
        for ev in ctx.orchestrator.answer_stream(
            turn["question"], history=turn["history"]
        ):
            # —— 检索完成：先把引用铺出来，前端可以边等生成边看依据 ——
            if ev.kind == "retrieval":
                citations = [citation_payload(c) for c in ev.data["citations"]]
                if citations:
                    yield sse.frame("citations", {"citations": citations})
                    yield sse.frame("status", {"stage": "generating",
                                              "text": "正在组织回答…"})
                else:
                    # 没有召回：接下来只会收到一段固定拒答话术
                    yield sse.frame("status", {"stage": "refused",
                                              "text": "没有检索到相关制度条款"})
                continue

            # —— 增量文本 ——
            if ev.kind == "delta":
                text = ev.data.get("text", "")
                if not text:
                    continue
                partial.append(text)
                yield sse.frame("delta", {"text": text})
                continue

            # —— 护栏告警（流式只能事后告警，不能撤回已发出的字）——
            if ev.kind == "guard":
                yield sse.frame("guard", ev.data)
                continue

            # —— 生成失败 ——
            if ev.kind == "error":
                err = translate_llm_error(RuntimeError(ev.data.get("message", "")))
                text = ev.data.get("text") or "".join(partial)
                persist_once(content=text or "（生成失败，未产生内容）",
                             citations=citations, status="error")
                yield sse.error_frame(err.code, err.message, turn["trace_id"], err.detail)
                return

            # —— 收尾 ——
            if ev.kind == "final":
                ans = ev.data["answer"]
                # 先落库再发 done：done 是"客户端可以据此收尾"的信号，
                # 若此时写库失败，刷新页面会看不到这条回答，顺序反了更容易察觉问题
                persist_once(content=ans.answer, citations=citations, status=ans.status,
                             token_in=ans.tokens_in, token_out=ans.tokens_out,
                             latency_ms=ans.latency_ms, model=ans.model,
                             prompt_version=ans.prompt_version)
                yield sse.frame("done", {
                    "message_id": turn["assistant_message_id"],
                    "status": ans.status,
                    "usage": {"tokens_in": ans.tokens_in, "tokens_out": ans.tokens_out,
                              "latency_ms": ans.latency_ms},
                    "trace_id": turn["trace_id"],
                    "query_used": ans.query_used,
                    "prompt_version": ans.prompt_version,
                    "citations_count": len(citations),
                })
                return

    except GeneratorExit:
        # 客户端断开（关页面 / 点停止 / 网络断）。已生成的文本要留档：
        # 审计要能复现"用户到底看到了什么"，所以记 error 而不是删掉这条。
        persist_once(content="".join(partial) or "（用户中断，未生成内容）",
                     citations=citations, status="error")
        raise
    except Exception as ex:                       # noqa: BLE001
        log(f"流式回答异常：{ex}", "err")
        persist_once(content="".join(partial) or "（生成失败，未产生内容）",
                     citations=citations, status="error")
        yield sse.error_frame(INTERNAL.code, INTERNAL.message.format(
            trace_id=turn["trace_id"]), turn["trace_id"])


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@router.post("/chat")
async def chat(request: Request, user: dict = Depends(auth.current_user)) -> StreamingResponse:
    """提问，SSE 流式返回。校验/会话类错误在返回响应之前抛出（真 4xx），流中错误走 event。"""
    ctx = request.app.state.ctx
    trace_id = getattr(request.state, "trace_id", "")
    body = await _read_json(request)
    req = parse_chat_request(body)

    # 模式分派：agent 走工具循环，否则走固定检索流程。
    # 默认取配置 agent_default_mode（默认 "rag"，即阶段 1-4 的行为），
    # 想全局切到 Agent 只需设 AGENT_DEFAULT_MODE=agent，不用改前端。
    mode = req.mode
    if mode == "rag" and ctx.settings.agent_enabled:
        mode = ctx.settings.agent_default_mode
    if mode == "agent" and not ctx.settings.agent_enabled:
        mode = "rag"                       # 功能被关时静默回退，不给用户报错

    turn = await run_in_threadpool(
        _prepare_turn, ctx, req, trace_id,
        user_id=user["id"], is_admin=(user.get("role") == "admin"),
    )
    # 把 user 信息塞进 turn，Agent 的 ToolContext 要用它做权限判断
    turn["user_id"] = user["id"]
    turn["user_role"] = user.get("role", "employee")

    streamer = _stream_agent_turn if mode == "agent" else _stream_turn
    return StreamingResponse(
        streamer(ctx, turn),
        media_type="text/event-stream",
        headers=sse.SSE_HEADERS,
    )


# --------------------------------------------------------------------------- #
# Agent 流式生成器
# --------------------------------------------------------------------------- #
# Agent 的"流程状态"与 chat_messages.answer_status 的"答案质量"不是一回事。
# 前者多出 drafted / escalated / inconsistent / degraded，而后者有 CHECK 约束
# 只认 answered/partial/refused/error/streaming。落库时把流程状态映射回质量状态：
#   · drafted / escalated → answered（答案本身有效，流程只是停在"等确认"）
#   · inconsistent / degraded → partial（需要复核）
# done 事件仍返回原始状态给前端，因为前端要据此展示"已建草稿""已转人工"。
AGENT_STATUS_TO_DB = {
    "answered": "answered",
    "drafted": "answered",
    "escalated": "answered",
    "awaiting_confirmation": "answered",   # 草稿待确认：对话消息本身是正常答复
    "degraded": "partial",
    "inconsistent": "partial",
    "error": "error",
}


def _stream_agent_turn(ctx: Any, turn: dict[str, Any]) -> Iterator[str]:
    """
    Agent 模式的事件流。与 _stream_turn 的差别：

      · 检索不再是固定一步，而是模型自己决定调什么工具（tool_call/tool_result）
      · 最终答案在**所有工具执行完之后**才产生，作为一帧 delta 发出
      · 事件里多了 tool_call / tool_result，前端可以在状态条上展示"正在做什么"

    会话落库复用 _prepare_turn（占位行）与 _persist_turn（收尾），
    所以 Agent 的对话同样出现在后台审计页里 —— 只是 citations 字段更精简。
    """
    from ..core.agent.loop import AgentLoop
    from ..core.agent.registry import ToolContext
    from ..core.agent.toolset import build_registry
    from ..core.llm import build_chat_model
    from ..core.llm.mock_agent import MockToolCallingModel, script_happy_path

    persisted = False

    def persist_once(**kw: Any) -> None:
        nonlocal persisted
        if persisted:
            return
        _persist_turn(ctx, turn, **kw)
        persisted = True

    yield sse.frame("session", {
        "session_id": turn["session_id"],
        "user_message_id": turn["user_message_id"],
        "seq": turn["assistant_seq"],
    })
    yield sse.frame("status", {"stage": "analyzing",
                               "text": "正在分析，可能需要查询制度与员工信息…"})

    s = ctx.settings
    # 模型：mock provider 用脚本化假 Agent（离线演示），其余走真实兼容实现
    if s.llm_provider == "mock":
        model = MockToolCallingModel(script_happy_path())
        log("Agent 模式使用脚本化假模型（LLM_PROVIDER=mock）；接真模型请配 "
            "LLM_PROVIDER=openai_compat", "warn")
    else:
        model = build_chat_model(s)

    retriever = getattr(ctx.orchestrator, "retriever", None)
    loop = AgentLoop(registry=build_registry(), model=model, settings=s,
                     retriever=retriever, fallback=ctx.orchestrator)
    tool_ctx = ToolContext(
        settings=s,
        user={"id": turn["user_id"], "role": turn["user_role"]},
        session_id=turn["session_id"],
        retriever=retriever)

    final: dict[str, Any] = {}
    for ev in loop.stream(turn["question"], ctx=tool_ctx, history=turn["history"]):
        kind = ev.kind
        if kind == "thought":
            yield sse.frame("status", {"stage": "analyzing",
                                       "text": (ev.data.get("text") or "正在思考…")[:48]})
        elif kind == "tool_call":
            yield sse.frame("tool_call", ev.data)
        elif kind == "tool_result":
            yield sse.frame("tool_result", ev.data)
        elif kind == "confirmation_required":
            # 建单被拦截：把草稿预览单独发一帧，前端据此弹出确认卡片
            yield sse.frame("confirmation_required", ev.data)
        elif kind == "final":
            final = ev.data

    answer = final.get("answer") or "（未能生成答复）"
    status = final.get("status") or "error"
    tokens = final.get("tokens") or {}
    citations = final.get("citations") or []

    # awaiting_confirmation：草稿还没建，落库的只是"待确认"这条对话消息。
    # 真正落单在员工点确认后由 /api/agent/confirm-draft 完成
    db_status = AGENT_STATUS_TO_DB.get(status, "partial")
    persist_once(
        content=answer, citations=citations, status=db_status,
        token_in=int(tokens.get("in", 0)), token_out=int(tokens.get("out", 0)),
        latency_ms=final.get("latency_ms"), model="agent",
    )

    # 最终答案整段发给前端，由打字机逐字显示
    yield sse.frame("delta", {"text": answer})
    yield sse.frame("done", {
        "status": status,
        "turns": final.get("turns"),
        "tool_calls": len(final.get("tool_calls") or []),
        "draft_id": final.get("draft_id") or "",
        "order_no": final.get("order_no") or "",
        "draft_preview": final.get("draft_preview"),
        "degraded": final.get("degraded") or False,
        "usage": {"tokens_in": tokens.get("in", 0), "tokens_out": tokens.get("out", 0)},
    })


@router.post("/sessions")
async def create_session(request: Request,
                         user: dict = Depends(auth.current_user)) -> JSONResponse:
    """新建会话。body 可空（全部取默认值）。"""
    ctx = request.app.state.ctx
    raw = await request.body()
    body: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            body = parsed if isinstance(parsed, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as ex:
            raise ApiError(MISSING_JSON, detail={"reason": str(ex)[:200]}) from ex

    title = body.get("title")
    scene = body.get("scene") or "employee"

    def _create() -> dict[str, Any]:
        conn = db.connect(ctx.settings.chat_db_path)
        try:
            return chat_repo.create_session(
                conn, user_id=user["id"],
                legal_entity_id=ctx.settings.legal_entity_id,
                scene=str(scene), title=(str(title)[:128] if title else None),
                model=_model_label(ctx),
            )
        finally:
            conn.close()

    return JSONResponse(ok_body(await run_in_threadpool(_create)))


@router.get("/sessions/{session_id}/messages")
def list_messages(session_id: str, request: Request, limit: int = 50,
                  user: dict = Depends(auth.current_user)) -> JSONResponse:
    """按 seq 正序返回历史消息，供刷新页面后恢复对话（含引用）。"""
    ctx = request.app.state.ctx
    is_admin = user.get("role") == "admin"
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        sess = chat_repo.get_session(conn, session_id)
        if sess is None or sess.get("deleted_at"):
            raise ApiError(SESSION_NOT_FOUND, detail={"session_id": session_id})
        # 越权与不存在返回同一个错误：不让调用方通过错误码差异探测会话是否存在
        if not is_admin and sess.get("user_id") and sess["user_id"] != user["id"]:
            raise ApiError(SESSION_NOT_FOUND, detail={"session_id": session_id})
        rows = chat_repo.list_messages(conn, session_id, limit=limit)
    finally:
        conn.close()

    return JSONResponse(ok_body({
        "session_id": session_id,
        "title": sess.get("title"),
        "messages": [message_payload(r) for r in rows],
        "has_more": len(rows) >= max(1, min(limit, 200)),
    }))


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    """
    自检。**索引没建好也返回 200**——健康检查的职责是"描述坏"，不是"自己也坏掉"。
    真正该返回 503 的是 /chat。
    """
    ctx = request.app.state.ctx
    s = ctx.settings
    m = ctx.manifest or {}
    db_ready = True
    try:
        conn = db.connect(s.chat_db_path)
        conn.execute("SELECT 1")
        conn.close()
    except Exception:                             # noqa: BLE001
        db_ready = False

    return JSONResponse(ok_body({
        "index": {
            "ready": ctx.orchestrator is not None,
            "chunk_count": m.get("chunk_count"),
            "embedding_model": m.get("embedding_model"),
            "doc_version": m.get("doc_version"),
            "source_file": m.get("source_file"),
            "built_at": m.get("built_at"),
            "error": ctx.index_error,
            "rebuild": getattr(ctx, "rebuild_state", {}).get("status", "idle"),
        },
        "llm": {"provider": s.llm_provider, "model": _model_label(ctx)},
        "db": {"ready": db_ready, "path": str(s.chat_db_path)},
        # 鉴权状态对外可见：排查"为什么后台打不开"时，第一件事就是看这个
        "auth": {
            "required": s.auth_required,
            "secret_source": "env" if s.auth_secret else "file",
        },
    }))


@router.post("/messages/{message_id}/feedback")
def feedback(message_id: int, request: Request,
             payload: dict[str, Any] | None = None,
             user: dict = Depends(auth.current_user)) -> JSONResponse:
    """有用 / 无用。写 chat_messages.feedback，为阶段 3 扩评测集用。"""
    ctx = request.app.state.ctx
    value = (payload or {}).get("feedback")
    if value not in (1, -1):
        raise ApiError(MISSING_JSON, detail={"reason": "feedback 只能是 1 或 -1"})

    is_admin = user.get("role") == "admin"
    conn = db.connect(ctx.settings.chat_db_path)
    try:
        owner = chat_repo.message_owner(conn, message_id)
        if owner is None:
            raise ApiError(MESSAGE_NOT_FOUND, detail={"message_id": message_id})
        if not is_admin and owner.get("user_id") and owner["user_id"] != user["id"]:
            # 与"不存在"同一个错误：不让调用方用自增 id 探测别人的消息
            raise ApiError(MESSAGE_NOT_FOUND, detail={"message_id": message_id})
        changed = chat_repo.set_feedback(conn, message_id, int(value))
    finally:
        conn.close()
    if not changed:
        raise ApiError(MESSAGE_NOT_FOUND, detail={"message_id": message_id})

    return JSONResponse(ok_body({"message_id": message_id, "feedback": value}))
