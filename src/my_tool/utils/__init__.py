"""通用工具：终端输出、文本处理、可选依赖探测。"""

from .console import SEP, _init_console, disp_width, log, pad, section
from .text import STOPWORDS, normalize_text, sha256_text, tokenize

__all__ = [
    "SEP",
    "STOPWORDS",
    "_init_console",
    "disp_width",
    "log",
    "normalize_text",
    "pad",
    "section",
    "sha256_text",
    "tokenize",
]
