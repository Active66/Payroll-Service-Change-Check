"""终端输出与日志。阶段 2 换成 structlog + 审计日志分流，调用点不用改。
"""

import sys
import unicodedata
from datetime import datetime


# =============================================================================
# 【模块 02 · 日志与终端输出】目标文件：src/my_tool/utils/console.py
# 非核心（阶段 2 换成 structlog + 审计日志分流），这里只保证输出可读、可重定向。
# =============================================================================

SEP = "─" * 78


def _init_console() -> None:
    """Windows 控制台默认 cp936，打印中文与箭头发送乱码，这里强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def log(msg: str, level: str = "info") -> None:
    """统一日志出口。阶段 2 换成结构化日志（含 trace_id），调用点不用改。"""
    prefix = {"info": "·", "ok": "✓", "warn": "!", "err": "×", "step": "»"}.get(level, "·")
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {prefix} {msg}")


def section(title: str) -> None:
    print(f"\n{SEP}\n{title}\n{SEP}")

def disp_width(s: str) -> int:
    """终端显示宽度：中文/全角算 2 列，西文算 1 列。表格对齐要用它，否则中文全歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int) -> str:
    """按显示宽度右补空格（超宽则截断），让命令行表格能对齐。"""
    while disp_width(s) > width:
        s = s[:-1]
    return s + " " * max(0, width - disp_width(s))
