"""越界问题（制度里查不到）的分支测试。

这里守的是四条边界，任何一条破了都意味着"合规问答"变成了"随便聊"：

  ① 默认仍是拒答，且**不调用模型**（0 token）—— 开关必须是显式打开；
  ② 打开后必须带系统拼的"非制度依据"声明，模型漏写也照样有；
  ③ 通用分支**不跑护栏**：没有 context 时护栏会把每个数字判成未溯源，
     结果全变 partial，等于把功能做废；
  ④ 对外 API 与 Agent 模式不受这个开关影响（它们不该出现通用知识）。
"""

import dataclasses
import json

import pytest

from my_tool.core.config import Settings
from my_tool.core.entities import Chunk, RetrievalHit
from my_tool.core.llm.base import ChatModel, LLMResult
from my_tool.core.orchestrator import Orchestrator
from my_tool.core.prompt import GENERAL_PROMPT_VERSION, OFFSCOPE_NOTICE
from my_tool.store import chat_repo, db, stats_repo


class _SpyModel(ChatModel):
    """记录被调用次数的假模型。顺带在文本里塞一个带单位的数字，用来验证护栏被跳过。"""

    provider = "spy"
    model = "spy-1"

    def __init__(self, text: str = "一般按公司惯例处理，举例说约 123 元 这类金额需以HR答复为准。"):
        self.calls = 0
        self.text = text
        self.last_messages: list[dict[str, str]] = []

    def chat(self, messages, *, temperature: float = 0.2, max_tokens: int = 900) -> LLMResult:
        self.calls += 1
        self.last_messages = messages
        return LLMResult(text=self.text, model=self.model, provider=self.provider,
                         tokens_in=100, tokens_out=20, latency_ms=5)


@pytest.fixture
def s(tmp_path) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(
        base,
        index_dir=tmp_path / "idx",
        chat_db_path=tmp_path / "chat.db",
        upload_dir=tmp_path / "up",
        embedding_provider="hash",
        llm_provider="mock",
    )


def _hit() -> RetrievalHit:
    chunk = Chunk(
        chunk_id="POL-CMP-2026-001#c001", doc_id="POL-CMP-2026-001", chunk_no=1,
        content="每月 25 日至当月月末为薪酬封版冻结期。",
        heading_path="第二章 变更类型与受理范围 > 第八条 冻结期",
        clause_no="第八条", clause_range="第八条", char_len=20, token_count=18,
        doc_title="员工薪酬变更核对制度", source_file="员工薪酬变更核对制度_v1.0.md",
    )
    return RetrievalHit(chunk=chunk, fused_score=0.9, vector_score=0.5, bm25_score=9.0)


def _orch(s: Settings, model: ChatModel, hits: list[RetrievalHit] | None = None) -> Orchestrator:
    """
    直接构造编排器并把检索结果打桩。

    不搭真索引：这里要测的是"检索结果为空之后怎么走"，
    真索引只会让用例变慢、且把断言从"分支逻辑"变成"检索质量"。
    """
    o = Orchestrator(store=None, bm25=None, embedder=None, chat_model=model, settings=s)
    got = list(hits or [])
    o.retriever.retrieve = lambda q, top_k=None, as_of=None: (
        got, {"gate": "passed" if got else "blocked_no_evidence"})
    return o


# --------------------------------------------------------------------------- #
# ① 默认行为不变
# --------------------------------------------------------------------------- #
def test_default_is_refuse_and_model_never_called(s):
    assert s.offscope_answer == "refuse", "默认必须是拒答，不能因为加了开关就悄悄放开"

    model = _SpyModel()
    ans = _orch(s, model).answer("今天北京天气怎么样")

    assert ans.status == "refused"
    assert ans.scope == "policy"
    assert "无法确认" in ans.answer
    assert model.calls == 0, "拒答分支不该花 token 调模型"
    assert ans.tokens_in == 0 and ans.tokens_out == 0


