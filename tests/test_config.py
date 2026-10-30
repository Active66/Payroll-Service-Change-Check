"""`.env` 加载逻辑（core/config._load_dotenv）。

    这是"接真模型"时踩出来的：`.env.example` 一直写着"复制为 .env"，
    但 config 从来不读 .env 文件 —— 用户照做之后配置根本不生效。
    补上加载逻辑后，用这组测试把它钉住。
"""

from __future__ import annotations

import os

from my_tool.core.config import _load_dotenv


def test_loads_key_value_and_strips_quotes(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('LLM_PROVIDER=deepseek\nLLM_API_KEY="sk-abc"\n# 注释行\n\n', encoding="utf-8")

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    _load_dotenv(env)

    assert os.environ["LLM_PROVIDER"] == "deepseek"
    assert os.environ["LLM_API_KEY"] == "sk-abc"      # 引号被剥掉


def test_does_not_override_existing_env(tmp_path, monkeypatch):
    """环境变量优先：shell 里 export 的值不该被 .env 悄悄覆盖。"""
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=deepseek\n", encoding="utf-8")

    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    _load_dotenv(env)

    assert os.environ["LLM_PROVIDER"] == "qwen"


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    _load_dotenv(tmp_path / "not-exist.env")           # 不抛异常
    assert "LLM_PROVIDER" not in os.environ


def test_ignores_comment_and_blank(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# 全注释\n\n   \nLLM_MODEL=deepseek-chat\n", encoding="utf-8")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    _load_dotenv(env)
    assert os.environ["LLM_MODEL"] == "deepseek-chat"
    assert "全注释" not in os.environ
