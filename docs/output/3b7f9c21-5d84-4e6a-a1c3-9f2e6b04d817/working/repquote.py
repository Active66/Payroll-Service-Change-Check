# -*- coding: utf-8 -*-
"""把正文里的 ASCII 双引号成对替换为中文引号（跳过代码块），改善中文排版。"""
from pathlib import Path

MD = Path(r"E:\code\PayrollServiceChangeCheck\docs\薪酬服务变更核对系统-测试方案_v1.0.md")

lines = MD.read_text(encoding="utf-8").splitlines()
out = []
in_code = False
changed = 0
left = True

for line in lines:
    if line.startswith("```"):
        in_code = not in_code
        out.append(line)
        continue
    if in_code or '"' not in line:
        out.append(line)
        continue
    buf = []
    for ch in line:
        if ch == '"':
            buf.append("“" if left else "”")
            left = not left
            changed += 1
        else:
            buf.append(ch)
    out.append("".join(buf))

MD.write_text("\n".join(out) + "\n", encoding="utf-8")
print("replaced:", changed, "balanced:", left)
