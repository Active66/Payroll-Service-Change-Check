"""离线抽取式假模型：把检索质量与生成质量解耦，调试时不同时动两个变量。
"""

import math
import re
import time
from typing import Iterator

from ...utils.text import tokenize
from ..chunker import split_sentences
from .base import ChatModel, LLMResult, StreamChunk


def _stream_pieces(text: str, size: int = 14) -> Iterator[str]:
    """
    把成品答案切成"看起来像逐字生成"的帧。

    为什么不按固定字数硬切：中文按标点停顿更像真人输出，且前端拿到的是完整短语，
    打字机不会出现"半个词先出现"的抖动。过短的片段（如换行、单字标点）合并到下一帧。
    """
    buf = ""
    for piece in re.findall(r"[^。；\n]*[。；\n]|[^。；\n]+$", text):
        buf += piece
        if len(buf) >= size:
            yield buf
            buf = ""
    if buf:
        yield buf


class MockChatModel(ChatModel):
    """
    抽取式"假模型"：不生成新句子，只从召回切片里挑出与问题最相关的原句并按序拼接。

    它的价值在于**把检索质量和生成质量解耦**：
      · 如果 mock 答不出来，说明检索没召回对（问题在模块 08/09）；
      · 如果 mock 答得出来、真实模型答得不对，说明是生成或 prompt 的问题。
    这样调试时不会两个变量一起动。★不要把它当生产实现。★
    """

    provider = "mock"

    def __init__(self, top_sentences: int = 4):
        self.model = "mock-extractive"
        self.top_sentences = top_sentences

    def _compose(self, messages) -> tuple[str, str]:
        """
        抽取主逻辑。chat() 与 chat_stream() 共用这一份，
        否则"流式答得和 CLI 不一样"会成为最难查的一类 bug。
        返回 (答案文本, 当前用户消息原文)。
        """
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

        # 从 prompt 里把「资料」和「问题」拆出来（这里直接解析自己拼的模板，稳定可控）
        m = re.search(r"【资料】\n(.*?)\n\n【问题】\n(.*?)\n", user_msg, flags=re.S)
        context, question = (m.group(1), m.group(2)) if m else (user_msg, user_msg)

        q_tokens = set(tokenize(question))
        # 按 [1] [2] 切分资料块
        raw_blocks = re.split(r"\n(?=\[\d+\] 《)", context)
        scored: list[tuple[float, int, str]] = []
        for bi, block in enumerate(raw_blocks):
            idx_m = re.match(r"\[(\d+)\]", block.strip())
            cidx = int(idx_m.group(1)) if idx_m else bi + 1
            for sent in split_sentences(block):
                s = sent.strip()
                # 过滤三类噪音：过短、表格行、纯标题行。
                # 表格行（| ... |）脱离表头后无法独立成句，摘进答案只会误导读者。
                if len(s) < 12:
                    continue
                if s.startswith("|") or s.count("|") >= 2:
                    continue
                if re.match(r"^第[一二三四五六七八九十百零〇\d]+[条章]", s):
                    continue
                st = set(tokenize(s))
                if not st:
                    continue
                overlap = len(q_tokens & st) / math.sqrt(len(st))
                if overlap > 0:
                    scored.append((overlap, cidx, s))

        scored.sort(key=lambda x: -x[0])
        picked = scored[: self.top_sentences]

        if not picked:
            text = "根据现有资料无法确认该问题。检索到的制度条款中未出现与该问题相关的规定，请补充更具体的问题或提供所涉变更类型。"
        else:
            seen: set[str] = set()
            lines = ["（以下为由检索条款直接摘录的答案，当前使用离线 mock 模型）", ""]
            for _, cidx, s in picked:
                if s in seen:
                    continue
                seen.add(s)
                lines.append(f"- {s} [{cidx}]")
            text = "\n".join(lines)
        return text, user_msg

    def chat(self, messages, *, temperature: float = 0.2, max_tokens: int = 900) -> LLMResult:
        t0 = time.perf_counter()
        text, user_msg = self._compose(messages)
        latency = int((time.perf_counter() - t0) * 1000)
        return LLMResult(
            text=text, model=self.model, provider=self.provider, latency_ms=latency,
            tokens_in=len(user_msg) // 2, tokens_out=len(text) // 2,
        )

    def chat_stream(self, messages, *, temperature: float = 0.2,
                    max_tokens: int = 900) -> Iterator[StreamChunk]:
        """
        流式版：答案一次算完，再切成若干帧吐出去。

        注意这里**故意不加 sleep**：服务端不该为"看起来像流式"而故意变慢，
        节奏感是前端打字机的职责（见 web/app.js 的二级缓冲）。
        """
        t0 = time.perf_counter()
        text, user_msg = self._compose(messages)
        for piece in _stream_pieces(text):
            yield StreamChunk(text=piece)
        yield StreamChunk(result=LLMResult(
            text=text, model=self.model, provider=self.provider,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            tokens_in=len(user_msg) // 2, tokens_out=len(text) // 2,
        ))
