"""SSE 编码测试。格式错了浏览器会静默不解析，所以这里逐字断言。"""

from __future__ import annotations

import json

from my_tool.api import sse


def _split(frame: str) -> tuple[list[str], dict]:
    """把一帧拆成 (行列表, data 解析结果)，顺便校验必须以空行结尾。"""
    assert frame.endswith("\n\n"), "每一帧都必须以空行结束"
    lines = frame[:-2].split("\n")
    data_line = [l for l in lines if l.startswith("data:")][0]
    return lines, json.loads(data_line[5:].lstrip())


def test_frame_format():
    out = sse.frame("delta", {"text": "冻结期内"})
    lines, payload = _split(out)
    assert lines[0] == "event: delta"
    assert lines[1].startswith("data: ")
    assert payload == {"text": "冻结期内"}


def test_chinese_not_escaped():
    """中文必须原样输出。转义成 \\uXXXX 虽然前端能解析，但抓包与日志全废。"""
    out = sse.frame("status", {"text": "正在检索制度…"})
    assert "正在检索制度" in out
    assert "\\u" not in out


def test_data_is_single_line():
    """data 只能是单行：多行会让部分客户端把第二行当成新的字段。"""
    out = sse.frame("citations", {"snippet": "第一行\n第二行"})
    body = out[:-2].split("\n")
    assert len(body) == 2                       # 一行 event、一行 data
    assert json.loads(body[1][5:].lstrip())["snippet"] == "第一行\n第二行"


def test_comment_frame():
    out = sse.comment()
    assert out.startswith(": ")
    assert out.endswith("\n\n")
    sse.comment()                                # 可重复调用，无副作用


def test_error_frame_matches_json_contract():
    """
    流中错误的字段必须与 JSON 错误体一致（code/message/trace_id/detail），
    这样前端一个渲染函数就能覆盖两条错误路径。
    """
    out = sse.error_frame("LLM_001", "生成回答超时，请稍后重试", "tr_abc", {"upstream": "timeout"})
    _, payload = _split(out)
    assert payload["code"] == "LLM_001"
    assert payload["message"] == "生成回答超时，请稍后重试"
    assert payload["trace_id"] == "tr_abc"
    assert payload["detail"]["upstream"] == "timeout"


def test_sse_headers_disable_buffering():
    """缺了这两个头，代理会把流攒成一坨，打字机效果直接消失。"""
    assert sse.SSE_HEADERS["X-Accel-Buffering"] == "no"
    assert "no-transform" in sse.SSE_HEADERS["Cache-Control"]
