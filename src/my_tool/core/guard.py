"""答案护栏：引用有效性 + 数字可溯源 + 拒答识别，把"幻觉率 0"变成可执行检查。
"""

import re
from dataclasses import dataclass, field


# =============================================================================
# 【模块 12 · 答案护栏】目标文件：src/my_tool/core/guard.py
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
