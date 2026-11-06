# -*- coding: utf-8 -*-
from pathlib import Path

from docx import Document

DOCX = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\3b7f9c21-5d84-4e6a-a1c3-9f2e6b04d817\stage3\薪酬服务变更核对系统-测试方案_v1.0.docx")
OUT = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\3b7f9c21-5d84-4e6a-a1c3-9f2e6b04d817\working\verify.txt")

d = Document(str(DOCX))
lines = ["paragraphs=%d tables=%d sections=%d" % (len(d.paragraphs), len(d.tables), len(d.sections))]
lines.append("---- headings ----")
for p in d.paragraphs:
    if p.style.name.startswith("Heading"):
        lines.append("[%s] %s" % (p.style.name, p.text.strip()[:60]))

lines.append("---- table 2 (统计表) ----")
tb = d.tables[2]
for r in tb.rows:
    lines.append(" | ".join(c.text.strip() for c in r.cells))

lines.append("---- 含“小计”的单元格 ----")
cnt = 0
for i, tb in enumerate(d.tables, 1):
    for r in tb.rows:
        for c in r.cells:
            if "小计" in c.text or c.text.strip() == "":
                if "小计" in c.text:
                    lines.append("table %d: [%s]" % (i, c.text.strip()))
                    cnt += 1
lines.append("小计命中 %d 处" % cnt)

lines.append("---- 首段 ----")
for p in d.paragraphs[:14]:
    if p.text.strip():
        lines.append("%s" % p.text.strip()[:70])

OUT.write_text("\n".join(lines), encoding="utf-8")
print("ok")
