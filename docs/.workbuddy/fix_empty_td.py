# -*- coding: utf-8 -*-
import re, io, sys

p = r"E:\code\PayrollServiceChangeCheck\docs\output\20261106-quote-roi\stage2\formatted-报价书.html"
with io.open(p, encoding="utf-8") as f:
    s = f.read()

before_empty = len(re.findall(r"<td([^>]*)></td>", s))
s = re.sub(r"<td([^>]*)></td>", lambda m: "<td%s>\u3000</td>" % m.group(1), s)
s = s.replace("&nbsp;", "\u3000")
s = s.replace("<th></th>", "<th>\u3000</th>")

with io.open(p, "w", encoding="utf-8") as f:
    f.write(s)

with io.open(r"E:\code\PayrollServiceChangeCheck\docs\.workbuddy\tmp_fix.txt", "w", encoding="utf-8") as f:
    f.write("empty td fixed: %d\n" % before_empty)
    f.write("remaining empty td: %d\n" % len(re.findall(r"<td([^>]*)></td>", s)))
