"""LLM 适配层测试：协议形状、错误语义、mock 行为。

不打真实网络：出网函数只有 http_post_json 一个，把它替换掉就能覆盖整条适配链。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from my_tool.core.config import Settings
from my_tool.core.llm import build_chat_model
from my_tool.core.llm.base import LLMResult
from my_tool.core.llm.http import http_post_json
from my_tool.core.llm.mock import MockChatModel
from my_tool.core.llm.openai_compat import OpenAICompatChatModel


class _FakeResponse:
    """最小化的 urlopen 返回值：只需要 read() 与上下文管理。"""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, body: bytes = b'{"error":"bad"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://example.invalid", code, "err", {}, io.BytesIO(body))


# --------------------------------------------------------------------------
# 出网层
# --------------------------------------------------------------------------
def test_post_json_sends_bearer_and_returns_parsed(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"data": []})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    out = http_post_json("https://api.example.com/v1/embeddings", {"input": "x"}, "sk-test", 5, 1)

    assert out == {"data": []}
    assert captured["url"].endswith("/v1/embeddings")
    assert captured["auth"] == "Bearer sk-test"
    assert captured["body"] == {"input": "x"}


def test_post_json_does_not_retry_on_4xx(monkeypatch):
    """4xx 是参数/鉴权错误，重试只会把错误放大成三倍账单。"""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise _http_error(400)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="400"):
        http_post_json("https://x/v1/chat", {}, "k", 5, 3)
    assert calls["n"] == 1


def test_post_json_retries_on_5xx(monkeypatch):
    """5xx 是对端抖动，指数退避重试是值得的。"""
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise _http_error(503)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("my_tool.core.llm.http.time.sleep", lambda _s: None)

    with pytest.raises(RuntimeError):
        http_post_json("https://x/v1/chat", {}, "k", 5, 3)
    assert calls["n"] == 3


# --------------------------------------------------------------------------
# 生成模型
# --------------------------------------------------------------------------
def test_openai_compat_maps_usage_and_model(monkeypatch):
    payload = {
        "model": "deepseek-chat",
        "choices": [{"message": {"content": "  答案 [1]  "}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }
    monkeypatch.setattr(
        "my_tool.core.llm.openai_compat.http_post_json",
        lambda url, body, key, timeout, retries: payload,
    )
    model = OpenAICompatChatModel("https://api.deepseek.com/v1", "sk", "deepseek-chat")
    res = model.chat([{"role": "user", "content": "hi"}])

    assert isinstance(res, LLMResult)
    assert res.text == "答案 [1]"          # 两侧空白应被裁掉
    assert res.model == "deepseek-chat"
    assert (res.tokens_in, res.tokens_out) == (120, 30)
    assert res.provider == "openai_compat"


def test_openai_compat_rejects_malformed_payload(monkeypatch):
    monkeypatch.setattr(
        "my_tool.core.llm.openai_compat.http_post_json",
        lambda *a, **k: {"unexpected": True},
    )
    model = OpenAICompatChatModel("https://x/v1", "k", "m")
    with pytest.raises(RuntimeError, match="结构异常"):
        model.chat([{"role": "user", "content": "hi"}])


def test_mock_model_extracts_sentences_with_citation_markers():
    """
    mock 只做抽取：挑出与问题词重叠最多的原句并加角标。
    它的作用是"检索对不对"的探针 —— 答不出来就是召回没命中，与生成质量无关。
    """
    prompt = (
        "【资料】\n"
        "[1] 《制度》第十八条（DOC#c001）\n"
        "第十八条 缴费基数\n2026 年度社会保险缴费基数上限为 36,921 元/月。\n\n"
        "[2] 《制度》第五条（DOC#c002）\n"
        "第五条 变更类型清单\n系统受理的变更类型包括调薪、降薪。\n\n"
        "【问题】\n社保缴费基数上限是多少？\n"
    )
    res = MockChatModel().chat([{"role": "user", "content": prompt}])

    assert "36,921" in res.text
    assert "[1]" in res.text
    assert res.provider == "mock"


def test_mock_model_is_honest_when_nothing_matches():
    """无相关句时必须输出拒答话术，而不是硬凑一句 —— 否则护栏测不出来。"""
    prompt = "【资料】\n[1] 《度》第一条\n完全不相干的内容。\n\n【问题】\n社保基数是多少？\n"
    res = MockChatModel().chat([{"role": "user", "content": prompt}])
    assert "无法确认" in res.text


def test_factory_returns_mock_by_default(settings: Settings):
    assert isinstance(build_chat_model(settings), MockChatModel)


def test_factory_builds_openai_compat_for_real_provider(settings: Settings):
    s = Settings(
        llm_provider="deepseek", llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="sk-test", llm_model="deepseek-chat",
    )
    model = build_chat_model(s)
    assert isinstance(model, OpenAICompatChatModel)
    assert model.provider == "deepseek"
