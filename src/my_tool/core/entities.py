"""领域模型。字段与数据库表 policy_document_chunks / chat_messages.citations 对齐。
"""

from dataclasses import asdict, dataclass, field
from typing import Any


# =============================================================================
# 【模块 03 · 领域模型】目标文件：src/my_tool/core/entities.py
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
    source_file: str = ""        # 物理文件名，前端引用卡片展示用（阶段 2 新增）

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

    # —— 阶段 2 新增：前端"引用来源"卡片要展示"文件名 + 片段" ——
    #   doc_title 用于界面上的《》书名号展示，source_file 是可下载/可追溯的物理文件名。
    #   两者都冗余自 Chunk（切片里已有），不额外查库，也不破坏已有调用方（有默认值）。
    doc_title: str = ""
    source_file: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
