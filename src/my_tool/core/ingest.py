"""上传摄入：落盘 → 解析 → 切片。

    **这里不向量化**。向量化统一归 IndexManager 的重建流程，因为：
      · 多文档重建要复用缓存，逐次上传时算一遍、重建时再算一遍是纯浪费；
      · 上传立刻 embed 会让"上传大 PDF"卡住整个请求（几十秒的超时）。
    所以上传只负责把文件变成切片，然后触发一次后台重建。
"""

import hashlib
import re
from pathlib import Path
from typing import Any

from ..utils.text import sha256_text
from .chunker import chunk_blocks
from .config import Settings
from .parser import parse_markdown

# =============================================================================
# 【模块 23 · 文档摄入】目标文件：src/my_tool/core/ingest.py
# =============================================================================

ALLOWED_EXT = {".md", ".markdown", ".txt", ".pdf"}


class IngestError(ValueError):
    """摄入失败。kind 用来让上层选错误码：type / size / parse。"""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def safe_filename(name: str) -> str:
    """
    只保留文件名部分并去掉危险字符。

    `../../etc/passwd` 与 `C:\\Windows\\x.md` 都必须在这里被消化掉，
    否则上传接口本身就是任意文件写入。
    """
    base = Path(str(name).replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f<>:\"|?*]", "", base).strip()
    return base or "unnamed"


def decode_text(data: bytes) -> str:
    """
    中文文档编码兜底：UTF-8（含 BOM）→ GBK。

    GBK 在 Windows 上导出的制度文件里很常见，直接 utf-8 解码会炸掉整个上传；
    而失败一次，管理员往往就改用别的工具了。
    """
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise IngestError("parse", "文本编码无法识别，请另存为 UTF-8 后重试")


def save_upload(s: Settings, filename: str, data: bytes) -> tuple[Path, str]:
    """
    把上传内容写进 upload_dir，返回 (物理路径, 原始文件名)。

    物理文件名用内容摘要前 16 位：同名不同内容不会互相覆盖，
    也避免中文文件名在 Windows 与 Linux 之间搬来搬去的编码问题。
    """
    name = safe_filename(filename)
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise IngestError("type", "仅接受 .md / .txt / .pdf")

    limit = max(1, s.upload_max_mb) * 1024 * 1024
    if len(data) > limit:
        raise IngestError("size", f"文件过大（上限 {s.upload_max_mb} MB）")
    if not data:
        raise IngestError("parse", "文件是空的")

    s.upload_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()[:16]
    path = s.upload_dir / f"{digest}{ext}"
    path.write_bytes(data)
    return path, name


def _fallback_doc_id(name: str, content: str) -> str:
    """没有 front matter 也没有手填 doc_id 时，用文件名 + 内容摘要生成一个稳定 id。"""
    stem = Path(name).stem
    slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "-", stem).strip("-")[:24] or "DOC"
    digest = sha256_text(content)[:6].upper()
    return f"{slug}-{digest}"


def ingest(s: Settings, path: Path, source_file: str,
           form: dict[str, str] | None = None) -> dict[str, Any]:
    """
    解析 + 切片，返回一份"待入库"的完整结果。

    返回的 chunks 里每一片都带着 doc_id / doc_title / source_file，
    这样引用卡片能直接显示"哪份文件第几条"，不需要回表查。
    """
    from .pdf import extract

    form = {k: (v or "").strip() for k, v in (form or {}).items()}
    ext = path.suffix.lower()

    if ext == ".pdf":
        text, pdf_stats = extract(path, title=form.get("title", ""))
        # PDF 没有 front matter，meta 全靠表单或文件名推断
        raw_meta: dict[str, str] = {}
        blocks = parse_markdown(text)[1]
    else:
        text = decode_text(path.read_bytes())
        raw_meta, blocks = parse_markdown(text)
        pdf_stats = {}

    if not blocks:
        raise IngestError("parse", "未解析出任何内容，请检查文件是否为空或格式异常")

    # 元数据优先级：表单 > 文档 front matter > 从文件名推断
    doc_id = form.get("doc_id") or raw_meta.get("doc_id", "") or _fallback_doc_id(source_file, text)
    title = form.get("title") or raw_meta.get("title", "") or Path(source_file).stem
    version_raw = form.get("version") or raw_meta.get("version", "1") or "1"
    try:
        version = max(1, int(float(version_raw)))
    except ValueError:
        version = 1

    meta = {
        "doc_id": doc_id,
        "title": title,
        "version": version,
        "doc_type": form.get("doc_type") or raw_meta.get("doc_type", "policy"),
        "region_code": form.get("region_code") or raw_meta.get("region_code", ""),
        "effective_from": form.get("effective_from") or raw_meta.get("effective_from", ""),
        "effective_to": form.get("effective_to") or raw_meta.get("effective_to") or "",
        "source_file": source_file,
    }

    chunks = chunk_blocks(
        blocks,
        chunk_size=s.chunk_size,
        overlap=s.chunk_overlap,
        min_size=s.min_chunk_size,
        meta=meta,
        mode=s.chunk_mode,
    )
    if not chunks:
        raise IngestError("parse", "切片结果为空，文档结构可能异常")

    total_chars = sum(c.char_len for c in chunks)
    stats = {
        **pdf_stats,
        "blocks": len(blocks),
        "chunks": len(chunks),
        "chars": total_chars,
        "checksum": sha256_text(text),
        "ext": ext,
    }
    return {"meta": meta, "chunks": chunks, "stats": stats, "text": text}


def describe_error(ex: IngestError) -> str:
    """把摄入异常翻译成人能读懂的一句话，页面直接显示。"""
    return ex.reason


__all__ = ["ALLOWED_EXT", "IngestError", "decode_text", "describe_error",
           "ingest", "safe_filename", "save_upload"]
