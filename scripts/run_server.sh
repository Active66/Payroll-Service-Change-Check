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

# 选一个**真的装了依赖**的解释器。写死 python 的后果是：环境被清过一次之后
# 启动直接 ModuleNotFoundError，而错误信息看起来像"代码坏了"。
pick_python() {
  local c
  # Windows 的 venv 是 Scripts/python.exe，Linux/macOS 才是 bin/python —— 两种都列，
  # 否则在 Git Bash 里跑会因为找不到解释器直接放弃
  for c in "${PYTHON:-}" \
           "$REPO_ROOT/.venv/Scripts/python.exe" "$REPO_ROOT/.venv/bin/python" \
           "$HOME/.workbuddy/binaries/python/envs/payroll311/Scripts/python.exe" \
           "$HOME/.workbuddy/binaries/python/envs/payroll311/bin/python" \
           python3 python; do
    [ -n "$c" ] || continue
    command -v "$c" >/dev/null 2>&1 || [ -x "$c" ] || continue
    if "$c" -c "import fastapi, jieba, numpy; fastapi.FastAPI; jieba.lcut; numpy.zeros(1)" >/dev/null 2>&1; then
      printf '%s' "$c"
      return 0
    fi
  done
  return 1
}

if ! PY="$(pick_python)"; then
  echo "找不到装好依赖的 Python。先执行：python -m pip install -r requirements.txt" >&2
  exit 1
fi
echo "==> Python：$PY"

# 用相对路径 "src"：脚本已经 cd 到仓库根，Windows 版 Python 不认 Git Bash 的
# /e/code/... 这种 POSIX 路径，写绝对路径反而会 "No module named my_tool"。
# 同时直接覆盖而不是追加：Windows 的 PYTHONPATH 分隔符是 ';'，
# 拿 ':' 拼上外部值会得到一条 Windows 解析不了的路径。
export PYTHONPATH="src"

echo "==> 服务地址：http://${HOST}:${PORT}/"
echo "==> 自检：curl -s http://${HOST}:${PORT}/healthz | python -m json.tool"
echo

exec "$PY" -m my_tool serve --host "$HOST" --port "$PORT"
