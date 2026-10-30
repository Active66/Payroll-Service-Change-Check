"""一次问答的总调度：检索 → 组装 prompt → 生成 → 护栏 → 组装引用。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from ..utils.console import log
from .config import Settings
from .entities import Citation, RetrievalHit
from .guard import GuardResult, run_guard
from .prompt import PROMPT_VERSION, build_prompt
from .retrieval.bm25 import BM25Index
from .retrieval.embedding import EmbeddingModel
from .retrieval.hybrid import HybridRetriever
from .retrieval.vector_store import VectorStore
from .llm.base import ChatModel, LLMResult


# =============================================================================
# 【模块 14 · 编排器】目标文件：src/my_tool/core/orchestrator.py
# ★核心模块（阶段 2 复用）：一次问答的总调度，把 5 个协作者串起来。
#
#   检索 → 组装 prompt → 生成 → 护栏 → 组装引用
#
#   设计上的两个"故意"：
#     ① 无召回时**不进 LLM**，直接拒答。省 token，也让拒答行为可解释；
#     ② 引用列表由检索结果生成，不由模型输出解析 —— 模型只负责写角标，
#        角标与 chunk_id 的映射权在系统手里，模型无法伪造出处。
#
#   阶段 2 新增 answer_stream()：把"检索完成""增量文本""护栏结论""最终结果"
#   分成四类事件 yield 出去，供 HTTP 层编码成 SSE。
#   两条路径（answer / answer_stream）**共用 _prepare 与 _finalize**，
#   只有"生成"这一段不同，否则 CLI 与网页会慢慢漂移成两种行为。
# =============================================================================

# 无召回时的固定话术。写成常量而不是散落在两处，保证 CLI 与网页看到的是同一句话。
_REFUSAL_TEXT = (
    "根据现有制度资料无法确认该问题：未检索到相关条款。"
    "建议补充变更类型或具体条款主题后重试。"
)

# 追问线索词：短问题里出现这些，说明它离开上一轮就读不懂（"那超过 30 天呢"）
_FOLLOWUP_HINTS = (
    "这个", "那个", "它", "其", "上面", "刚才", "还有", "呢", "那",
    "超过", "低于", "如果", "那么", "这些", "那种",
)


def build_retrieval_query(question: str, history: Sequence[dict[str, str]] | None,
                          settings: Settings) -> str:
    """
    决定"拿什么去检索"，这是多轮问答里最容易被忽略的一步。

    现象：第 2 轮用户只说"那超过 30 天呢？"，单独拿这句去检索，向量和 BM25 都召回不到。
    处理：短问题 + 含指代词 → 把上一轮问题拼进来，**只用于检索**；
          生成时仍然用当前问题原文（强行拼给模型会让它复述上一轮）。

    为什么不用 LLM 改写 query：多一次调用、多 300ms，而且改写本身会引入新的幻觉，
    而 6 轮窗口内的指代用规则能覆盖大部分情况。三个参数都可配。
    """
    q = question.strip()
    if not history or not settings.followup_merge_enabled:
        return q
    if len(q) > settings.followup_max_chars:        # 完整问题不需要补上下文
        return q
    if not any(h in q for h in _FOLLOWUP_HINTS):
        return q
    prev = next((m["content"] for m in reversed(history) if m["role"] == "user"), "").strip()
    if not prev:
        return q
    # 只取上一轮的第一句并截断：上一轮本身可能是长问题，整段拼进来会把 query 带偏
    prev = prev.split("\n")[0][:60]
    return f"{prev} {q}"


@dataclass
class RAGAnswer:
    """一次问答的完整结果，字段与 chat_messages 表对齐，阶段 2 直接落库即可。"""

    question: str
    answer: str
    status: str                                     # answered / partial / refused / error
    citations: list[Citation] = field(default_factory=list)
    hits: list[RetrievalHit] = field(default_factory=list)
    guard: GuardResult | None = None
    debug: dict[str, Any] = field(default_factory=dict)
    prompt_version: str = PROMPT_VERSION
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    query_used: str = ""                            # 实际用于检索的 query（含追问合并的结果）


@dataclass
class StreamEvent:
    """
    流式事件。kind 的取值与职责：

      retrieval —— 检索完成（携 hits / citations / 是否拒答）。
                   放在生成之前发，前端可以先把"参考资料"铺出来，感知更快，
                   也让用户能在等待时更早发现"召回的条款不对"。
      delta     —— 增量文本
      guard     —— 护栏告警（仅当有告警）
      final     —— 收尾，data["answer"] 是完整 RAGAnswer（落库用它）
      error     —— 生成中途失败，data 里带已生成的部分文本
    """

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Prep:
    """检索之后、生成之前的中间态。把 answer() 与 answer_stream() 的公共前缀收在这里。"""

    question: str
    query_used: str
    hits: list[RetrievalHit]
    citations: list[Citation]
    messages: list[dict[str, str]]
    context: str
    debug: dict[str, Any]
    prompt_version: str = PROMPT_VERSION


class Orchestrator:
    """
    一次问答的总调度：检索 → 闸门 → prompt → 生成 → 护栏 → 引用。

    prompt_provider 可选：不传就用内置模板（CLI、评测、单测都不需要接数据库）。
    传了就从数据库取当前生效的模板 —— 后台改完 prompt，下一个请求就会用新的。
    """

    def __init__(self, store: VectorStore, bm25: BM25Index, embedder: EmbeddingModel,
                 chat_model: ChatModel, settings: Settings,
                 prompt_provider: Any = None):
        self.retriever = HybridRetriever(store, bm25, embedder, settings)
        self.chat_model = chat_model
        self.s = settings
        self.prompt_provider = prompt_provider

    # ------------------------------------------------------------------ #
    # 公共前缀：检索 + 拒答判定 + prompt + 引用
    # ------------------------------------------------------------------ #
    def _prepare(self, question: str, top_k: int | None,
                 history: Sequence[dict[str, str]] | None) -> _Prep:
        query_used = build_retrieval_query(question, history, self.s)

        # ① 混合检索（top3）
        hits, rdebug = self.retriever.retrieve(query_used, top_k=top_k)
        rdebug["query_used"] = query_used

        # ② 组装 prompt（history 插在 system 之后、user 之前，不参与检索）
        messages: list[dict[str, str]] = []
        context = ""
        prompt_version = PROMPT_VERSION
        if hits:
            template = self.prompt_provider.active() if self.prompt_provider else None
            messages, context, prompt_version = build_prompt(question, hits, template=template)
            if history:
                # 只在 system 之后插入历史，且不参与检索——检索永远用"当前问题"，
                # 否则多轮之后 query 会被历史污染（架构文档 1.2 步骤②③的分工）。
                messages = [messages[0], *history, messages[-1]]

        return _Prep(
            question=question,
            query_used=query_used,
            hits=hits,
            citations=self._build_citations(hits),
            messages=messages,
            context=context,
            debug=rdebug,
            prompt_version=prompt_version,
        )

    @staticmethod
    def _build_citations(hits: Sequence[RetrievalHit]) -> list[Citation]:
        """
        引用列表由系统生成，模型只负责写角标 —— 这是"模型无法伪造出处"的实现点。

        带上 doc_title 与 source_file，是因为前端要展示"文件名 + 片段"；
        它们本来就冗余在切片里，不需要回表。
        """
        return [
            Citation(
                index=i,
                chunk_id=h.chunk.chunk_id,
                doc_id=h.chunk.doc_id,
                clause_no=h.chunk.clause_range or h.chunk.clause_no,
                heading_path=h.chunk.heading_path,
                score=round(h.fused_score, 6),
                snippet=h.chunk.content[:120],
                doc_title=h.chunk.doc_title,
                source_file=h.chunk.source_file,
            )
            for i, h in enumerate(hits, start=1)
        ]

    def _finalize(self, prep: _Prep, llm: LLMResult, guard: GuardResult, t0: float) -> RAGAnswer:
        return RAGAnswer(
            question=prep.question, answer=llm.text, status=guard.status,
            citations=prep.citations, hits=prep.hits, guard=guard,
            debug={**prep.debug, "t_llm_ms": llm.latency_ms, "provider": llm.provider},
            model=llm.model, tokens_in=llm.tokens_in, tokens_out=llm.tokens_out,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            query_used=prep.query_used,
            prompt_version=prep.prompt_version,
        )

    # ------------------------------------------------------------------ #
    # 非流式：CLI / 评测 / 单测走这条，行为与阶段 1 完全一致
    # ------------------------------------------------------------------ #
    def answer(self, question: str, *, top_k: int | None = None,
               history: Sequence[dict[str, str]] | None = None) -> RAGAnswer:
        t0 = time.perf_counter()
        question = question.strip()
        if not question:
            return RAGAnswer(question=question, answer="请输入有效问题。", status="error")

        prep = self._prepare(question, top_k, history)

        # 无召回 → 直接拒答，不浪费一次生成调用
        if not prep.hits:
            return RAGAnswer(
                question=question,
                answer=_REFUSAL_TEXT,
                status="refused",
                debug={**prep.debug, "refused_reason": "no_recall"},
                latency_ms=int((time.perf_counter() - t0) * 1000),
                query_used=prep.query_used,
                prompt_version=prep.prompt_version,
            )

        try:
            llm = self.chat_model.chat(
                prep.messages,
                temperature=self.s.llm_temperature,
                max_tokens=self.s.llm_max_tokens,
            )
        except Exception as ex:
            log(f"生成失败：{ex}", "err")
            return RAGAnswer(
                question=question, answer=f"生成失败：{ex}", status="error",
                hits=prep.hits, debug=prep.debug,
                latency_ms=int((time.perf_counter() - t0) * 1000),
                query_used=prep.query_used,
            )

        guard = run_guard(llm.text, prep.context, len(prep.hits))
        return self._finalize(prep, llm, guard, t0)

    # ------------------------------------------------------------------ #
    # 流式：HTTP 层把它编码成 SSE
    # ------------------------------------------------------------------ #
    def answer_stream(self, question: str, *, top_k: int | None = None,
                      history: Sequence[dict[str, str]] | None = None) -> Iterator[StreamEvent]:
        t0 = time.perf_counter()
        question = question.strip()
        if not question:
            yield StreamEvent("final", {"answer": RAGAnswer(
                question=question, answer="请输入有效问题。", status="error")})
            return

        prep = self._prepare(question, top_k, history)
        yield StreamEvent("retrieval", {
            "hits": prep.hits,
            "citations": prep.citations,
            "query_used": prep.query_used,
            "refused": not prep.hits,
            "debug": prep.debug,
        })

        # —— 拒答分支：不进 LLM，0 token ——
        # 用 delta 把固定话术发出去，前端不必为此多写一个渲染分支
        if not prep.hits:
            yield StreamEvent("delta", {"text": _REFUSAL_TEXT})
            yield StreamEvent("final", {"answer": RAGAnswer(
                question=question,
                answer=_REFUSAL_TEXT,
                status="refused",
                debug={**prep.debug, "refused_reason": "no_recall"},
                latency_ms=int((time.perf_counter() - t0) * 1000),
                query_used=prep.query_used,
                prompt_version=prep.prompt_version,
            )})
            return

        parts: list[str] = []
        llm: LLMResult | None = None
        try:
            for chunk in self.chat_model.chat_stream(
                prep.messages,
                temperature=self.s.llm_temperature,
                max_tokens=self.s.llm_max_tokens,
            ):
                if chunk.result is not None:      # 收尾帧：带 token 与耗时
                    llm = chunk.result
                    continue
                if not chunk.text:
                    continue
                parts.append(chunk.text)
                yield StreamEvent("delta", {"text": chunk.text})
        except Exception as ex:
            # 已发出的字收不回来，所以这里只上报"生成了多少 + 为什么停"，
            # 由 HTTP 层落库为 error 并发 event: error
            log(f"流式生成失败：{ex}", "err")
            yield StreamEvent("error", {"message": str(ex), "text": "".join(parts)})
            return

        text = "".join(parts)
        if llm is None:
            # 供应商一个 delta 都没给（或只给了 [DONE]）：补一个结果，保证后续逻辑统一
            llm = LLMResult(text=text, model=self.chat_model.model,
                            provider=self.chat_model.provider)
        else:
            llm.text = text                      # 以实际拼出的文本为准，避免首尾空格差异

        guard = run_guard(text, prep.context, len(prep.hits))
        if guard.warnings:
            # 流式与护栏的取舍：边流边判 → 只能事后告警。
            # 前端据此在气泡底部挂一条黄色提示，不阻断阅读。
            yield StreamEvent("guard", {
                "status": guard.status,
                "warnings": guard.warnings,
                "unverified_numbers": guard.unverified_numbers,
                "invalid_citations": guard.invalid_citations,
            })

        yield StreamEvent("final", {"answer": self._finalize(prep, llm, guard, t0)})