def test_invalid_value_falls_back_to_refuse(tmp_path):
    """配置写错时该更严格，而不是更宽松。"""
    import os
    from my_tool.core.config import Settings as S

    old = os.environ.get("OFFSCOPE_ANSWER")
    os.environ["OFFSCOPE_ANSWER"] = "generl"      # 拼错
    try:
        assert S.from_env().offscope_answer == "refuse"
        os.environ["OFFSCOPE_ANSWER"] = "GENERAL"  # 大小写不敏感
        assert S.from_env().offscope_answer == "general"
    finally:
        if old is None:
            os.environ.pop("OFFSCOPE_ANSWER", None)
        else:
            os.environ["OFFSCOPE_ANSWER"] = old


# --------------------------------------------------------------------------- #
# ② 开启后的行为
# --------------------------------------------------------------------------- #
def test_general_branch_answers_with_forced_notice(s):
    s2 = dataclasses.replace(s, offscope_answer="general")
    model = _SpyModel()
    ans = _orch(s2, model).answer("年终奖一般怎么算")

    assert model.calls == 1
    assert ans.status == "offscope"
    assert ans.scope == "offscope"
    assert ans.answer.startswith(OFFSCOPE_NOTICE), "声明必须由系统拼在最前，不能指望模型"
    assert ans.citations == [] and ans.hits == []
    assert ans.prompt_version == GENERAL_PROMPT_VERSION


def test_general_prompt_has_no_context_and_no_citation_rules(s):
    s2 = dataclasses.replace(s, offscope_answer="general")
    model = _SpyModel()
    _orch(s2, model).answer("年终奖一般怎么算")

    system_text = model.last_messages[0]["content"]
    user_text = model.last_messages[-1]["content"]
    # 关键：不能残留"必须标角标""资料没给就说未找到"这类制度约束，
    # 否则模型会照旧拒答，开关等于没开。
    # 注意不能简单断言 "[1]" 不在 system 里 —— 通用提示词本身就写着
    # "不要写 [1][2] 这类角标"，那是反向约束。这里查的是强制块是否还在。
    assert "【资料】" not in user_text
    assert "【回答格式（系统强制" not in system_text
    assert "制度中未找到" not in system_text
    assert user_text == "年终奖一般怎么算", "通用分支只发问题，不拼上下文"


def test_general_branch_skips_guard(s):
    """
    答案里有「123 元」这种 context 里找不到的数字，护栏会判 partial。
    通用分支不该跑护栏 —— 没有原文可核，跑出来的结论只有噪声。
    """
    s2 = dataclasses.replace(s, offscope_answer="general")
    ans = _orch(s2, _SpyModel()).answer("年终奖一般怎么算")

    assert ans.guard is None
    assert ans.status == "offscope", "不能被护栏改成 partial"


def test_allow_offscope_false_still_refuses(s):
    """对外 API 走这条：即使全局开了 general，也不给它通用回答。"""
    s2 = dataclasses.replace(s, offscope_answer="general")
    model = _SpyModel()
    ans = _orch(s2, model).answer("年终奖一般怎么算", allow_offscope=False)

    assert ans.status == "refused"
    assert model.calls == 0


# --------------------------------------------------------------------------- #
# ③ 制度内问题不受影响
# --------------------------------------------------------------------------- #
def test_in_scope_question_keeps_policy_path(s):
    s2 = dataclasses.replace(s, offscope_answer="general")
    model = _SpyModel(text="每月 25 日起为冻结期[1]。")
    ans = _orch(s2, model, hits=[_hit()]).answer("冻结期是什么时候")

    assert ans.status == "answered"
    assert ans.scope == "policy"
    assert len(ans.citations) == 1
    assert ans.guard is not None
    assert "【资料】" in model.last_messages[-1]["content"]


# --------------------------------------------------------------------------- #
# ④ 流式
# --------------------------------------------------------------------------- #
def _collect(gen):
    return [(ev.kind, ev.data) for ev in gen]


