"""核心业务逻辑：配置、模型、摄入、检索、生成、护栏、编排。"""

from .chunker import chunk_blocks
from .config import Settings
from .entities import Chunk, Citation, RetrievalHit
from .guard import GuardResult, run_guard
from .orchestrator import Orchestrator, RAGAnswer
from .parser import Block, parse_markdown
from .pipeline import build_bm25, build_index, load_index
from .prompt import PROMPT_VERSION, build_prompt

__all__ = [
    "Block",
    "Chunk",
    "Citation",
    "GuardResult",
    "Orchestrator",
    "PROMPT_VERSION",
    "RAGAnswer",
    "RetrievalHit",
    "Settings",
    "build_bm25",
    "build_index",
    "build_prompt",
    "chunk_blocks",
    "load_index",
    "parse_markdown",
    "run_guard",
]
