"""HTTP 层（阶段 2）。只做协议转换：解析请求、编码 SSE、翻译错误。

刻意不在 `__init__.py` 里 import app —— 那会在"只想引用错误码"时顺带把索引加载一遍。
需要应用实例时请显式 `from my_tool.api.app import create_app`。
"""
