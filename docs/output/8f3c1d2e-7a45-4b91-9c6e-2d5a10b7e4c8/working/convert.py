# -*- coding: utf-8 -*-
import io
import json
import os
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(r"C:\Users\ROG\.workbuddy\plugins\cache\workbuddy-builtin\tencent-docx\5.5.6-wb.38337834.g5f969292.h4918b5ced607")
SCRIPTS = PLUGIN / "skills" / "html-to-docx" / "scripts"
HTML = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\stage2\formatted-项目上线交付运行手册.html")
DOCX = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\stage3\薪酬服务变更核对系统_项目上线交付运行手册_v1.0.docx")
OUT = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\working\convert.json")

home = Path(os.path.expanduser("~"))
cands = [
    home / ".venv-html-to-docx" / "Scripts" / "python.exe",
    home / ".venv-html-to-docx" / "bin" / "python",
]
py = next((p for p in cands if p.exists()), None)

log = []
log.append("venv candidates: %s" % [str(p) for p in cands])
log.append("picked: %s" % (str(py) if py else "NONE"))

if py is None:
    OUT.write_text("\n".join(log), encoding="utf-8")
    sys.exit(3)

DOCX.parent.mkdir(parents=True, exist_ok=True)
cmd = [
    str(py), "-m", "html_to_docx", "convert", str(HTML),
    "-o", str(DOCX),
    "--page-size", "A4",
    "--orientation", "portrait",
    "--margin-top", "2.5",
    "--margin-bottom", "2.0",
    "--margin-left", "2.5",
    "--margin-right", "2.5",
]
r = subprocess.run(cmd, cwd=str(SCRIPTS), capture_output=True, text=True, encoding="utf-8", errors="replace")
log.append("cmd: %s" % " ".join(cmd))
log.append("exit: %s" % r.returncode)
log.append("stdout: %s" % (r.stdout or ""))
log.append("stderr: %s" % (r.stderr or "")[-3000:])
log.append("docx exists: %s size: %s" % (DOCX.exists(), DOCX.stat().st_size if DOCX.exists() else 0))
OUT.write_text("\n".join(log), encoding="utf-8")
print("ok")
