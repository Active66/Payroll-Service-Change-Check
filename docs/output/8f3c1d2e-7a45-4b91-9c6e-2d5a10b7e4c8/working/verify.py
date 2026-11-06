# -*- coding: utf-8 -*-
import io
from pathlib import Path

from docx import Document

DOCX = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\stage3\薪酬服务变更核对系统_项目上线交付运行手册_v1.0.docx")
OUT = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\working\verify.txt")

d = Document(str(DOCX))
lines = []
lines.append("paragraphs=%d tables=%d sections=%d" % (len(d.paragraphs), len(d.tables), len(d.sections)))
lines.append("---- first 40 paragraphs ----")
for p in d.paragraphs[:40]:
    t = p.text.strip()
    if t:
        lines.append("[%s] %s" % (p.style.name, t[:80]))
lines.append("---- headings ----")
for p in d.paragraphs:
    if p.style.name.startswith("Heading"):
        lines.append("[%s] %s" % (p.style.name, p.text.strip()[:60]))
lines.append("---- table sizes ----")
for i, tb in enumerate(d.tables, 1):
    lines.append("table %d: rows=%d cols=%d first=%s" % (i, len(tb.rows), len(tb.columns), tb.rows[0].cells[0].text.strip()[:40]))
lines.append("---- footer sections ----")
for i, s in enumerate(d.sections):
    lines.append("section %d: start=%s" % (i, s.start_type))
    ftr = s.footer
    lines.append("  footer text: %s" % (" | ".join(p.text for p in ftr.paragraphs)))
OUT.write_text("\n".join(lines), encoding="utf-8")
print("ok")
