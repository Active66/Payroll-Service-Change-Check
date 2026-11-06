# -*- coding: utf-8 -*-
import io
from docx import Document

p = r"E:\code\PayrollServiceChangeCheck\docs\薪酬服务变更核对系统-三档报价与投入产出测算书.docx"
d = Document(p)

texts = [x.text for x in d.paragraphs]
joined = "\n".join(texts)
tbl_text = "\n".join(c.text for t in d.tables for r in t.rows for c in r.cells)
all_text = joined + "\n" + tbl_text

lines = []
lines.append("paragraphs=%d tables=%d sections=%d" % (len(d.paragraphs), len(d.tables), len(d.sections)))
lines.append("has_placeholder_prompt=%s" % ("请输入" in all_text))
lines.append("chars=%d" % len(all_text))
lines.append("--- headings ---")
for x in d.paragraphs:
    if x.style.name.startswith("Heading"):
        lines.append("[%s] %s" % (x.style.name, x.text))
lines.append("--- first 12 paragraphs ---")
for x in d.paragraphs[:12]:
    if x.text.strip():
        lines.append("%s | %s" % (x.style.name, x.text[:60]))
lines.append("--- section info ---")
for i, s in enumerate(d.sections):
    lines.append("sec%d start=%s w=%.1fcm h=%.1fcm L=%.2f R=%.2f" % (
        i, s.start_type, s.page_width.cm, s.page_height.cm,
        s.left_margin.cm, s.right_margin.cm))
lines.append("--- table shapes ---")
for i, t in enumerate(d.tables):
    lines.append("t%d rows=%d cols=%d first=%s" % (i, len(t.rows), len(t.columns), t.rows[0].cells[0].text[:14]))

with io.open(r"E:\code\PayrollServiceChangeCheck\docs\.workbuddy\tmp_verify.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
