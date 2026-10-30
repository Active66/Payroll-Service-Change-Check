#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 本地一键初始化：建虚拟环境 → 装依赖 → 建索引 → 自检
#
#   bash scripts/build.sh              # 完整流程
#   bash scripts/build.sh --no-venv    # 已有环境，只装依赖 + 建索引
#
# Windows 用户也可直接跑 PowerShell 等价命令（见 README「快速开始」）。
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PY="${PYTHON:-python}"
USE_VENV=1
[[ "${1:-}" == "--no-venv" ]] && USE_VENV=0

echo "==> 仓库根：$REPO_ROOT"

# ---------------------------------------------------------------------------
# 1) 虚拟环境
# ---------------------------------------------------------------------------
if [[ $USE_VENV -eq 1 ]]; then
  if [[ ! -d .venv ]]; then
    echo "==> 创建虚拟环境 .venv"
    "$PY" -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate
  PY=python
fi
echo "==> Python：$("$PY" -V 2>&1)"

# ---------------------------------------------------------------------------
# 2) 依赖
# ---------------------------------------------------------------------------
echo "==> 安装依赖"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt

# ---------------------------------------------------------------------------
# 3) 建索引（解析 → 500 字切片 → 向量化 → FAISS）
#    注意：索引是派生物，data/index 不进版本库，必须各自重建。
# ---------------------------------------------------------------------------
echo "==> 构建索引"
PYTHONPATH=src "$PY" -m my_tool build

# ---------------------------------------------------------------------------
# 4) 自检：跑一遍评测，确认检索链路没有退化
# ---------------------------------------------------------------------------
echo "==> 检索评测"
PYTHONPATH=src "$PY" scripts/eval_retrieval.py

cat <<'EOF'

完成。常用命令：

  source .venv/bin/activate          # Windows: .venv\Scripts\activate
  PYTHONPATH=src python -m my_tool ask "冻结期内可以改银行账户吗？" --debug
  PYTHONPATH=src python -m my_tool chat
  python examples/demo.py

或先装成命令：pip install -e ".[dev]"  之后直接用 payroll-check build / ask / chat / eval
EOF
