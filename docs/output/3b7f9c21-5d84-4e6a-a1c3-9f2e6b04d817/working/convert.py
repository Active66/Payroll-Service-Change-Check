# -*- coding: utf-8 -*-
import os
import shutil
import subprocess
from pathlib import Path

PLUGIN = Path(r"C:\Users\ROG\.workbuddy\plugins\cache\workbuddy-builtin\tencent-docx\5.5.6-wb.38337834.g5f969292.h4918b5ced607")
SCRIPTS = PLUGIN / "skills" / "html-to-docx" / "scripts"

ROOT = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\3b7f9c21-5d84-4e6a-a1c3-9f2e6b04d817")
HTML = ROOT / "stage2" / "formatted-测试方案.html"
DOCX = ROOT / "stage3" / "薪酬服务变更核对系统-测试方案_v1.0.docx"
MD = Path(r"E:\code\PayrollServiceChangeCheck\docs\薪酬服务变更核对系统-测试方案_v1.0.md")
OUT = ROOT / "working" / "convert.json"

# 溯源：md 作为 stage1 产物
(ROOT / "stage1").mkdir(parents=True, exist_ok=True)
shutil.copyfile(MD, ROOT / "stage1" / "final_draft.md")

home = Path(os.path.expanduser("~"))
py = home / ".venv-html-to-docx" / "Scripts" / "python.exe"
if not py.exists():
    py = home / ".venv-html-to-docx" / "bin" / "python"

DOCX.parent.mkdir(parents=True, exist_ok=True)
cmd = [
    str(py), "-m", "html_to_docx", "convert", str(HTML),
    "-o", str(DOCX),
    "--page-size", "A4", "--orientation", "portrait",
    "--margin-top", "2.5", "--margin-bottom", "2.0",
    "--margin-left", "2.5", "--margin-right", "2.5",
]
r = subprocess.run(cmd, cwd=str(SCRIPTS), capture_output=True, text=True, encoding="utf-8", errors="replace")
OUT.write_text(
    "exit=%s\ndocx_exists=%s\nsize=%s\nstdout=%s\nstderr_tail=%s"
    % (r.returncode, DOCX.exists(), DOCX.stat().st_size if DOCX.exists() else 0,
       (r.stdout or "")[:800], (r.stderr or "")[-1500:]),
    encoding="utf-8",
)
print("ok")
