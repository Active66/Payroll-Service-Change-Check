# ---------------------------------------------------------------------------
# 启动员工问答入口（Windows / PowerShell）
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1 -Port 8080
#
# 前置条件：已建索引（python -m my_tool build）。
# ---------------------------------------------------------------------------

param(
    [string]$Host_ = "127.0.0.1",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

# 未安装成包时，让 `python -m my_tool` 能找到 src 布局
$env:PYTHONPATH = Join-Path $RepoRoot "src"

Write-Host "==> 服务地址：http://${Host_}:${Port}/" -ForegroundColor Cyan
Write-Host "==> 自检：curl http://${Host_}:${Port}/healthz" -ForegroundColor DarkGray

python -m my_tool serve --host $Host_ --port $Port
