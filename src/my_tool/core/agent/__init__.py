"""Agent 子系统。

    分层（自下而上，每层只依赖下面一层）：

        tools/     具体工具的实现。每个工具一个 handler，只做一件事
        rules.py   规则引擎。把制度条款变成可复现判定，只算不写
        gate.py    校验凭证。顺序约束的硬边界
        registry.py 工具注册、参数校验、工具集裁剪、轨迹
        loop.py    Agent 循环（批次 4）

    这个包**不认识 FastAPI**。路由层通过 `loop.run()` 调用它，
    这样 CLI、单测、将来的异步 worker 都能复用同一套 Agent 逻辑。
"""

from .registry import Registry, ToolContext, ToolResult, ToolSpec
from .toolset import build_registry

__all__ = ["Registry", "ToolContext", "ToolResult", "ToolSpec", "build_registry"]