def test_stream_emits_notice_as_first_delta(s):
    s2 = dataclasses.replace(s, offscope_answer="general")
    events = _collect(_orch(s2, _SpyModel()).answer_stream("年终奖一般怎么算"))

    deltas = [d for k, d in events if k == "delta"]
    assert deltas and deltas[0]["text"] == OFFSCOPE_NOTICE

    retrieval = next(d for k, d in events if k == "retrieval")
    assert retrieval["offscope"] is True
    assert retrieval["refused"] is False, "通用分支不能被前端标成「未找到依据」"

    assert not [k for k, _ in events if k == "guard"], "通用分支不发护栏告警"

    final = next(d for k, d in events if k == "final")["answer"]
    assert final.status == "offscope"
    assert final.answer.startswith(OFFSCOPE_NOTICE)
    assert final.answer.endswith(_SpyModel().text)


def test_stream_without_offscope_unchanged(s):
    events = _collect(_orch(s, _SpyModel()).answer_stream("今天北京天气怎么样"))
    retrieval = next(d for k, d in events if k == "retrieval")
    assert retrieval["refused"] is True and retrieval["offscope"] is False
    final = next(d for k, d in events if k == "final")["answer"]
    assert final.status == "refused"


# --------------------------------------------------------------------------- #
# ⑤ 落库与统计口径
# --------------------------------------------------------------------------- #
def test_sse_persists_offscope_as_answered_with_scope_flag(tmp_path, s):
    """
    端到端（不走 HTTP，直接驱动 SSE 生成器）：通用回答必须落得进库。

    `answer_status` 的 CHECK 约束只认 answered/partial/refused/error/streaming。
    如果直接把 offscope 写进去，SQLite 会拒绝整条 UPDATE —— 而失败发生在异步落库
    路径里，表现为"页面有答案、刷新就没了"，线上极难定位。这条用例守的就是这层映射。
    """
    from types import SimpleNamespace

    from my_tool.api.routes_chat import _stream_turn

    s2 = dataclasses.replace(s, offscope_answer="general")
    db.init_db(s2.chat_db_path)
    conn = db.connect(s2.chat_db_path)
    try:
        sess = chat_repo.create_session(conn, user_id="u1", legal_entity_id="e1")
        turn = chat_repo.begin_turn(conn, session_id=sess["session_id"], question="年终奖怎么算")
    finally:
        conn.close()

    ctx = SimpleNamespace(settings=s2, orchestrator=_orch(s2, _SpyModel()))
    frames = list(_stream_turn(ctx, {
        "session_id": sess["session_id"],
        "user_message_id": turn["user_message_id"],
        "assistant_message_id": turn["assistant_message_id"],
        "assistant_seq": turn["assistant_seq"],
        "question": "年终奖怎么算",
        "history": [],
        "trace_id": "t-offscope",
    }))
    body = "".join(frames)
    assert '"status": "offscope"' in body or '"status":"offscope"' in body

    conn = db.connect(s2.chat_db_path)
    try:
        row = conn.execute(
            "SELECT answer_status, scope, citations FROM chat_messages WHERE id = ?",
            (turn["assistant_message_id"],)).fetchone()
    finally:
        conn.close()

    assert row["answer_status"] == "answered", "offscope 落库必须映射成 answered"
    assert row["scope"] == "offscope"
    assert json.loads(row["citations"]) == []


def test_scope_column_migrates_and_counts_separately(tmp_path):
    db_path = tmp_path / "c.db"
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(chat_messages)")}
        assert "scope" in cols, "老库必须有这一列，否则 UPDATE 会报 no such column"

        sess = chat_repo.create_session(conn, user_id="u1", legal_entity_id="e1")
        turn = chat_repo.begin_turn(conn, session_id=sess["session_id"], question="年终奖怎么算")
        chat_repo.finish_turn(
            conn, turn["assistant_message_id"], content="通用解释",
            answer_status="answered", scope="offscope", model="spy-1")

        row = conn.execute("SELECT scope FROM chat_messages WHERE id = ?",
                           (turn["assistant_message_id"],)).fetchone()
        assert row["scope"] == "offscope"

        counts = stats_repo.today_counts(conn)
        # 通用回答不能混进 answered，否则"多聊几句拒答率就降下来了"
        assert counts["offscope"] == 1
        assert counts["answered"] == 0
        assert counts["refused"] == 0
        assert stats_repo.refusal_rate(counts) == 0.0
    finally:
        conn.close()
