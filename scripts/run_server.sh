#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 启动员工问答入口（FastAPI + SSE）
#
#   bash scripts/run_server.sh              # 127.0.0.1:8000
#   bash scripts/run_server.sh 0.0.0.0 8080 # 指定地址与端口
#
# 前置条件：已建索引（bash scripts/build.sh 或 python -m my_tool build）。
# 索引缺失时服务仍会启动，但 /chat 会返回 IDX_001，/healthz 里能看到原因。
# ---------------------------------------------------------------------------
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HOST="${1:-127.0.0.1}"
PORT="${2:-8000}"
PY="${PYTHON:-python}"

export PYTHONPATH="${PYTHONPATH:-src}"

echo "==> 服务地址：http://${HOST}:${PORT}/"
echo "==> 自检：curl -s http://${HOST}:${PORT}/healthz | python -m json.tool"
echo

exec "$PY" -m my_tool serve --host "$HOST" --port "$PORT"
