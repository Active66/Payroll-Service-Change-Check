"""SSE 编码。只做一件事：把 (event, data) 变成符合规范的一帧文本。

规范要点（写错任何一条，浏览器都会静默不解析）：

    event: delta\n
    data: {"text":"..."}\n
    \n                     ← 空行才是"这一帧结束"

另外两条容易漏的：

  · `data:` 的值必须是**单行**。json.dumps 默认不换行，中文会被转义成 \\uXXXX，
    所以必须显式 `ensure_ascii=False`，否则前端收到的中文是转义串
    （虽然 JSON.parse 能还原，但抓包和日志里全是乱码，排障时非常痛苦）。
  · 以 `:` 开头的行是注释，按规范会被忽略。正好用来发心跳，
    防止 nginx / 负载均衡把"长时间没数据"的连接掐掉。
"""

import json
from typing import Any


def frame(event: str, data: dict[str, Any]) -> str:
    """编码一帧。返回 str，由 StreamingResponse 用 UTF-8 写出去。"""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def comment(text: str = "ping") -> str:
    """心跳帧。只有注释行，前端解析器会跳过，但连接不会因空闲被断开。"""
    return f": {text}\n\n"


def error_frame(code: str, message: str, trace_id: str = "",
                detail: dict[str, Any] | None = None) -> str:
    """
    流中途的错误帧。

    字段与 JSON 错误体完全一致（code/message/detail/trace_id），
    这样前端只需要一个 renderError()，不必区分"流前失败"与"流中失败"。
    """
    body: dict[str, Any] = {"code": code, "message": message, "trace_id": trace_id}
    if detail:
        body["detail"] = detail
    return frame("error", body)


# 统一响应头。三个都必须，缺一个就会"看起来在流式，实际被缓冲"：
#   · no-transform  → 禁止代理压缩/改写
#   · X-Accel-Buffering: no → nginx 不缓冲（等价于 proxy_buffering off）
#   · no-cache      → 中间层不缓存
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
