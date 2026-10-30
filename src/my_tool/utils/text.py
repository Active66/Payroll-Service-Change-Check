"""文本工具：分词、归一化、摘要、停用词表。

BM25 与哈希向量共用同一个 tokenize，保证两个通道对"词"的定义一致——
这是混合检索能互补而不是互相打架的前提。
"""

import hashlib
import re

from .env import HAS_JIEBA, jieba


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
if HAS_JIEBA:
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
    if HAS_JIEBA:
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

def sha256_text(text: str) -> str:
    """文件/文本摘要，对应 policy_documents.checksum，用于防重复索引与漂移检测。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
