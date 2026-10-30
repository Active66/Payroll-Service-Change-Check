"""提示词模板的进程内缓存。

    为什么需要缓存：每问一次都查一次库，对这个规模其实也无所谓（0.1ms），
    但模板要在**每个请求的检索前**取到，而它几乎不变 —— 缓存能让"改模板立即生效"
    和"不为一个常量查库"同时成立。

    为什么 TTL 而不是纯事件失效：
      · 单 worker：保存时调用 invalidate()，下一个请求立刻拿到新版；
      · 多 worker：只有收到请求的那个 worker 会 invalidate，其他 worker
        靠 TTL 兜底。所以对外只能说"最多 5 秒内生效"，不能宣称"绝对实时"。
"""

import time
from typing import Any, Callable

from .prompt import BUILTIN_TEMPLATE, PromptTemplate

# =============================================================================
# 【模块 26 · 模板提供者】目标文件：src/my_tool/core/prompt_provider.py
#   core 不碰 SQL：loader 由装配层注入，返回 dict 或 None。
# =============================================================================


class PromptProvider:
    """取"当前生效的模板"。取不到就用内置默认，绝不因模板问题让问答挂掉。"""

    def __init__(self, loader: Callable[[], dict[str, Any] | None], ttl_seconds: float = 5.0):
        self._loader = loader
        self._ttl = max(0.0, float(ttl_seconds))
        self._cached: PromptTemplate | None = None
        self._expires_at = 0.0

    def active(self) -> PromptTemplate:
        now = time.monotonic()
        if self._cached is not None and now < self._expires_at:
            return self._cached

        tpl = self._from_db()
        self._cached = tpl
        self._expires_at = now + self._ttl
        return tpl

    def invalidate(self) -> None:
        """保存/回滚后调用。只是让下一次取数走库，不主动去查。"""
        self._cached = None
        self._expires_at = 0.0

    def _from_db(self) -> PromptTemplate:
        try:
            row = self._loader()
        except Exception:                          # noqa: BLE001 — 读库失败不该让问答 500
            return BUILTIN_TEMPLATE
        if not row:
            return BUILTIN_TEMPLATE
        try:
            return PromptTemplate(
                version=int(row.get("version") or 0),
                name=str(row.get("name") or ""),
                system_prompt=str(row.get("system_prompt") or ""),
                user_template=str(row.get("user_template") or ""),
            )
        except (TypeError, ValueError):
            return BUILTIN_TEMPLATE
