"""Agent 工具集装配。

    一个函数把 9 个工具装进 registry，顺序按"读 → 判 → 写 → 交互"。
    这个顺序不影响模型（它能看见全部 schema），但影响日志与文档的可读性。
"""

from __future__ import annotations

from .registry import Registry
from .tools import check as check_tools
from .tools import read as read_tools
from .tools import write as write_tools

__all__ = ["build_registry", "Registry"]


def build_registry() -> Registry:
    """构造完整工具集。无状态，可以每次请求建一个新的（成本只有 dict 操作）。"""
    registry = Registry()
    read_tools.register(registry)
    check_tools.register(registry)
    write_tools.register(registry)
    return registry


def read_only_names() -> list[str]:
    """
    只读工具名。给 Router 用：闲聊类问题不该看到 create_draft ——
    模型看到一个能建单的工具，会倾向于用它。
    """
    return ["lookup_policy", "lookup_param", "get_employee", "get_change_order",
            "check_rules"]
