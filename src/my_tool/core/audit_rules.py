"""审计预筛规则。

    **这些规则只产生"提示"，不产生"结论"**：
    命中不等于违规，只是把"值得看一眼"的条目挑出来。人工标记是另一张表，
    两者分开存，否则"确认违规 3 条"这个数字会被自动预筛污染，失去意义。

    为什么用规则而不是让另一个 LLM 当判官：
      · 可解释 —— 每条命中都能指给管理员看"因为出现了这个数字"；
      · 零成本、零延迟 —— 列表页每翻一页都调一次模型是不可接受的；
      · 判官本身也会幻觉，而且它的错更难申诉。
"""

import re
from typing import Any, Sequence

# 带单位的数字：金额、比例、倍数、天数、小时。只抓这些，避免把"第 3 条"也算进去
_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万元|元|%|倍|天|小时|个工作日|工作日)")

# 推测语气：模型在"没有依据但想显得有用"时最常说的词
_HEDGE_WORDS = ("通常", "一般来说", "大概是", "应该是", "建议您", "一般情况", "原则上可以",
                "多数公司", "行业惯例", "据了解")


def _strip_num(s: str) -> str:
    return s.replace(",", "").strip()


def assess(answer: str, citations: Sequence[dict[str, Any]], status: str) -> list[dict[str, str]]:
    """
    返回命中的规则列表，每项 {"code", "label"}。空列表表示没有可疑点。

    调用方（列表页）对当前页的 20 条现算，不落库 —— 规则改了历史数据立刻按新规则重算，
    不需要回填。
    """
    hits: list[dict[str, str]] = []
    text = answer or ""
    cites = list(citations or [])

    # R1 未溯源数字：回答里带单位的数字，在任何一条引用片段里都找不到
    cite_text = " ".join(_strip_num(str(c.get("snippet", ""))) for c in cites)
    unverified = []
    for m in _NUM_RE.finditer(text):
        num = _strip_num(m.group(1))
        if num and num not in cite_text:
            unverified.append(num)
    if unverified:
        hits.append({"code": "R1",
                     "label": "未溯源数字 " + "、".join(dict.fromkeys(unverified))})

    # R2 拒答却给了结论：状态是拒答，正文却出现带单位的数字。
    # 判据不用"正文长度超过 N 字"这类魔法数：拒答时出现金额本身就是矛盾，
    # 而系统的固定拒答话术不含任何数字，所以不会误报。
    if status == "refused" and _NUM_RE.search(text):
        hits.append({"code": "R2", "label": "拒答却给出金额/比例"})

    # R3 推测语气
    found = [w for w in _HEDGE_WORDS if w in text]
    if found:
        hits.append({"code": "R3", "label": "推测语气 " + "、".join(found[:3])})

    # R4 无引用作答：状态下是正常作答，却一条引用都没有
    if status in ("answered", "partial") and not cites:
        hits.append({"code": "R4", "label": "无引用作答"})

    return hits


def risk_summary(answer: str, citations: Sequence[dict[str, Any]], status: str) -> str:
    """列表页显示用的一行摘要。没有命中就返回空串。"""
    hits = assess(answer, citations, status)
    return "；".join(h["label"] for h in hits)
