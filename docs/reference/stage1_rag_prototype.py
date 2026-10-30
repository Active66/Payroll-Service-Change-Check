#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
薪酬变更核对系统 · 阶段 1 最小原型（单文件版）
================================================================================

本文件是《薪酬变更核对系统_技术架构设计_v0.1》第 04 节「第一周最小验证目标」中
D2 + D3 的最小可运行剪影：**一份真实制度 → 切片入向量库 → 混合检索 → 带引用的回答**。

刻意不做的事（与架构文档 4.4 一致）：
  · 不做 Web / FastAPI / 前端（阶段 2 再做）
  · 不做图谱抽取与多跳（阶段 4）
  · 不做多轮滚动摘要、权限、审计链（阶段 5+）
  · 不做真实 rerank 模型（先用 RRF 融合顶替，接口已留出）

--------------------------------------------------------------------------------
单文件但保留模块边界
--------------------------------------------------------------------------------
架构文档要求 api / services / agents / llm / ingestion 分层，本原型把每个将来要
拆出去的模块用 `# ===== 【模块 NN ...】 =====` 大段分隔，并标注目标文件路径。
阶段 2 拆包时按段剪切即可，不需要重写逻辑。

  【模块 01】配置          -> app/core/config.py                    ★复用
  【模块 02】日志与工具    -> app/core/logging.py                   复用
  【模块 03】领域模型      -> app/domain/entities/*.py              ★复用
  【模块 04】Markdown 解析 -> app/ingestion/parser/markdown.py      ★复用
  【模块 05】切片器        -> app/ingestion/chunker.py              ★复用（最关键）
  【模块 06】Embedding     -> app/llm/base.py + providers/*          ★复用
  【模块 07】向量库        -> app/agents/retrieval/vector_store.py  ★复用（FAISS→pgvector）
  【模块 08】BM25          -> app/agents/retrieval/bm25.py          ★复用（内存→tsvector）
  【模块 09】混合检索      -> app/agents/retrieval/hybrid.py        ★复用（RRF 不改）
  【模块 10】Prompt        -> app/agents/prompts/rag_answer.py       ★复用
  【模块 11】LLM 适配器    -> app/llm/providers/openai_compat.py    ★复用（唯一出网口）
  【模块 12】答案护栏      -> app/agents/guard.py                   ★复用（幻觉率 0 的抓手）
  【模块 13】索引流水线    -> app/ingestion/pipeline.py             ★复用
  【模块 14】编排器        -> app/agents/orchestrator.py            ★复用
  【模块 15】CLI / 评测    -> scripts/ + tests/evals/runner.py       仅原型

--------------------------------------------------------------------------------
运行方式（Windows / PowerShell，已在本机 Python 3.13 验证）
--------------------------------------------------------------------------------
    cd E:\\code\\PayrollServiceChangeCheck\\prototype

    # 1) 建索引：解析 → 500 字切片 → 向量化 → 写入 FAISS
    python stage1_rag_prototype.py build

    # 2) 单次提问（默认 mock 模型，不联网，用来验证检索与引用链路）
    python stage1_rag_prototype.py ask "调薪超过薪酬带宽怎么处理"

    # 3) 交互式问答（/sources 看原文、/debug 切调试、/quit 退出）
    python stage1_rag_prototype.py chat

    # 4) 内置 8 题检索评测（tests/evals 的雏形）
    python stage1_rag_prototype.py eval

    # 5) 查看当前配置与索引 manifest
    python stage1_rag_prototype.py info

    # 6) 接入真实 LLM（任意 OpenAI 兼容端点：DeepSeek / 通义 / Kimi / Ollama / vLLM）
    $env:LLM_PROVIDER="deepseek"; $env:LLM_API_KEY="sk-xxx"
    python stage1_rag_prototype.py ask "社保基数上限是多少"

依赖：faiss-cpu、numpy 必需；jieba 可选（无则退化为字符 n-gram 分词）。
      不依赖 openai SDK —— 出网只用标准库 urllib，避免多一层版本耦合。
"""

from __future__ import annotations

import argparse
import abc
import dataclasses
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
import unicodedata
import zlib
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Sequence

# --------------------------------------------------------------------------- #
# 可选依赖：缺失时降级而不是崩溃。原型阶段要能在裸环境跑起来，
# 但真实环境（阶段 2+）应当把 faiss/numpy 提升为硬依赖并把版本钉死。
# --------------------------------------------------------------------------- #
try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:
    import faiss
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore[assignment]

try:
    import jieba

    jieba.setLogLevel(60)  # 关掉 jieba 启动时的构建日志
    _HAS_JIEBA = True
except ImportError:  # pragma: no cover
    jieba = None  # type: ignore[assignment]
    _HAS_JIEBA = False


# =============================================================================
# 【模块 01 · 配置】目标文件：app/core/config.py
# ★核心模块（阶段 2 复用）：改成 pydantic-settings 的 BaseSettings 即可，
#   字段名与分层（llm / retrieval / ingestion）保持不变，调用方无需改动。
# =============================================================================
#
# 设计要点：所有可变量走环境变量，不在代码里写死。
# 三个"供应商可配置"的开关分别是：
#   LLM_PROVIDER          —— 生成模型（mock / openai / deepseek / qwen / ollama ...）
#   EMBEDDING_PROVIDER    —— 向量模型（hash / openai / ...）
#   LLM_BASE_URL / EMBEDDING_BASE_URL —— 同一协议下换厂商只改这两行
# --------------------------------------------------------------------------- #

# 项目根目录：本文件在 prototype/ 下，上一级即工程根
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOC = PROJECT_ROOT / "data" / "policy" / "员工薪酬变更核对制度_v1.0.md"
DEFAULT_INDEX_DIR = PROJECT_ROOT / "data" / "index"

# 各厂商的默认端点与默认模型。值为 (base_url, model)。
# 除 mock 外全部走 OpenAI 兼容协议，所以"新增一个供应商"= 增加一行字典。
PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai":   ("https://api.openai.com/v1", "gpt-4o-mini"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "qwen":     ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "zhipu":    ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "ollama":   ("http://localhost:11434/v1", "qwen2.5:7b"),
    "vllm":     ("http://localhost:8000/v1", "Qwen2.5-7B-Instruct"),
}

EMBEDDING_PROVIDER_DEFAULTS: dict[str, tuple[str, str, int]] = {
    # (base_url, model, dim)
    "openai": ("https://api.openai.com/v1", "text-embedding-3-small", 1536),
    "qwen":   ("https://dashscope.aliyuncs.com/compatible-mode/v1", "text-embedding-v3", 1024),
    "siliconflow": ("https://api.siliconflow.cn/v1", "BAAI/bge-m3", 1024),
    "ollama": ("http://localhost:11434/v1", "bge-m3", 1024),
}


@dataclass
class Settings:
    """全局配置。所有字段都能被同名（大写）环境变量覆盖。"""

    # —— 数据与索引路径 ——
    doc_path: Path = DEFAULT_DOC
    index_dir: Path = DEFAULT_INDEX_DIR

    # —— 切片参数（架构文档要求 500 字，overlap 用于句间衔接，避免答案被切断）——
    chunk_size: int = 500
    chunk_overlap: int = 80
    min_chunk_size: int = 60      # 小于该长度的碎片并入上一块，避免产生无意义切片
    chunk_mode: str = "size"      # size=按 500 字装箱（默认）；clause=一条一款

    # —— 检索参数 ——
    top_k: int = 3                # 用户要求：top3
    recall_k: int = 12            # 每一路先召回 recall_k 条，再融合取 top_k
    rrf_k: int = 60               # RRF 平滑常数（原论文取 60，几乎不用调）
    w_vector: float = 1.0         # 向量通道权重
    w_bm25: float = 1.0           # 全文通道权重

    # —— 拒答闸门：两路最高分都低于各自阈值 → 判定"库里没有依据"，直接拒答 ——
    #   阈值与向量模型强相关（本机 hash 向量有效问题 ≈0.27~0.38，越界 ≈0.11；
    #   bge-m3 这类真实模型的相关分会更高），换模型必须重新标定。
    min_vector_score: float = 0.25
    min_bm25_score: float = 4.0

    # —— LLM ——
    llm_provider: str = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 900
    llm_timeout: float = 60.0
    llm_max_retries: int = 3

    # —— Embedding ——
    embedding_provider: str = "hash"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 512
    embedding_batch: int = 16

    # —— 检索过滤：模拟"制度生效区间"过滤，对应 policy_documents.effective_from/to ——
    as_of: str = ""               # 留空 = 用 2026-06-01，便于复现；真实系统取业务日期

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量构造配置。默认值保证"不配任何东西也能跑通 mock 链路"。"""

        def e(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        def ei(name: str, default: int) -> int:
            try:
                return int(e(name) or default)
            except ValueError:
                return default

        def ef(name: str, default: float) -> float:
            try:
                return float(e(name) or default)
            except ValueError:
                return default

        provider = e("LLM_PROVIDER", "mock").lower() or "mock"
        base_url, model = PROVIDER_DEFAULTS.get(provider, ("", ""))
        # 环境变量优先于厂商默认值：换自建网关时只改 LLM_BASE_URL
        base_url = e("LLM_BASE_URL", base_url)
        model = e("LLM_MODEL", model)
        # 没有 key 却选了真实供应商 → 自动退回 mock，避免原型一上来就报错
        api_key = e("LLM_API_KEY", e("OPENAI_API_KEY", ""))
        if provider != "mock" and not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
            print(f"[warn] LLM_PROVIDER={provider} 但未配置 LLM_API_KEY，本次降级为 mock 模型。")
            provider = "mock"

        emb_provider = e("EMBEDDING_PROVIDER", "hash").lower() or "hash"
        emb_base, emb_model, emb_dim = EMBEDDING_PROVIDER_DEFAULTS.get(emb_provider, ("", "", 512))
        emb_base = e("EMBEDDING_BASE_URL", emb_base)
        emb_model = e("EMBEDDING_MODEL", emb_model)
        emb_dim = ei("EMBEDDING_DIM", emb_dim if emb_provider != "hash" else 512)
        emb_key = e("EMBEDDING_API_KEY", api_key)
        if emb_provider != "hash" and not emb_key:
            print(f"[warn] EMBEDDING_PROVIDER={emb_provider} 但未配置密钥，向量通道降级为 hash。")
            emb_provider = "hash"

        return cls(
            doc_path=Path(e("POLICY_DOC_PATH", str(DEFAULT_DOC))),
            index_dir=Path(e("INDEX_DIR", str(DEFAULT_INDEX_DIR))),
            chunk_size=ei("CHUNK_SIZE", 500),
            chunk_overlap=ei("CHUNK_OVERLAP", 80),
            min_chunk_size=ei("MIN_CHUNK_SIZE", 60),
            chunk_mode=e("CHUNK_MODE", "size") or "size",
            top_k=ei("TOP_K", 3),
            recall_k=ei("RECALL_K", 12),
            rrf_k=ei("RRF_K", 60),
            w_vector=ef("W_VECTOR", 1.0),
            w_bm25=ef("W_BM25", 1.0),
            min_vector_score=ef("MIN_VECTOR_SCORE", 0.25),
            min_bm25_score=ef("MIN_BM25_SCORE", 4.0),
            llm_provider=provider,
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            llm_temperature=ef("LLM_TEMPERATURE", 0.2),
            llm_max_tokens=ei("LLM_MAX_TOKENS", 900),
            llm_timeout=ef("LLM_TIMEOUT", 60.0),
            llm_max_retries=ei("LLM_MAX_RETRIES", 3),
            embedding_provider=emb_provider,
            embedding_base_url=emb_base,
            embedding_api_key=emb_key,
            embedding_model=emb_model,
            embedding_dim=emb_dim,
            embedding_batch=ei("EMBEDDING_BATCH", 16),
            as_of=e("AS_OF_DATE", ""),
        )

    def as_of_date(self) -> date:
        """检索基准日期：真实系统取"业务日期"，原型里固定一个可复现的日期。"""
        if self.as_of:
            try:
                return date.fromisoformat(self.as_of)
            except ValueError:
                pass
        return date(2026, 6, 1)


# =============================================================================
# 【模块 02 · 日志与终端输出】目标文件：app/core/logging.py
# 非核心（阶段 2 换成 structlog + 审计日志分流），这里只保证输出可读、可重定向。
# =============================================================================

_SEP = "─" * 78


def _init_console() -> None:
    """Windows 控制台默认 cp936，打印中文与箭头发送乱码，这里强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def log(msg: str, level: str = "info") -> None:
    """统一日志出口。阶段 2 换成结构化日志（含 trace_id），调用点不用改。"""
    prefix = {"info": "·", "ok": "✓", "warn": "!", "err": "×", "step": "»"}.get(level, "·")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {prefix} {msg}")


def section(title: str) -> None:
    print(f"\n{_SEP}\n{title}\n{_SEP}")


def sha256_text(text: str) -> str:
    """文件/文本摘要，对应 policy_documents.checksum，用于防重复索引与漂移检测。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def disp_width(s: str) -> int:
    """终端显示宽度：中文/全角算 2 列，西文算 1 列。表格对齐要用它，否则中文全歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int) -> str:
    """按显示宽度右补空格（超宽则截断），让命令行表格能对齐。"""
    while disp_width(s) > width:
        s = s[:-1]
    return s + " " * max(0, width - disp_width(s))


# =============================================================================
# 【模块 03 · 领域模型】目标文件：app/domain/entities/{chunk,retrieval}.py
# ★核心模块（阶段 2 复用）：字段与数据库表 policy_document_chunks 一一对应，
#   将来只需把 dataclass 换成 SQLAlchemy 2.0 声明式模型，序列化逻辑不变。
# =============================================================================


@dataclass
class Chunk:
    """
    一个知识切片。字段直接对齐架构文档 3.7 的 policy_document_chunks 表：

        id / doc_id / chunk_no / heading_path / content / page_no / token_count
        embedding / tsv

    这里额外带上 doc 级元数据（title / region / effective_from/to / version），
    因为 RAG 检索必须能在过滤阶段拿到"生效区间"，否则会把已废止制度答给用户
    ——架构文档 3.5 明确点名这是这类系统最常见的线上事故。
    """

    chunk_id: str            # 形如 POL-CMP-2026-001#c003，全局可追溯
    doc_id: str
    chunk_no: int            # 文档内序号，从 1 开始
    content: str             # 切片正文（含 overlap 衔接部分）
    heading_path: str        # "第二章 变更类型与受理范围 > 第五条 变更类型清单"
    clause_no: str           # 从 heading 抽出的条款号，如 "第五条"；无则空串
    char_len: int
    token_count: int         # 近似 token 数，用于 prompt 预算
    page_no: int | None = None   # Markdown 无页码；PDF/DOCX 摄入时填真实页码

    # —— 引用定位（一条切片可能覆盖多个条款，必须能说清"覆盖了哪几条"）——
    clause_range: str = ""       # 展示用：单条为 "第五条"，多条为 "第五条–第七条"
    clauses: list[str] = field(default_factory=list)   # 覆盖的条款号列表

    # —— doc 级元数据（冗余，避免检索时回表）——
    doc_title: str = ""
    doc_type: str = "policy"
    region_code: str = ""
    version: int = 1
    effective_from: str = ""     # YYYY-MM-DD
    effective_to: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Chunk":
        return cls(**d)

    @property
    def citation_label(self) -> str:
        """给人和给模型看的引用标签：优先条款号，其次章节路径。"""
        loc = self.clause_range or self.clause_no
        if loc and self.doc_title:
            return f"《{self.doc_title}》{loc}"
        return self.heading_path or self.chunk_id


@dataclass
class RetrievalHit:
    """一条召回结果。保留双通道分数与名次，是排查"为什么这条没被召回"的关键。"""

    chunk: Chunk
    fused_score: float = 0.0        # RRF 融合分
    vector_score: float = 0.0       # 余弦相似度（向量通道）
    vector_rank: int | None = None  # 向量通道名次（1 起）
    bm25_score: float = 0.0         # BM25 原始分
    bm25_rank: int | None = None    # 全文通道名次（1 起）

    @property
    def channels(self) -> str:
        """标注该条被哪几路召回，形如 "向量2/BM25-"。1+1 命中通常最可信。"""
        v = str(self.vector_rank) if self.vector_rank else "-"
        b = str(self.bm25_rank) if self.bm25_rank else "-"
        return f"向量{v}/BM25{b}"


@dataclass
class Citation:
    """回答里的一条引用，对应 chat_messages.citations 的元素结构。"""

    index: int              # 回答正文中的角标 [1] [2]
    chunk_id: str           # 回溯 L0 原文的主键（架构文档 0.3 L1 铁律）
    doc_id: str
    clause_no: str          # 条款定位，单条为"第五条"，多条为"第五条–第七条"
    heading_path: str
    score: float
    snippet: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# =============================================================================
# 【模块 04 · Markdown 解析】目标文件：app/ingestion/parser/markdown.py
# ★核心模块（阶段 2 复用）：PDF/DOCX 解析器只要产出同样的 Block 列表，
#   下游切片器与索引流程完全不用改 —— 这就是"解析与切片解耦"的价值。
# =============================================================================


@dataclass
class Block:
    """
    解析后的结构化块。刻意只保留"标题层级 + 正文 + 表格"三种，
    因为薪酬制度真正要保住的结构就这三样：条款归属、条文正文、参数表格。
    """

    kind: str            # "heading" / "text" / "table"
    text: str
    heading_path: str    # 该块所属的完整标题路径
    level: int = 0       # heading 级别，其它为 0
    is_table: bool = False


def parse_markdown(text: str) -> tuple[dict[str, str], list[Block]]:
    """
    把 Markdown 拆成 (front_matter, blocks)。

    关键决策：
      1. 表格整块保留，绝不在行中间切断 —— 制度里的"基数上下限""风险分级"
         一旦被切断，模型就会拿着半张表编数字。
      2. 每个正文块都挂 heading_path，这是实现"按条款引用"的前提。
      3. YAML front matter 里读 doc_id / effective_from 等元数据，
         对应 policy_documents 表字段。
    """
    meta: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").split("\n")
    idx = 0

    # —— 1) front matter：极简 YAML 解析（只支持 key: value，够原型用）——
    if lines and lines[0].strip() == "---":
        idx = 1
        while idx < len(lines) and lines[idx].strip() != "---":
            line = lines[idx]
            if ":" in line and not line.startswith(" "):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
            idx += 1
        idx += 1  # 跳过结束的 ---

    blocks: list[Block] = []
    heading_stack: list[tuple[int, str]] = []   # [(level, title)]
    buf: list[str] = []

    def current_path() -> str:
        return " > ".join(t for _, t in heading_stack)

    def flush_text() -> None:
        """把累积的正文行合并成一个 text 块。"""
        nonlocal buf
        body = "\n".join(buf).strip()
        buf = []
        if body:
            blocks.append(Block(kind="text", text=body, heading_path=current_path()))

    while idx < len(lines):
        raw = lines[idx]
        stripped = raw.strip()

        # 空行 = 块分隔符
        if not stripped:
            flush_text()
            idx += 1
            continue

        # —— 标题 ——
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", stripped)
        if m:
            flush_text()
            level = len(m.group(1))
            title = m.group(2)
            # 弹掉层级更深或同级的旧标题，维护正确的祖先链
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            blocks.append(Block(kind="heading", text=title, heading_path=current_path(), level=level))
            idx += 1
            continue

        # —— 表格：连续的 | 开头行整块收集 ——
        if stripped.startswith("|"):
            flush_text()
            table_lines: list[str] = []
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                table_lines.append(lines[idx].strip())
                idx += 1
            blocks.append(
                Block(kind="table", text="\n".join(table_lines), heading_path=current_path(), is_table=True)
            )
            continue

        buf.append(raw)
        idx += 1

    flush_text()
    # 第一块若是文档大标题，去掉重复的 H1（避免 heading_path 里出现两次标题）
    return meta, blocks


# =============================================================================
# 【模块 05 · 切片器】目标文件：app/ingestion/chunker.py
# ★核心模块（阶段 2 原样复用）：架构文档 4.5 把"切片质量"列为第一周真正的风险，
#   所以这里不是简单 text[i:i+500]，而是五层策略：
#
#     ① 500 字装箱，但装箱单元是"条款"：一条放得下就继续放下一条，放不下就在
#        条款边界断开 —— 纯按字符硬切会把一条制度切成两半，引用就失去意义；
#     ② 章为硬边界：同一章内可合并多条款，跨章绝不合并（否则召回串味）；
#     ③ 表格不可切：整张表作为原子单元独占一个切片，制度里的基数表/分级表
#        一旦被切断，模型就会拿着半张表编数字；
#     ④ 标题内联：把"第X条 XXX"写进正文，脱离标题的条文无法自证身份，
#        也让"续段"能通过 `（续）` 标记保持可追溯；
#     ⑤ 重叠衔接：相邻切片重叠 chunk_overlap 字并对齐句首，避免答案正好
#        跨在切口上导致"召回命中却答不全"。
#
#   引用粒度：每个切片记录 clauses[] 与 clause_range，即使一个切片覆盖 4 条，
#   也能精确说出"第五章 第十七条–第二十条"。这是本实现与"朴素 500 字硬切"的关键差别。
#
#   阶段 2 迁移注意：本实现基于字符数。若改用 tiktoken 计数，
#   把 len() 换成 token 计数即可，切分骨架不动。
# =============================================================================

# 中文句子边界：句号/问号/感叹号/分号/换行，保留标点
_SENT_SPLIT = re.compile(r"[^。！？；!?;\n]*[。！？；!?;\n]|[^。！？；!?;\n]+$")
# 条款号抽取：第五条 / 第十二条
_CLAUSE_RE = re.compile(r"第[一二三四五六七八九十百零〇\d]+条")
# 为新条款预留的正文空间：剩余不足这么多字时，宁可提前封口，
# 也不要把"第X条"的标题留在上一块末尾（会导致孤儿标题，引用失真）
_BODY_RESERVE = 120


def _clause_no_of(heading_path: str) -> str:
    """从标题路径里取最后一个条款号，作为引用主键。"""
    found = _CLAUSE_RE.findall(heading_path)
    return found[-1] if found else ""


def split_sentences(text: str) -> list[str]:
    """按中文标点切句，保留标点。切不开时退回整段。"""
    parts = [s for s in _SENT_SPLIT.findall(text) if s.strip()]
    return parts or [text]


def _pack_sentences(sentences: list[str], limit: int) -> list[str]:
    """把句子贪心装箱成不超过 limit 的分组，不跨句切开。"""
    groups: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for s in sentences:
        s_len = len(s)
        if cur and cur_len + s_len > limit:
            groups.append("".join(cur))
            cur, cur_len = [], 0
        # 单句本身就超长（制度里偶有超长枚举）→ 硬切
        while s_len > limit:
            groups.append(s[:limit])
            s = s[limit:]
            s_len = len(s)
        cur.append(s)
        cur_len += s_len
    if cur:
        groups.append("".join(cur))
    return groups


def _overlap_tail(text: str, overlap: int) -> str:
    """
    取上一块的尾部作为下一块的开头。对齐到句首，避免从半个词开始。
    这是"检索能命中但答不全"的常见成因，必须在切片层解决。

    注意：要把尾部的标题行（"第X条 …"）丢掉。标题一旦被带进下一块，
    就会出现"第四条 生效与版本"在相邻两块各出现一次的重复，
    模型读到两遍同样的条号，很容易把归属搞错。
    """
    if overlap <= 0 or len(text) <= overlap:
        return ""
    tail = text[-overlap:]
    # 去掉尾部的标题行（"第X条 …" / "第X章 …"）
    tail = re.sub(r"(?m)^第[一二三四五六七八九十百零〇\d]+[条章][^\n]*$", "", tail)
    # 从尾部片段里找到第一个句末标点之后的位置，作为干净的起点
    for i in range(len(tail)):
        if tail[i] in "。！？；\n":
            return tail[i + 1 :].strip()
    return tail.strip()


def _section_key(heading_path: str) -> str:
    """
    取标题路径的前两段（文档标题 + 章）作为"绝不跨越的语义边界"。
    切片可以横跨同一章内的多个条款，但绝不能横跨两章 ——
    否则一个切片里同时出现"社保基数"和"银行账户"，检索会把风马牛不相及的内容一起召回。
    """
    parts = [p for p in heading_path.split(" > ") if p.strip()]
    return " > ".join(parts[:2])


def chunk_blocks(
    blocks: Sequence[Block],
    *,
    chunk_size: int = 500,
    overlap: int = 80,
    min_size: int = 60,
    meta: dict[str, str] | None = None,
    mode: str = "size",
) -> list[Chunk]:
    """
    块列表 → 切片列表。这是全流程质量的分水岭，策略见本模块头部注释。

    参数 mode：
      · "size"   （默认）按 500 字装箱。装箱的**单元是条款**：一条放得下就继续放下一条，
                 放不下就在条款边界处断开。这样既满足"500 字切片"，又因为标题内联在正文里、
                 并记录了 clause_range，引用精度仍然是条款级。
      · "clause" 一条一款，绝不合并。适合需要"精确到条"的核对场景（阶段 2 可切换）。
    """
    meta = meta or {}
    doc_id = meta.get("doc_id") or "DOC-UNKNOWN"
    doc_title = meta.get("title", "")
    # 版本号在制度文件里常写成 1.0 / 1 / v2，统一折成整数存库（1.0 → 1）
    try:
        version = int(float(str(meta.get("version", "1") or "1")))
    except ValueError:
        version = 1
    region = meta.get("region_code", "")
    eff_from = meta.get("effective_from", "")
    eff_to = meta.get("effective_to") or None
    doc_type = meta.get("doc_type", "policy")

    chunks: list[Chunk] = []
    chunk_no = 0

    # 当前正在累积的切片状态
    cur_parts: list[str] = []
    cur_len = 0
    cur_section = ""            # 当前所属"章"
    cur_path = ""               # 当前所属"章"的完整路径（用作 heading_path）
    cur_clauses: list[str] = [] # 本切片覆盖的条款号
    carry = ""                  # 从上一块继承的 overlap 文本

    def emit(keep_carry: bool = False) -> None:
        """
        把当前累积内容落成一个切片，并清空状态。

        keep_carry 只在"同一条款被迫续切"时置 True：
        切在条款边界上时不需要重叠（语义单元已经完整），加了只会让相邻两块重复，
        既浪费 token 又让模型看到两遍相同内容。
        """
        nonlocal chunk_no, cur_parts, cur_len, carry, cur_clauses
        body = "\n".join(p for p in cur_parts if p).strip()
        if not body:
            cur_parts, cur_len, cur_clauses = [], 0, []
            return
        chunk_no += 1
        clauses = list(dict.fromkeys(cur_clauses))   # 去重且保序
        if not clauses:
            clause_range = ""
        elif len(clauses) == 1:
            clause_range = clauses[0]
        else:
            clause_range = f"{clauses[0]}–{clauses[-1]}"
        chunks.append(
            Chunk(
                chunk_id=f"{doc_id}#c{chunk_no:03d}",
                doc_id=doc_id,
                chunk_no=chunk_no,
                content=body,
                heading_path=cur_path,
                clause_no=clauses[0] if clauses else "",
                clause_range=clause_range,
                clauses=clauses,
                char_len=len(body),
                token_count=int(len(body) * 0.75),   # 中文粗估：1 字 ≈ 0.75 token
                page_no=None,
                doc_title=doc_title,
                doc_type=doc_type,
                region_code=region,
                version=version,
                effective_from=eff_from,
                effective_to=eff_to,
            )
        )
        carry = _overlap_tail(body, overlap) if keep_carry else ""
        # 表格行不适合当 overlap 前缀（会把半张表带到下一块），直接丢弃
        if carry.lstrip().startswith("|"):
            carry = ""
        cur_parts, cur_len, cur_clauses = [], 0, []

    for blk in blocks:
        # ---------------- 标题块：决定"封口时机" ----------------
        if blk.kind == "heading":
            section = _section_key(blk.heading_path)
            clause = _clause_no_of(blk.text)

            # ① 跨章 → 强制封口
            if cur_parts and section and cur_section and section != cur_section:
                emit()
            # ② 条款级模式 → 每遇新条款就封口
            if mode == "clause" and clause and cur_clauses:
                emit()
            # ③ 剩余空间放不下"这一条 + 一段正文"→ 在条款边界处提前封口。
            #    这里必须给正文预留空间（_BODY_RESERVE），否则会出现
            #    "条标题落在上一块末尾、条文正文落在下一块"的孤儿标题，
            #    引用时说的第X条并不在它所在的那一块里，是致命的可举证性缺陷。
            if clause and cur_parts and cur_len + len(blk.text) + _BODY_RESERVE > chunk_size:
                emit()

            cur_section = section or cur_section
            cur_path = section or blk.heading_path

            # 章标题与条标题要写进正文：脱离标题的条文会失去"这是第几条"的自证能力
            if blk.level >= 2:
                cur_parts.append(blk.text)
                cur_len += len(blk.text) + 1
            if clause:
                cur_clauses.append(clause)
            continue

        # ---------------- 表格块：原子单元，永不在行间切断 ----------------
        if blk.is_table:
            # 放得下 → 并入当前条款块，引用仍然落在该条款上（如"第二十六条 + 分级表"）
            if cur_parts and cur_len + len(blk.text) + 1 <= chunk_size:
                cur_parts.append(blk.text)
                cur_len += len(blk.text) + 1
                continue
            # 放不下 → 整表独占一个切片，但继承前一条款归属，避免出现"无主表格"
            prev_clauses = list(cur_clauses)
            prev_path = cur_path
            if cur_parts:
                emit()
            cur_path = prev_path
            cur_clauses = prev_clauses
            cur_parts.append(blk.text)
            cur_len = len(blk.text)
            emit()
            continue

        # ---------------- 正文块 ----------------
        if not cur_path:
            cur_path = _section_key(blk.heading_path)

        # 超长段落按句切；正常段落整块参与装箱
        pieces = (
            [blk.text]
            if len(blk.text) <= chunk_size
            else _pack_sentences(split_sentences(blk.text), chunk_size)
        )

        for piece in pieces:
            piece_len = len(piece)
            if cur_parts and cur_len + piece_len + 1 > chunk_size:
                # 记录断点所属条款，让"被切开的后半段"仍然能自称是第几条
                prev_clause = cur_clauses[-1] if cur_clauses else ""
                # 同一条款内部被迫续切 → 用 overlap 把上一句带过来，避免答案被切口截断
                emit(keep_carry=True)
                if carry:
                    cur_parts.append(carry)
                    cur_len = len(carry)
                if prev_clause:
                    cont = f"{prev_clause}（续）"
                    cur_parts.append(cont)
                    cur_len += len(cont) + 1
                    cur_clauses.append(prev_clause)
            cur_parts.append(piece)
            cur_len += piece_len + 1

    emit()

    # —— 收尾：合并同章内的过短碎片，避免出现"只有一句话"的低信息切片 ——
    merged: list[Chunk] = []
    for c in chunks:
        if merged and c.char_len < min_size and merged[-1].heading_path == c.heading_path:
            prev = merged[-1]
            prev.content = (prev.content + "\n" + c.content).strip()
            prev.char_len = len(prev.content)
            prev.token_count = int(prev.char_len * 0.75)
            prev.clauses = list(dict.fromkeys(prev.clauses + c.clauses))
            if prev.clauses:
                prev.clause_no = prev.clauses[0]
                prev.clause_range = (
                    prev.clauses[0] if len(prev.clauses) == 1
                    else f"{prev.clauses[0]}–{prev.clauses[-1]}"
                )
            continue
        merged.append(c)
    # 重新编号，保证 chunk_id 连续可读
    for i, c in enumerate(merged, start=1):
        c.chunk_no = i
        c.chunk_id = f"{doc_id}#c{i:03d}"
    return merged


# =============================================================================
# 【模块 06 · Embedding 抽象与实现】目标文件：app/llm/base.py + app/llm/providers/*
# ★核心模块（阶段 2 复用）：抽象层决定"换模型不动业务"。
#
#   两种实现：
#     HashEmbedding          —— 离线确定性哈希向量，无网可跑，用于验证链路骨架；
#                                ★不要用于生产★，它只有词面相似度，没有语义。
#     OpenAICompatEmbedding  —— 真实向量模型（bge-m3 / text-embedding-3 / Qwen-embedding），
#                               同一协议，换厂商只改 base_url + model + dim。
# =============================================================================

# 停用词：只保留必要的一小张表，BM25 与哈希向量都要用
STOPWORDS = {
    "的", "了", "和", "是", "在", "有", "与", "及", "或", "对", "为", "被", "把", "就",
    "也", "都", "而", "并", "等", "中", "上", "下", "之", "其", "该", "这", "那", "个",
    "我", "你", "他", "她", "它", "吗", "呢", "啊", "吧", "怎么", "如何", "什么", "多少",
    "请问", "一下", "可以", "需要", "是否", "哪些", "怎样", "为什么", "么",
}

# 领域自定义词：不加入词典的话，jieba 会把"薪酬带宽"切成"薪酬/带宽"，
# 而制度原文里"薪酬带宽"是专有名词 → 词面匹配会丢分。
DOMAIN_WORDS = [
    "薪酬带宽", "社保基数", "公积金基数", "缴费基数", "冻结期", "封版", "补发",
    "验证打款", "双人复核", "变更单", "明细项", "生效日", "提交日", "阻断",
    "差异分级", "人工核对队列", "薪酬共享服务中心", "专项附加扣除", "离职结算",
]
if _HAS_JIEBA:
    for _w in DOMAIN_WORDS:
        jieba.add_word(_w, freq=20000)


def normalize_text(text: str) -> str:
    """统一大小写、全角、数字千分位，保证文档与问题在同一口径上比对。"""
    t = text.lower()
    # 全角数字/字母 → 半角
    t = "".join(chr(ord(c) - 0xFEE0) if "０" <= c <= "９" or "Ａ" <= c <= "ｚ" else c for c in t)
    # 36,921 → 36921：分词前抹掉千分位，否则 BM25 永远匹配不上
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)
    return t


def tokenize(text: str) -> list[str]:
    """
    统一分词入口（BM25 与哈希向量共用，保证两个通道的"词"定义一致）。

    有 jieba 用 cut_for_search（会额外产出子词，提升中文召回）；
    没有 jieba 退化为「字符 unigram + bigram」，对中文短查询仍然可用。
    """
    t = normalize_text(text)
    if _HAS_JIEBA:
        raw = list(jieba.cut_for_search(t))
    else:  # pragma: no cover
        chars = [c for c in t if not c.isspace() and c not in "，。！？；：、（）()【】[]"]
        raw = chars + [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    out: list[str] = []
    for w in raw:
        w = w.strip()
        if not w or w in STOPWORDS:
            continue
        if re.fullmatch(r"[\W_]+", w):     # 纯标点丢弃
            continue
        if len(w) == 1 and not w.isdigit() and not ("\u4e00" <= w <= "\u9fff"):
            continue
        out.append(w)
    return out


class EmbeddingModel(abc.ABC):
    """
    向量模型协议。阶段 2 拆包后这就是 app/llm/base.py 里的 Protocol。
    签名固定为 list[str] -> list[list[float]]，批量优先（真实模型一次请求多条省时省钱）。
    """

    name: str = "base"
    dim: int = 0

    @abc.abstractmethod
    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


class HashEmbedding(EmbeddingModel):
    """
    离线哈希向量（hashing trick + 对数词频 + L2 归一化）。

    用途只有一个：**没有 API key 时也能把"切片→向量→FAISS→检索→引用"整条链路跑通**，
    让阶段 1 的验收不被采购流程卡住。

    为什么用 crc32 而不是内置 hash()：Python 对 str 的 hash 每个进程加随机盐，
    重启后向量全变，索引直接失效。这是自己实现哈希向量时最容易踩的坑。
    """

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.name = f"hash-tf-{dim}"

    def _vec(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text)
        if not tokens:
            return vec
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        for tok, cnt in tf.items():
            # 对数词频压缩长文本优势，避免一条长条款吞掉整个语义
            w = 1.0 + math.log(cnt)
            h = zlib.crc32(tok.encode("utf-8"))          # 确定性哈希
            vec[h % self.dim] += w
            # 位置无关的二次散列，降低碰撞造成的方向偏差
            vec[(h >> 11) % self.dim] += w * 0.5
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


class OpenAICompatEmbedding(EmbeddingModel):
    """
    真实向量模型，走 OpenAI /embeddings 兼容协议。
    bge-m3、text-embedding-3-*、text-embedding-v3 都能直接接。
    """

    def __init__(self, base_url: str, api_key: str, model: str, dim: int,
                 batch: int = 16, timeout: float = 60.0, retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = model
        self.dim = dim
        self.batch = batch
        self.timeout = timeout
        self.retries = retries

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            part = list(texts[i : i + self.batch])
            payload = {"model": self.name, "input": part}
            data = _http_post_json(
                f"{self.base_url}/embeddings", payload, self.api_key, self.timeout, self.retries
            )
            # 按 index 排序，防止服务端乱序返回
            items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
            out.extend([it["embedding"] for it in items])
            log(f"embedding 进度 {min(i + self.batch, len(texts))}/{len(texts)}", "info")
        if out and len(out[0]) != self.dim:
            log(f"向量维度与配置不符：配置 {self.dim}，实际 {len(out[0])}，已按实际值修正。", "warn")
            self.dim = len(out[0])
        return out


def build_embedding_model(s: Settings) -> EmbeddingModel:
    """工厂：配置 → 实例。上层永远只依赖 EmbeddingModel 接口。"""
    if s.embedding_provider == "hash":
        return HashEmbedding(dim=s.embedding_dim)
    return OpenAICompatEmbedding(
        base_url=s.embedding_base_url,
        api_key=s.embedding_api_key,
        model=s.embedding_model,
        dim=s.embedding_dim,
        batch=s.embedding_batch,
        timeout=s.llm_timeout,
        retries=s.llm_max_retries,
    )


# =============================================================================
# 【模块 07 · 向量库（FAISS）】目标文件：app/agents/retrieval/vector_store.py
# ★核心模块（阶段 2 复用）：接口 search(vec, k) -> [(chunk_idx, score)] 保持不变，
#   实现从 FAISS 换成 pgvector 时，只替换本类内部，hybrid 层无感。
#
#   FAISS 选型说明：
#     · IndexFlatIP + 向量 L2 归一化 == 余弦相似度（精确检索，小库首选）；
#     · 切片量超过 1 万条再换 IndexHNSWFlat(M=16)，和架构文档 pgvector 的
#       HNSW(m=16, ef_construction=64) 参数对齐，召回表现一致。
# =============================================================================


class VectorStore:
    """
    向量库封装。同时支持 FAISS 与 numpy 两种后端，对外接口完全一致。
    持久化三件套：index 文件 + chunks.jsonl + manifest.json。
    """

    INDEX_FILE = "index.faiss"
    VECTORS_FILE = "vectors.npy"
    CHUNKS_FILE = "chunks.jsonl"
    MANIFEST_FILE = "manifest.json"

    def __init__(self, chunks: list[Chunk], dim: int):
        self.chunks = chunks
        self.dim = dim
        self.index = None            # FAISS index
        self._matrix = None          # numpy 回退
        self.backend = "none"

    # ---------------- 构建 ----------------
    def build(self, vectors: list[list[float]]) -> None:
        if np is None:
            raise RuntimeError("缺少 numpy：请先 pip install numpy")
        mat = np.asarray(vectors, dtype="float32")
        # L2 归一化 → 内积即余弦相似度
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms
        self.dim = int(mat.shape[1])

        if faiss is not None:
            n = mat.shape[0]
            if n > 10000:
                # 大库用 HNSW，参数与 pgvector 侧保持一致，便于横向对比
                self.index = faiss.IndexHNSWFlat(self.dim, 16)
                self.index.hnsw.efConstruction = 64
                self.index.hnsw.efSearch = 64
            else:
                self.index = faiss.IndexFlatIP(self.dim)
            self.index.add(mat)
            self.backend = "faiss"
        else:
            self._matrix = mat
            self.backend = "numpy"
            log("未安装 faiss，向量检索降级为 numpy 暴力内积（结果等价，仅性能差）。", "warn")

    # ---------------- 检索 ----------------
    def search(self, query_vec: list[float], top_k: int) -> list[tuple[int, float]]:
        """返回 [(chunk 下标, 余弦相似度)]，按分数降序。"""
        if not self.chunks:
            return []
        q = np.asarray([query_vec], dtype="float32")
        n = np.linalg.norm(q)
        if n > 0:
            q = q / n
        k = min(top_k, len(self.chunks))

        if self.backend == "faiss" and self.index is not None:
            scores, idxs = self.index.search(q, k)
            return [(int(i), float(s)) for i, s in zip(idxs[0], scores[0]) if i >= 0]

        sims = (self._matrix @ q[0])  # type: ignore[operator]
        order = np.argsort(-sims)[:k]
        return [(int(i), float(sims[i])) for i in order]

    # ---------------- 持久化 ----------------
    def save(self, index_dir: Path, manifest: dict[str, Any]) -> None:
        index_dir.mkdir(parents=True, exist_ok=True)
        with open(index_dir / self.CHUNKS_FILE, "w", encoding="utf-8") as f:
            for c in self.chunks:
                f.write(json.dumps(c.to_json(), ensure_ascii=False) + "\n")
        if self.backend == "faiss" and self.index is not None:
            faiss.write_index(self.index, str(index_dir / self.INDEX_FILE))
        elif self._matrix is not None:
            np.save(index_dir / self.VECTORS_FILE, self._matrix)
        manifest = {**manifest, "backend": self.backend, "dim": self.dim,
                    "chunk_count": len(self.chunks)}
        (index_dir / self.MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, index_dir: Path) -> tuple["VectorStore", dict[str, Any]]:
        mpath = index_dir / cls.MANIFEST_FILE
        if not mpath.exists():
            raise FileNotFoundError(
                f"索引不存在：{index_dir}\n请先执行：python stage1_rag_prototype.py build"
            )
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        chunks = [
            Chunk.from_json(json.loads(line))
            for line in (index_dir / cls.CHUNKS_FILE).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        store = cls(chunks, int(manifest.get("dim", 0)))

        backend = manifest.get("backend", "faiss")
        if backend == "faiss":
            store.index = faiss.read_index(str(index_dir / cls.INDEX_FILE))
            store.backend = "faiss"
        else:
            store._matrix = np.load(index_dir / cls.VECTORS_FILE)
            store.backend = "numpy"
        return store, manifest


# =============================================================================
# 【模块 08 · BM25 全文检索】目标文件：app/agents/retrieval/bm25.py
# ★核心模块（阶段 2 复用）：算法部分一行不用改；上生产时把倒排表换成
#   PostgreSQL 的 tsvector + zhparser，或者直接调 rank_bm25 库。
#
#   为什么必须有这一路：制度问题里大量是**精确词**——"36,921""冻结期""L4"
#   这类字符串，向量相似度天然不敏感。架构文档 0.2 把这一点列为选型理由之一。
# =============================================================================


class BM25Index:
    """
    Okapi BM25（k1=1.5, b=0.75，中文场景的经验值）。
    公式：score = Σ idf(qi) * tf*(k1+1) / (tf + k1*(1-b+b*dl/avgdl))
    """

    def __init__(self, docs_tokens: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.docs = docs_tokens
        self.n_docs = len(docs_tokens)
        self.doc_len = [len(t) for t in docs_tokens]
        self.avgdl = (sum(self.doc_len) / self.n_docs) if self.n_docs else 0.0

        # 词频表与文档频率
        self.tfs: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for toks in docs_tokens:
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self.tfs.append(tf)
            for t in tf:
                df[t] = df.get(t, 0) + 1
        # idf 用带平滑的变体，避免高频词出现负分
        self.idf = {
            t: math.log(1 + (self.n_docs - c + 0.5) / (c + 0.5)) for t, c in df.items()
        }

    def search(self, query_tokens: list[str], top_k: int) -> list[tuple[int, float]]:
        scores: list[tuple[int, float]] = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            dl = self.doc_len[i] or 1
            for q in query_tokens:
                f = tf.get(q)
                if not f:
                    continue
                idf = self.idf.get(q, 0.0)
                denom = f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += idf * f * (self.k1 + 1) / denom
            if s > 0:
                scores.append((i, s))
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]


# =============================================================================
# 【模块 09 · 混合检索（RRF 融合）】目标文件：app/agents/retrieval/hybrid.py
# ★核心模块（阶段 2 复用，且是最该原样复用的一个）：
#
#   融合用 RRF（Reciprocal Rank Fusion）而不是"加权分数相加"，
#   原因：向量余弦（0~1）与 BM25（0~30）量纲完全不同，直接相加等于让 BM25 独裁。
#   RRF 只看名次：score = Σ w_i / (k + rank_i)，天然免疫量纲问题，也不用调归一化。
#
#   阶段 2 的扩展点（本类已留出接口）：
#     · 加第三路（图谱扩展）→ 往 channels 里多塞一路即可；
#     · 加真实 reranker    → 在 fuse 之后、截断之前插一层 cross-encoder。
# =============================================================================


def is_effective(chunk: Chunk, as_of: date) -> bool:
    """
    生效区间过滤。架构文档 3.5 的两个"容易设计错的点"之一：
    检索必须带 effective_from <= 业务日期 <= coalesce(effective_to, ∞)，
    否则会把已废止制度答给用户。
    """
    try:
        if chunk.effective_from:
            if date.fromisoformat(chunk.effective_from) > as_of:
                return False
        if chunk.effective_to:
            if date.fromisoformat(chunk.effective_to) < as_of:
                return False
    except ValueError:
        return True   # 元数据脏数据不阻断检索，但要记日志（阶段 2 补）
    return True


def rrf_fuse(
    vector_hits: list[tuple[int, float]],
    bm25_hits: list[tuple[int, float]],
    chunks: Sequence[Chunk],
    *,
    k: int = 60,
    w_vector: float = 1.0,
    w_bm25: float = 1.0,
    top_k: int = 3,
    as_of: date | None = None,
) -> list[RetrievalHit]:
    """
    两路召回 → RRF 融合 → top_k。
    同一个 chunk 被两路同时召回时，分数叠加，这正是"精确词 + 语义"互补的收益。
    """
    pool: dict[int, RetrievalHit] = {}

    for rank, (idx, score) in enumerate(vector_hits, start=1):
        ch = chunks[idx]
        if as_of and not is_effective(ch, as_of):
            continue
        h = pool.setdefault(idx, RetrievalHit(chunk=ch))
        h.vector_score = score
        h.vector_rank = rank
        h.fused_score += w_vector / (k + rank)

    for rank, (idx, score) in enumerate(bm25_hits, start=1):
        ch = chunks[idx]
        if as_of and not is_effective(ch, as_of):
            continue
        h = pool.setdefault(idx, RetrievalHit(chunk=ch))
        h.bm25_score = score
        h.bm25_rank = rank
        h.fused_score += w_bm25 / (k + rank)

    ranked = sorted(pool.values(), key=lambda h: -h.fused_score)
    return ranked[:top_k]


class HybridRetriever:
    """
    编排"两路召回 + 融合"。这是 orchestrator 唯一需要认识的检索入口。
    """

    def __init__(self, store: VectorStore, bm25: BM25Index, embedder: EmbeddingModel,
                 settings: Settings):
        self.store = store
        self.bm25 = bm25
        self.embedder = embedder
        self.s = settings

    def retrieve(self, question: str, top_k: int | None = None,
                 as_of: date | None = None) -> tuple[list[RetrievalHit], dict[str, Any]]:
        top_k = top_k or self.s.top_k
        as_of = as_of or self.s.as_of_date()
        t0 = time.perf_counter()

        # ① 向量通道
        t1 = time.perf_counter()
        qvec = self.embedder.embed_one(question)
        vector_hits = self.store.search(qvec, self.s.recall_k)
        t_vec = (time.perf_counter() - t1) * 1000

        # ② 全文通道（与向量通道用的 tokenize 完全一致，减少口径差异）
        t2 = time.perf_counter()
        q_tokens = tokenize(question)
        bm25_hits = self.bm25.search(q_tokens, self.s.recall_k)
        t_bm = (time.perf_counter() - t2) * 1000

        # ③ 融合
        hits = rrf_fuse(
            vector_hits, bm25_hits, self.store.chunks,
            k=self.s.rrf_k, w_vector=self.s.w_vector, w_bm25=self.s.w_bm25,
            top_k=top_k, as_of=as_of,
        )
        debug = {
            "query_tokens": q_tokens,
            "vector_candidates": len(vector_hits),
            "bm25_candidates": len(bm25_hits),
            "as_of": as_of.isoformat(),
            "t_vector_ms": round(t_vec, 2),
            "t_bm25_ms": round(t_bm, 2),
            "t_total_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

        # ④ 拒答闸门：注意看的是"两路各自的最好成绩"，而不是融合后的 top1。
        #    RRF 只看名次，一条垃圾切片只要在两路都排第一，融合分照样最高；
        #    用原始分数设闸才能把"库里确实没有"和"有但排得靠后"区分开。
        if hits:
            best_vec = max(h.vector_score for h in hits)
            best_bm = max(h.bm25_score for h in hits)
            debug["best_vector_score"] = round(best_vec, 4)
            debug["best_bm25_score"] = round(best_bm, 3)
            if best_vec < self.s.min_vector_score and best_bm < self.s.min_bm25_score:
                debug["gate"] = "blocked_no_evidence"
                hits = []
            else:
                debug["gate"] = "passed"
        return hits, debug


# =============================================================================
# 【模块 11-前置 · HTTP 出网工具】目标文件：app/llm/_http.py
# （因为下面的 Embedding 模块就要用它，所以提前定义；Python 运行时解析，位置不影响调用）
# ★核心模块（阶段 2 复用）：所有出网请求都收敛到这里，方便统一限流、超时、降级、记账。
#   刻意不引入 openai SDK —— 一套 requests 级的实现足够覆盖所有兼容端点，
#   也避免把供应商 SDK 的版本问题带进业务代码。
# =============================================================================


def _http_post_json(url: str, payload: dict[str, Any], api_key: str,
                    timeout: float = 60.0, retries: int = 3) -> dict[str, Any]:
    """
    POST JSON，带指数退避重试。
    只对"可重试"的错误重试：429 限流、5xx、网络超时；4xx 参数错误立即失败。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            # 4xx（除 429）重试无意义，直接抛出
            if 400 <= ex.code < 500 and ex.code != 429:
                raise RuntimeError(f"LLM 接口返回 {ex.code}：{detail}") from ex
            last_err = RuntimeError(f"HTTP {ex.code}：{detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            last_err = RuntimeError(f"网络异常：{ex}")
        if attempt < retries:
            wait = min(2 ** (attempt - 1), 8)
            log(f"请求失败（{last_err}），{wait}s 后第 {attempt + 1} 次重试", "warn")
            time.sleep(wait)
    raise RuntimeError(f"请求最终失败：{url} → {last_err}")


# =============================================================================
# 【模块 10 · Prompt 模板】目标文件：app/agents/prompts/rag_answer.py
# ★核心模块（阶段 2 复用）：prompt 必须版本化，否则线上线下答案不可比，
#   评测集跑出来的分数也没法归因。PROMPT_VERSION 会写进 chat_messages，便于回溯。
#
#   三条硬约束直接写进 system prompt，并且**由模块 12 的护栏做程序化复核**——
#   Prompt 只是软约束，靠它压不住幻觉；这是架构文档 0.4 的核心判断。
# =============================================================================

PROMPT_VERSION = "rag-answer.stage1.v1"

SYSTEM_PROMPT = """你是一名严谨的薪酬制度问答助手，服务对象是薪酬共享服务中心（SSC）专员与 HRBP。

请严格遵守以下规则：
1. 【只依据资料】只能使用【资料】中出现的条款作答，不得使用你自己的常识或行业经验补充。
2. 【逐条引用】每一个结论句后必须标注来源角标，格式为 [1]、[2]，角标对应【资料】的编号；不要把角标写成 (1) 或 ①。
3. 【数字照抄】金额、比例、基数、天数、条号必须从资料中逐字照抄，禁止换算、合并、四舍五入或自行推算。资料没有给出的数字，直接说"资料未规定"。
4. 【无据拒答】如果【资料】不足以回答，或只覆盖了问题的一部分，请明确说明"根据现有资料无法确认……"，并指出缺少哪方面依据；不要猜测，不要给"通常做法"。
5. 【区分口径】若资料提及地区差异或政策优先，请在答案中提示适用范围（如上海地区）。
6. 输出使用简洁中文条目，不要复述资料原文整段，不要输出与问题无关的条款。"""

USER_PROMPT_TEMPLATE = """【资料】
{context}

【问题】
{question}

请依据上面的资料作答，并在每条结论后标注角标。"""


def build_prompt(question: str, hits: Sequence[RetrievalHit]) -> tuple[list[dict[str, str]], str]:
    """
    组装 messages，同时返回拼好的 context（护栏复核要用同一份文本）。

    资料格式刻意带上「编号 + 文档 + 条款 + chunk_id」：
      · 编号给模型引用；
      · 条款号给人核对；
      · chunk_id 给系统回溯到 L0 原文（架构文档 0.3 的 L1 铁律：
        每个节点必须挂 doc_id + chunk_id 才能回到真相源）。
    """
    blocks: list[str] = []
    for i, h in enumerate(hits, start=1):
        c = h.chunk
        header = f"[{i}] 《{c.doc_title}》{c.clause_range or c.heading_path}（{c.chunk_id}）"
        blocks.append(f"{header}\n{c.content}")
    context = "\n\n".join(blocks) if blocks else "（无可用资料）"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT_TEMPLATE.format(context=context, question=question)},
    ]
    return messages, context


# =============================================================================
# 【模块 11 · LLM 适配器】目标文件：app/llm/base.py + app/llm/providers/openai_compat.py
# ★核心模块（阶段 2 复用）：这是系统**唯一允许出网调用生成模型的地方**。
#   接口只有一个方法 chat(messages) -> LLMResult，屏蔽掉所有供应商差异。
#
#   两个实现：
#     MockChatModel           —— 抽取式假模型，零依赖、零成本、确定性，
#                                 用来验证"检索+引用+护栏"是否打通，不评估生成质量；
#     OpenAICompatChatModel   —— 真实模型，一家协议接所有厂商（含 vLLM / Ollama 本地部署）。
#
#   阶段 2 的扩展点：Azure（多一个 api-version 查询参数）、流式 SSE、
#   token 预算记账（app/llm/budget.py）—— 都可以在本接口内部实现，调用方无感。
# =============================================================================


@dataclass
class LLMResult:
    """统一返回结构，token 与耗时是成本核算的原始数据，必须回传。"""

    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    provider: str = ""


class ChatModel(abc.ABC):
    """生成模型协议。阶段 2 用 typing.Protocol 重写，保持同一签名。"""

    provider: str = "base"
    model: str = "base"

    @abc.abstractmethod
    def chat(self, messages: list[dict[str, str]], *, temperature: float = 0.2,
             max_tokens: int = 900) -> LLMResult:
        ...


class MockChatModel(ChatModel):
    """
    抽取式"假模型"：不生成新句子，只从召回切片里挑出与问题最相关的原句并按序拼接。

    它的价值在于**把检索质量和生成质量解耦**：
      · 如果 mock 答不出来，说明检索没召回对（问题在模块 08/09）；
      · 如果 mock 答得出来、真实模型答得不对，说明是生成或 prompt 的问题。
    这样调试时不会两个变量一起动。★不要把它当生产实现。★
    """

    provider = "mock"

    def __init__(self, top_sentences: int = 4):
        self.model = "mock-extractive"
        self.top_sentences = top_sentences

    def chat(self, messages, *, temperature: float = 0.2, max_tokens: int = 900) -> LLMResult:
        t0 = time.perf_counter()
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

        # 从 prompt 里把「资料」和「问题」拆出来（这里直接解析自己拼的模板，稳定可控）
        m = re.search(r"【资料】\n(.*?)\n\n【问题】\n(.*?)\n", user_msg, flags=re.S)
        context, question = (m.group(1), m.group(2)) if m else (user_msg, user_msg)

        q_tokens = set(tokenize(question))
        # 按 [1] [2] 切分资料块
        raw_blocks = re.split(r"\n(?=\[\d+\] 《)", context)
        scored: list[tuple[float, int, str]] = []
        for bi, block in enumerate(raw_blocks):
            idx_m = re.match(r"\[(\d+)\]", block.strip())
            cidx = int(idx_m.group(1)) if idx_m else bi + 1
            for sent in split_sentences(block):
                s = sent.strip()
                # 过滤三类噪音：过短、表格行、纯标题行。
                # 表格行（| ... |）脱离表头后无法独立成句，摘进答案只会误导读者。
                if len(s) < 12:
                    continue
                if s.startswith("|") or s.count("|") >= 2:
                    continue
                if re.match(r"^第[一二三四五六七八九十百零〇\d]+[条章]", s):
                    continue
                st = set(tokenize(s))
                if not st:
                    continue
                overlap = len(q_tokens & st) / math.sqrt(len(st))
                if overlap > 0:
                    scored.append((overlap, cidx, s))

        scored.sort(key=lambda x: -x[0])
        picked = scored[: self.top_sentences]

        if not picked:
            text = "根据现有资料无法确认该问题。检索到的制度条款中未出现与该问题相关的规定，请补充更具体的问题或提供所涉变更类型。"
        else:
            seen: set[str] = set()
            lines = ["（以下为由检索条款直接摘录的答案，当前使用离线 mock 模型）", ""]
            for _, cidx, s in picked:
                if s in seen:
                    continue
                seen.add(s)
                lines.append(f"- {s} [{cidx}]")
            text = "\n".join(lines)

        latency = int((time.perf_counter() - t0) * 1000)
        return LLMResult(
            text=text, model=self.model, provider=self.provider, latency_ms=latency,
            tokens_in=len(user_msg) // 2, tokens_out=len(text) // 2,
        )


class OpenAICompatChatModel(ChatModel):
    """
    真实生成模型。POST {base_url}/chat/completions，字段与 OpenAI 完全一致。
    已验证可对接：OpenAI / DeepSeek / 通义千问(compatible-mode) / Kimi / 智谱 / Ollama / vLLM。
    """

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 60.0,
                 retries: int = 3, provider: str = "openai_compat"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.provider = provider

    def chat(self, messages, *, temperature: float = 0.2, max_tokens: int = 900) -> LLMResult:
        t0 = time.perf_counter()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        data = _http_post_json(
            f"{self.base_url}/chat/completions", payload, self.api_key, self.timeout, self.retries
        )
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as ex:
            raise RuntimeError(f"LLM 返回结构异常：{str(data)[:300]}") from ex

        usage = data.get("usage") or {}
        return LLMResult(
            text=text.strip(),
            model=data.get("model", self.model),
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            provider=self.provider,
        )


def build_chat_model(s: Settings) -> ChatModel:
    """工厂：配置 → 具体实现。换供应商只改环境变量，业务代码零改动。"""
    if s.llm_provider == "mock":
        return MockChatModel()
    return OpenAICompatChatModel(
        base_url=s.llm_base_url, api_key=s.llm_api_key, model=s.llm_model,
        timeout=s.llm_timeout, retries=s.llm_max_retries, provider=s.llm_provider,
    )


# =============================================================================
# 【模块 12 · 答案护栏】目标文件：app/agents/guard.py
# ★核心模块（阶段 2 强烈建议复用）：这是把"幻觉率 0"从口号变成可执行检查的地方。
#
#   架构文档 0.4 的硬规矩：数字不由模型产出。落地方式是**程序化复核**——
#   把答案里出现的每个带单位的数字抽出来，逐个回查原文，
#   原文没有就是一次潜在幻觉，直接标记并降级为 partial。
#   这套检查在阶段 2 可以升级为"重试一次 + 证据不足则拒答"的闭环。
# =============================================================================

# 只抓"带单位"的数字，避免把条号、序号、角标误判成事实性数字
_NUM_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:%|％|元|天|个工作日|个工作日|个月|年|日|次|小时|分钟|倍|人|条|款)"
)
_CITE_RE = re.compile(r"\[(\d{1,2})\]")
_REFUSAL_HINTS = ("无法确认", "无法回答", "未规定", "资料未", "没有相关", "无法提供", "不足以")


@dataclass
class GuardResult:
    """护栏结论。阶段 2 把它整条写进 chat_messages.answer_status 与审计表。"""

    passed: bool
    status: str                      # answered / partial / refused
    cited_indexes: list[int] = field(default_factory=list)
    invalid_citations: list[int] = field(default_factory=list)
    unverified_numbers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _norm_number(s: str) -> str:
    """数字归一化：去千分位、去空格、全角转半角，用于和原文比对。"""
    s = s.strip().replace(",", "").replace("，", "").replace(" ", "")
    s = s.replace("％", "%")
    return s


def run_guard(answer: str, context: str, hit_count: int) -> GuardResult:
    """
    三个检查项：
      ① 引用有效性：角标必须指向真实存在的资料编号，且不能"一个角标都没有"；
      ② 数字可溯源：带单位的数字必须能在原文里找到；
      ③ 拒答识别：模型明确说"无法确认"时，不应被误判为异常。
    """
    warnings: list[str] = []
    res = GuardResult(passed=True, status="answered")

    # ① 引用检查
    cited = [int(x) for x in _CITE_RE.findall(answer)]
    res.cited_indexes = sorted(set(cited))
    res.invalid_citations = sorted({i for i in cited if i < 1 or i > hit_count})
    if res.invalid_citations:
        warnings.append(f"引用了不存在的资料编号：{res.invalid_citations}（共 {hit_count} 条资料）")
    if not cited:
        # 没有角标 = 结论不可举证。按架构文档口径，这属于"可疑答案"
        warnings.append("答案中没有任何引用角标，结论不可举证")

    # ② 数字溯源
    ctx_norm = _norm_number(context)
    unknown: list[str] = []
    for raw in _NUM_RE.findall(answer):
        if _norm_number(raw) not in ctx_norm:
            unknown.append(raw.strip())
    res.unverified_numbers = sorted(set(unknown))
    if res.unverified_numbers:
        warnings.append(
            "以下数字未能在资料原文中找到（疑似模型自造）：" + "、".join(res.unverified_numbers)
        )

    # ③ 状态判定
    refused = any(h in answer for h in _REFUSAL_HINTS)
    if refused:
        res.status = "refused"
    elif warnings:
        res.status = "partial"
    res.passed = res.status == "answered" and not warnings
    res.warnings = warnings
    return res


# =============================================================================
# 【模块 13 · 索引流水线】目标文件：app/ingestion/pipeline.py
# ★核心模块（阶段 2 复用）：解析→切片→向量→入库 四步编排，唯一变化是最后一步
#   从"写 FAISS"变成"写 pgvector + 发 DocumentIndexed 事件"。
#
#   manifest 是给运维用的，不是装饰：
#     · embedding_model / dim 变了 → 旧向量与新查询不在同一空间，必须重建；
#     · 文档 checksum 变了 → 说明制度被改过，需要走版本流程而不是原地覆盖。
#     这两条是架构文档 3.5「制度不能覆盖更新」在索引层的落地。
# =============================================================================


def build_index(s: Settings) -> tuple[VectorStore, dict[str, Any]]:
    """完整重建索引。生产环境应改为异步任务 + 进度上报，原型里同步执行。"""
    section("① 解析制度文件")
    if not s.doc_path.exists():
        raise FileNotFoundError(f"找不到制度文件：{s.doc_path}")
    raw = s.doc_path.read_text(encoding="utf-8")
    checksum = sha256_text(raw)
    meta, blocks = parse_markdown(raw)
    log(f"文件：{s.doc_path.name}", "ok")
    log(f"doc_id={meta.get('doc_id')}  version={meta.get('version')}  "
        f"生效 {meta.get('effective_from')} ~ {meta.get('effective_to') or '长期'}")
    log(f"算得 {len(blocks)} 个结构块（标题/正文/表格），checksum={checksum[:12]}…")

    section("② 切片（500 字 / 条款为装箱单元 / 表格不切 / 句末对齐 / 80 字重叠）")
    chunks = chunk_blocks(
        blocks,
        chunk_size=s.chunk_size,
        overlap=s.chunk_overlap,
        min_size=s.min_chunk_size,
        meta=meta,
        mode=s.chunk_mode,
    )
    if not chunks:
        raise RuntimeError("切片结果为空，请检查文档解析逻辑")
    lens = [c.char_len for c in chunks]
    log(f"共 {len(chunks)} 个切片，长度 min={min(lens)} / avg={sum(lens)//len(lens)} / max={max(lens)}"
        f"（目标 {s.chunk_size} 字，模式 {s.chunk_mode}）")
    for c in chunks[:3]:
        log(f"  {c.chunk_id}  [{c.clause_range or c.heading_path}]  {c.char_len} 字  "
            f"{c.content[:30].replace(chr(10), ' ')}…")

    section("③ 向量化")
    embedder = build_embedding_model(s)
    log(f"向量模型：{embedder.name}（dim={s.embedding_dim}，provider={s.embedding_provider}）")
    t0 = time.perf_counter()
    vectors = embedder.embed([c.content for c in chunks])
    log(f"完成 {len(vectors)} 条向量，耗时 {time.perf_counter() - t0:.2f}s", "ok")

    section("④ 写入向量库（FAISS）")
    store = VectorStore(chunks, embedder.dim)
    store.build(vectors)
    manifest = {
        "doc_path": str(s.doc_path),
        "doc_id": meta.get("doc_id", ""),
        "doc_title": meta.get("title", ""),
        "doc_version": meta.get("version", "1"),
        "doc_checksum": checksum,
        "effective_from": meta.get("effective_from", ""),
        "effective_to": meta.get("effective_to") or None,
        "chunk_size": s.chunk_size,
        "chunk_overlap": s.chunk_overlap,
        "chunk_mode": s.chunk_mode,
        "embedding_provider": s.embedding_provider,
        "embedding_model": embedder.name,
        "prompt_version": PROMPT_VERSION,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "tokenizer": "jieba.cut_for_search" if _HAS_JIEBA else "char-ngram",
    }
    store.save(s.index_dir, manifest)
    log(f"索引已写入：{s.index_dir}", "ok")
    log(f"后端：{store.backend}（{len(chunks)} 条向量）")
    return store, manifest


def load_index(s: Settings) -> tuple[VectorStore, dict[str, Any]]:
    """加载索引，并做一次"配置漂移"体检 —— 这是上线后最容易踩的坑。"""
    store, manifest = VectorStore.load(s.index_dir)
    embedder = build_embedding_model(s)
    if manifest.get("embedding_model") != embedder.name:
        log(f"索引用的向量模型是 {manifest.get('embedding_model')}，"
            f"当前配置是 {embedder.name}，两者不在同一向量空间，请先 build。", "err")
        raise RuntimeError("向量模型与索引不一致")
    if int(manifest.get("chunk_size", 0)) != s.chunk_size:
        log(f"切片参数不一致（索引 {manifest.get('chunk_size')}，配置 {s.chunk_size}），建议重建。", "warn")
    return store, manifest


def build_bm25(store: VectorStore) -> BM25Index:
    """BM25 倒排表在内存里现建（1000 条切片以内 < 10ms）。产出即用，无需持久化。"""
    return BM25Index([tokenize(c.content) for c in store.chunks])


# =============================================================================
# 【模块 14 · 编排器】目标文件：app/agents/orchestrator.py
# ★核心模块（阶段 2 复用）：一次问答的总调度，把 5 个协作者串起来。
#
#   检索 → 组装 prompt → 生成 → 护栏 → 组装引用
#
#   设计上的两个"故意"：
#     ① 无召回时**不进 LLM**，直接拒答。省 token，也让拒答行为可解释；
#     ② 引用列表由检索结果生成，不由模型输出解析 —— 模型只负责写角标，
#        角标与 chunk_id 的映射权在系统手里，模型无法伪造出处。
# =============================================================================


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


class Orchestrator:
    """阶段 1 版编排器。阶段 2 会在检索前插入 Router（意图分类），实现 4 条通道分流。"""

    def __init__(self, store: VectorStore, bm25: BM25Index, embedder: EmbeddingModel,
                 chat_model: ChatModel, settings: Settings):
        self.retriever = HybridRetriever(store, bm25, embedder, settings)
        self.chat_model = chat_model
        self.s = settings

    def answer(self, question: str, *, top_k: int | None = None,
               history: list[dict[str, str]] | None = None) -> RAGAnswer:
        t0 = time.perf_counter()
        question = question.strip()
        if not question:
            return RAGAnswer(question=question, answer="请输入有效问题。", status="error")

        # ① 混合检索（top3）
        hits, rdebug = self.retriever.retrieve(question, top_k=top_k)

        # ② 无召回 → 直接拒答，不浪费一次生成调用
        if not hits:
            return RAGAnswer(
                question=question,
                answer="根据现有制度资料无法确认该问题：未检索到相关条款。建议补充变更类型或具体条款主题后重试。",
                status="refused",
                debug={**rdebug, "refused_reason": "no_recall"},
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )

        # ③ 组装 prompt（history 为预留：阶段 2 接滚动摘要与指代消解）
        messages, context = build_prompt(question, hits)
        if history:
            # 只在 system 之后插入历史，且不参与检索——检索永远用"当前问题"，
            # 否则多轮之后 query 会被历史污染（架构文档 1.2 步骤②③的分工）。
            messages = [messages[0], *history, messages[-1]]

        # ④ 生成
        try:
            llm = self.chat_model.chat(
                messages,
                temperature=self.s.llm_temperature,
                max_tokens=self.s.llm_max_tokens,
            )
        except Exception as ex:
            log(f"生成失败：{ex}", "err")
            return RAGAnswer(
                question=question, answer=f"生成失败：{ex}", status="error",
                hits=hits, debug=rdebug,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )

        # ⑤ 护栏复核
        guard = run_guard(llm.text, context, len(hits))

        # ⑥ 组装引用：编号 → chunk 的映射由系统持有
        citations = [
            Citation(
                index=i,
                chunk_id=h.chunk.chunk_id,
                doc_id=h.chunk.doc_id,
                clause_no=h.chunk.clause_range or h.chunk.clause_no,
                heading_path=h.chunk.heading_path,
                score=round(h.fused_score, 6),
                snippet=h.chunk.content[:120],
            )
            for i, h in enumerate(hits, start=1)
        ]

        return RAGAnswer(
            question=question, answer=llm.text, status=guard.status,
            citations=citations, hits=hits, guard=guard,
            debug={**rdebug, "t_llm_ms": llm.latency_ms, "provider": llm.provider},
            model=llm.model, tokens_in=llm.tokens_in, tokens_out=llm.tokens_out,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )


# =============================================================================
# 【模块 15 · CLI 与检索评测】目标文件：scripts/ + tests/evals/runner.py
# 仅原型。阶段 2 这里会被 FastAPI 的 /v1/chat（SSE）替代；
# 但 eval 用例集要留下来 —— 架构文档 D5 的验收就靠它。
# =============================================================================


def render_answer(res: RAGAnswer, show_debug: bool = False) -> None:
    """把结果打印成人能读的样子：答案 + 引用 + 护栏结论。"""
    print()
    print(_SEP)
    print(f"问题：{res.question}")
    print(_SEP)
    print(res.answer)
    print()
    print("引用来源：")
    if not res.citations:
        print("  （无）")
    for c in res.citations:
        hit = next((h for h in res.hits if h.chunk.chunk_id == c.chunk_id), None)
        ch = hit.channels if hit else "-"
        print(f"  [{c.index}] {c.chunk_id}  {c.clause_no or '-'}  {ch}  "
              f"score={c.score:.5f}")
        print(f"      {c.heading_path}")
    print()
    g = res.guard
    if g:
        flag = "通过" if g.passed else "需关注"
        print(f"护栏：{flag}｜状态={g.status}｜引用 {g.cited_indexes or '无'}"
              + (f"｜未溯源数字 {g.unverified_numbers}" if g.unverified_numbers else ""))
        for w in g.warnings:
            print(f"  ! {w}")
    print(f"耗时 {res.latency_ms} ms｜模型 {res.model}｜prompt {res.prompt_version}｜"
          f"tokens in/out {res.tokens_in}/{res.tokens_out}")
    if show_debug:
        print("\n检索调试：")
        print(json.dumps(res.debug, ensure_ascii=False, indent=2))
        print("Top-K 明细：")
        for i, h in enumerate(res.hits, start=1):
            print(f"  [{i}] {h.chunk.chunk_id} fused={h.fused_score:.6f} "
                  f"vec={h.vector_score:.4f}(#{h.vector_rank}) "
                  f"bm25={h.bm25_score:.3f}(#{h.bm25_rank}) {h.chunk.clause_no}")
    print(_SEP)


# 内置评测集：问题 → 期望命中的关键证据（关键词）与期望条款号。
# 阶段 2 要扩展成 20 题（HR 同事出题）并纳入 CI，见架构文档 D5。
EVAL_CASES: list[dict[str, str]] = [
    {"q": "调薪后薪资超过薪酬带宽上限怎么处理？", "expect_clause": "第十三条", "expect_kw": "超带宽"},
    {"q": "2026 年上海社保缴费基数上限是多少？", "expect_clause": "第十八条", "expect_kw": "36,921"},
    {"q": "冻结期内可以改银行账户吗？", "expect_clause": "第八条", "expect_kw": "冻结期"},
    {"q": "单次补发最多覆盖几个月？", "expect_clause": "第三十条", "expect_kw": "3 个月"},
    {"q": "降薪需要员工书面确认吗？", "expect_clause": "第十四条", "expect_kw": "书面确认"},
    {"q": "银行账户验证打款的金额范围是多少？", "expect_clause": "第二十三条", "expect_kw": "0.01"},
    {"q": "L4 差异的判定条件和处置方式是什么？", "expect_clause": "第二十六条", "expect_kw": "5,000"},
    {"q": "制度修订后旧版本怎么处理？", "expect_clause": "第四条", "expect_kw": "新增版本"},
]


def run_eval(orchestrator: Orchestrator) -> None:
    """
    检索层评测：只看"证据有没有被召回"，不看生成质量。
    指标口径（对应架构文档 4.3）：
      · 命中率 hit@3      —— top3 里是否包含期望关键词
      · 条款准确 clause@3 —— top3 里是否包含期望条款号
    这两个指标低，说明切片或检索有问题，先别去调 prompt。
    """
    section("检索评测（hit@3 / clause@3）")
    hit = clause_ok = 0
    rows = []
    for i, case in enumerate(EVAL_CASES, start=1):
        res = orchestrator.answer(case["q"], top_k=3)
        contents = [h.chunk.content for h in res.hits]
        # 一个切片可能覆盖多个条款（如"第十七条–第十九条"），
        # 因此必须用 clauses 列表做集合判断，而不是对 clause_range 做子串匹配。
        clause_sets = [set(h.chunk.clauses) for h in res.hits]
        kw_hit = any(case["expect_kw"] in c for c in contents)
        cl_hit = any(case["expect_clause"] in cs for cs in clause_sets)
        hit += kw_hit
        clause_ok += cl_hit
        recalled = "、".join(h.chunk.clause_range or "(表)" for h in res.hits)
        rows.append((i, case["q"], "✓" if kw_hit else "×", "✓" if cl_hit else "×", recalled))

    print(pad("#", 4) + pad("问题", 38) + pad("关键词", 10) + pad("条款", 8) + "召回条款范围")
    for i, q, k, c, cl in rows:
        print(pad(str(i), 4) + pad(q, 38) + pad(k, 10) + pad(c, 8) + cl)
    n = len(EVAL_CASES)
    print(f"\nhit@3 = {hit}/{n} = {hit/n:.0%}    clause@3 = {clause_ok}/{n} = {clause_ok/n:.0%}")
    print("判定标准（架构文档 4.3）：引用正确率 ≥ 90%，即 8 题至少 8 中；低于此值先查切片与分词。")


def cmd_chat(orch: Orchestrator, show_debug: bool) -> None:
    """交互式问答。支持 /sources 查看上一条完整原文、/rebuild 重建索引。"""
    print("\n薪酬变更核对制度 · 问答原型（阶段 1）")
    print("输入问题回车即可；/sources 看完整原文，/debug 切换调试信息，/quit 退出")
    last: RAGAnswer | None = None
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in ("/quit", "/exit", "q"):
            break
        if q == "/debug":
            show_debug = not show_debug
            print(f"调试信息：{'开' if show_debug else '关'}")
            continue
        if q == "/sources" and last:
            for i, h in enumerate(last.hits, start=1):
                print(f"\n[{i}] {h.chunk.chunk_id} {h.chunk.heading_path}")
                print(h.chunk.content)
            continue
        last = orch.answer(q)
        render_answer(last, show_debug=show_debug)


def main(argv: list[str] | None = None) -> int:
    _init_console()
    parser = argparse.ArgumentParser(
        description="薪酬变更核对系统 · 阶段 1 RAG 最小原型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python stage1_rag_prototype.py build\n"
               "  python stage1_rag_prototype.py ask \"冻结期内能改银行账户吗\"\n"
               "  python stage1_rag_prototype.py eval",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("build", help="解析制度 → 切片 → 向量化 → 写入 FAISS")
    p_ask = sub.add_parser("ask", help="单次提问")
    p_ask.add_argument("question", nargs="+", help="问题文本")
    p_ask.add_argument("--debug", action="store_true", help="打印检索调试信息")
    p_ask.add_argument("--top-k", type=int, default=None, help="召回条数，默认 3")
    p_chat = sub.add_parser("chat", help="交互式问答")
    p_chat.add_argument("--debug", action="store_true")
    sub.add_parser("eval", help="跑内置检索评测")
    sub.add_parser("info", help="查看当前配置与索引状态")

    args = parser.parse_args(argv)
    s = Settings.from_env()

    if args.cmd == "build":
        build_index(s)
        return 0

    if args.cmd == "info":
        section("当前配置")
        print(json.dumps({
            "doc_path": str(s.doc_path),
            "index_dir": str(s.index_dir),
            "chunk_size": s.chunk_size, "chunk_overlap": s.chunk_overlap,
            "chunk_mode": s.chunk_mode,
            "top_k": s.top_k, "recall_k": s.recall_k, "rrf_k": s.rrf_k,
            "llm_provider": s.llm_provider, "llm_model": s.llm_model or "(未配置)",
            "llm_base_url": s.llm_base_url or "(未配置)",
            "embedding_provider": s.embedding_provider, "embedding_dim": s.embedding_dim,
            "as_of": s.as_of_date().isoformat(),
            "jieba": _HAS_JIEBA, "faiss": faiss is not None, "numpy": np is not None,
        }, ensure_ascii=False, indent=2))
        mpath = s.index_dir / VectorStore.MANIFEST_FILE
        if mpath.exists():
            print("\n索引 manifest：")
            print(mpath.read_text(encoding="utf-8"))
        else:
            print("\n索引尚未构建。")
        return 0

    # 其余命令都需要索引
    try:
        store, _ = load_index(s)
    except (FileNotFoundError, RuntimeError) as ex:
        log(str(ex), "err")
        log("先执行：python stage1_rag_prototype.py build", "step")
        return 1

    bm25 = build_bm25(store)
    embedder = build_embedding_model(s)
    chat_model = build_chat_model(s)
    orch = Orchestrator(store, bm25, embedder, chat_model, s)
    log(f"索引就绪：{len(store.chunks)} 条切片｜检索 top{s.top_k}｜生成模型 {chat_model.provider}/{chat_model.model}")

    if args.cmd == "ask":
        res = orch.answer(" ".join(args.question), top_k=args.top_k)
        render_answer(res, show_debug=args.debug)
        return 0

    if args.cmd == "eval":
        run_eval(orch)
        return 0

    # 无子命令 → 进交互模式，降低试用门槛
    cmd_chat(orch, show_debug=getattr(args, "debug", False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
