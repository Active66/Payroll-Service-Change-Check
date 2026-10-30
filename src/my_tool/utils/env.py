"""可选依赖探测。

原型阶段要能在"裸环境"里跑起来（只装 numpy 也能跑通），所以三个可选依赖在这里
统一探测并暴露布尔量。其它模块只读标志位、不各自写 try/except —— 降级逻辑只要
散落到十几处，迟早会出现"某处判了、某处没判"的半崩溃状态。
"""

from __future__ import annotations

# numpy：向量运算的地基。缺了连 numpy 兜底后端都用不了，属于硬依赖，这里仍做软探测
# 只是为了把"缺包"的报错推迟到真正使用处，并给出可读提示。
try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    HAS_NUMPY = False

# faiss：向量索引。缺失时 VectorStore 自动降级为 numpy 暴力内积（结果等价、性能差）。
try:
    import faiss

    HAS_FAISS = True
except ImportError:  # pragma: no cover
    faiss = None  # type: ignore[assignment]
    HAS_FAISS = False

# jieba：中文分词。缺失时 tokenize 退化为「字符 unigram + bigram」，短查询仍可用，
# 但长条款的 BM25 词面匹配会明显变差 —— 生产环境必须装。
try:
    import jieba

    jieba.setLogLevel(60)  # 关掉 jieba 启动时构建词典的刷屏日志
    HAS_JIEBA = True
except ImportError:  # pragma: no cover
    jieba = None  # type: ignore[assignment]
    HAS_JIEBA = False

__all__ = ["HAS_FAISS", "HAS_JIEBA", "HAS_NUMPY", "faiss", "jieba", "np"]
