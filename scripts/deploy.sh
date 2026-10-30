#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 发布前体检（pre-release gate）
#
#   bash scripts/deploy.sh
#
# 关于"部署"这件事，这里说明白：
#   阶段 1 交付的是**库 + CLI**，没有常驻服务，所以没有"部署到服务器"这个动作。
#   本脚本做的是"发布前必须全绿"的检查 —— 静态检查、单测、建索引、检索评测。
#   阶段 2 引入 FastAPI 之后，这个脚本会扩成：docker build → push → k8s rollout，
#   但下面这些检查会原样保留在前置阶段，因为它们是**代码层**的质量闸门。
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-python}"
export PYTHONPATH=src

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "1/5 静态检查（未定义名 / 未使用导入）"
"$PY" -m pyflakes src scripts tests examples

step "2/5 单元与端到端测试"
"$PY" -m pytest -q

step "3/5 重建索引（确保产物来自当前代码）"
"$PY" -m my_tool build

step "4/5 检索评测（clause@3 未达标即失败）"
"$PY" scripts/eval_retrieval.py

step "5/5 产物摘要"
"$PY" - <<'PY'
import json
from pathlib import Path

m = json.loads(Path("data/index/manifest.json").read_text(encoding="utf-8"))
for k in ("doc_id", "doc_version", "doc_checksum", "chunk_count", "chunk_size",
          "chunk_mode", "embedding_model", "prompt_version", "built_at"):
    v = m.get(k)
    if k == "doc_checksum" and v:
        v = v[:12] + "…"
    print(f"  {k:<18} {v}")
PY

cat <<'EOF'

全部通过。

要发布什么：
  · 代码：src/ + scripts/ + config/
  · 索引：data/index/ 属于派生物，不随包发布；目标环境执行 `python -m my_tool build` 重建
  · 密钥：一律通过环境变量注入，绝不写进镜像或配置文件
EOF
