import json
import subprocess
import sys

script = r"C:\Users\ROG\.workbuddy\plugins\cache\workbuddy-builtin\tencent-docx\5.5.6-wb.38337834.g5f969292.h4918b5ced607\skills\html-review\scripts\review_html.py"
html = r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\stage2\formatted-项目上线交付运行手册.html"
out = r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\working\review.json"

r = subprocess.run(
    [sys.executable, script, "--html", html, "--genre", "business-report"],
    capture_output=True,
    text=True,
    encoding="utf-8",
)
with open(out, "w", encoding="utf-8") as f:
    f.write("EXIT=%s\n" % r.returncode)
    f.write(r.stdout or "")
    f.write("\n---STDERR---\n")
    f.write(r.stderr or "")
print("done", r.returncode)
