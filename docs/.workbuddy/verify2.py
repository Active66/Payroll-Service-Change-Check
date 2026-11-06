# -*- coding: utf-8 -*-
import io
from docx import Document

p = r"E:\code\PayrollServiceChangeCheck\docs\薪酬服务变更核对系统-三档报价与投入产出测算书.docx"
d = Document(p)
out = []
for i, s in enumerate(d.sections):
    h = " | ".join(x.text for x in s.header.paragraphs if x.text.strip())
    f = " | ".join(x.text for x in s.footer.paragraphs if x.text.strip())
    out.append("sec%d header=[%s] footer=[%s] linked=%s" % (i, h, f, s.header.is_linked_to_previous))
# 抽查关键金额是否齐全
need = ["368,880", "826,800", "1,170,240", "780,000", "−678,514", "111,380",
        "375,775", "216.67", "14.4 倍", "37.6 万元", "114", "84"]
allt = "\n".join(x.text for x in d.paragraphs) + "\n" + "\n".join(c.text for t in d.tables for r in t.rows for c in r.cells)
for n in need:
    out.append("%s -> %s" % (n, n in allt))
with io.open(r"E:\code\PayrollServiceChangeCheck\docs\.workbuddy\tmp_verify.txt", "w", encoding="utf-8") as fh:
    fh.write("\n".join(out))
