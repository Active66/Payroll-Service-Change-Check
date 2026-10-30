"""所有出网请求的唯一出口，便于统一做限流、超时、重试与成本记账。
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Iterator

from ...utils.console import log


# =============================================================================
# 【模块 11-前置 · HTTP 出网工具】目标文件：src/my_tool/core/llm/http.py
# （因为下面的 Embedding 模块就要用它，所以提前定义；Python 运行时解析，位置不影响调用）
# ★核心模块（阶段 2 复用）：所有出网请求都收敛到这里，方便统一限流、超时、降级、记账。
#   刻意不引入 openai SDK —— 一套 requests 级的实现足够覆盖所有兼容端点，
#   也避免把供应商 SDK 的版本问题带进业务代码。
# =============================================================================


def http_post_json(url: str, payload: dict[str, Any], api_key: str,
                    timeout: float = 60.0, retries: int = 3) -> dict[str, Any]:
    """
    POST JSON，带指数退避重试。
    只对"可重试"的错误重试：429 限流、5xx、网络超时；4xx 参数错误立即失败。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            # 4xx（除 429）重试无意义，直接抛出
            if 400 <= ex.code < 500 and ex.code != 429:
                raise RuntimeError(f"LLM 接口返回 {ex.code}：{detail}") from ex
            last_err = RuntimeError(f"HTTP {ex.code}：{detail}")
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            last_err = RuntimeError(f"网络异常：{ex}")
        if attempt < retries:
            wait = min(2 ** (attempt - 1), 8)
            log(f"请求失败（{last_err}），{wait}s 后第 {attempt + 1} 次重试", "warn")
            time.sleep(wait)
    raise RuntimeError(f"请求最终失败：{url} → {last_err}")


def http_post_stream(url: str, payload: dict[str, Any], api_key: str,
                     timeout: float = 60.0) -> Iterator[str]:
    """
    以 SSE 方式 POST，逐行 yield 原始文本（不含行尾换行）。

    与 http_post_json 的三个关键差别：

      ① **不重试**。流式响应一旦开始吐字，重试就意味着重头再来一遍，
         前端会看到重复内容。失败只在"建立连接阶段"判定，交给调用方报错。
      ② **超时语义是"读超时"**。urllib 的 timeout 作用于每次 socket 读，
         不是整个请求；所以长回答不会被一刀切断，而"卡住不动"会超时。
      ③ 用 with 保证响应被关闭，否则连接泄漏，跑几百次就耗尽句柄。

    返回的行需要调用方自己按 SSE 规范解析（`data: ...` / 空行 / `[DONE]`）。
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        # 关掉响应压缩：压缩会把流攒成块，打字机就变成了"一次性出现"
        "Accept-Encoding": "identity",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as ex:
        detail = ""
        try:
            detail = ex.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        # 4xx 是配置/鉴权问题，5xx 是上游抖动，由 api 层翻译成 LLM_002 / LLM_003
        raise RuntimeError(f"LLM 接口返回 {ex.code}：{detail}") from ex
    except (urllib.error.URLError, TimeoutError, OSError) as ex:
        raise RuntimeError(f"网络异常：{ex}") from ex

    with resp:
        for raw in resp:                      # 按行迭代，不会把整段读进内存
            if not raw:
                continue
            yield raw.decode("utf-8", errors="replace").rstrip("\r\n")
