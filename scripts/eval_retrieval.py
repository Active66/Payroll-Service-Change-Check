#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索评测脚本（可放进 CI）。

    python scripts/eval_retrieval.py            # 用 data/index 下已建好的索引
    python scripts/eval_retrieval.py --rebuild  # 先重建再评测

退出码：
    0 = 达标（clause@3 ≥ 阈值）    1 = 未达标或索引缺失
CI 里卡合并看的就是这个退出码 —— "评测能跑"和"评测达标"是两件事。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from my_tool.core.config import Settings                            # noqa: E402
from my_tool.core.llm import build_chat_model                       # noqa: E402
from my_tool.core.orchestrator import Orchestrator                  # noqa: E402
from my_tool.core.pipeline import build_bm25, build_index, load_index  # noqa: E402
from my_tool.core.retrieval.embedding import build_embedding_model  # noqa: E402
from my_tool.evaluation import run_eval                             # noqa: E402
from my_tool.utils.console import _init_console, log                # noqa: E402

# 架构文档 4.3 的验收线。低于它说明检索/切片退化，应当阻断合并。
THRESHOLD = 0.90


def main() -> int:
    _init_console()
    ap = argparse.ArgumentParser(description="检索层评测（hit@3 / clause@3）")
    ap.add_argument("--rebuild", action="store_true", help="评测前先重建索引")
    ap.add_argument("--threshold", type=float, default=THRESHOLD, help=f"clause@3 达标线，默认 {THRESHOLD}")
    args = ap.parse_args()

    s = Settings.from_env()
    try:
        store, _ = build_index(s) if args.rebuild else load_index(s)
    except (FileNotFoundError, RuntimeError) as ex:
        log(str(ex), "err")
        log("先执行：python -m my_tool build", "step")
        return 1

    orch = Orchestrator(store, build_bm25(store), build_embedding_model(s), build_chat_model(s), s)
    metrics = run_eval(orch)

    if metrics["clause_at_k"] + 1e-9 < args.threshold:
        log(f"未达标：clause@3={metrics['clause_at_k']:.0%} < 阈值 {args.threshold:.0%}", "err")
        for f in metrics["failures"]:
            log(f"  失败用例 {f}", "warn")
        return 1

    log(f"达标：clause@3={metrics['clause_at_k']:.0%} ≥ {args.threshold:.0%}", "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
