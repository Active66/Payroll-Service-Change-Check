"""检索层评测（hit@3 / clause@3）。只看"证据有没有被召回"，不看生成质量。

两个指标低 → 先查切片与分词，别去调 prompt。

返回指标字典而不是只打印，是为了让 CI 和 scripts/eval_retrieval.py 能据阈值决定退出码：
"评测能跑"和"评测达标"是两件事，只有后者值得卡合并。
"""

from typing import Any

from .core.eval_dataset import EVAL_CASES
from .core.orchestrator import Orchestrator
from .utils.console import pad, section


def run_eval(orchestrator: Orchestrator, top_k: int = 3) -> dict[str, Any]:
    """
    检索层评测：只看"证据有没有被召回"，不看生成质量。
    指标口径（对应架构文档 4.3）：
      · 命中率 hit@3      —— top3 里是否包含期望关键词
      · 条款准确 clause@3 —— top3 里是否包含期望条款号
    这两个指标低，说明切片或检索有问题，先别去调 prompt。

    返回：{"hit_at_k": 0.0~1.0, "clause_at_k": 0.0~1.0, "n": 题数, "failures": [...]}
    """
    section(f"检索评测（hit@{top_k} / clause@{top_k}）")
    hit = clause_ok = 0
    rows = []
    failures: list[str] = []
    for i, case in enumerate(EVAL_CASES, start=1):
        res = orchestrator.answer(case["q"], top_k=top_k)
        contents = [h.chunk.content for h in res.hits]
        # 一个切片可能覆盖多个条款（如"第十七条–第十九条"），
        # 因此必须用 clauses 列表做集合判断，而不是对 clause_range 做子串匹配。
        clause_sets = [set(h.chunk.clauses) for h in res.hits]
        kw_hit = any(case["expect_kw"] in c for c in contents)
        cl_hit = any(case["expect_clause"] in cs for cs in clause_sets)
        hit += kw_hit
        clause_ok += cl_hit
        if not (kw_hit and cl_hit):
            failures.append(f"#{i} {case['q']}（期望 {case['expect_clause']} / {case['expect_kw']}）")
        recalled = "、".join(h.chunk.clause_range or "(表)" for h in res.hits)
        rows.append((i, case["q"], "✓" if kw_hit else "×", "✓" if cl_hit else "×", recalled))

    print(pad("#", 4) + pad("问题", 38) + pad("关键词", 10) + pad("条款", 8) + "召回条款范围")
    for i, q, k, c, cl in rows:
        print(pad(str(i), 4) + pad(q, 38) + pad(k, 10) + pad(c, 8) + cl)
    n = len(EVAL_CASES)
    print(f"\nhit@{top_k} = {hit}/{n} = {hit/n:.0%}    clause@{top_k} = {clause_ok}/{n} = {clause_ok/n:.0%}")
    print("判定标准（架构文档 4.3）：引用正确率 ≥ 90%，即 8 题至少 8 中；低于此值先查切片与分词。")

    return {
        "n": n,
        "hit_at_k": hit / n,
        "clause_at_k": clause_ok / n,
        "failures": failures,
    }
