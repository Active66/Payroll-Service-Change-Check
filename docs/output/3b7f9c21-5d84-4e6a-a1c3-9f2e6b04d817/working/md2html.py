# -*- coding: utf-8 -*-
"""把测试方案 md 转成 doc-typeset 规格的 HTML（business-report 骨架）。"""
import io
import re
from pathlib import Path

SRC = Path(r"E:\code\PayrollServiceChangeCheck\docs\薪酬服务变更核对系统-测试方案_v1.0.md")
OUT = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\3b7f9c21-5d84-4e6a-a1c3-9f2e6b04d817\stage2\formatted-测试方案.html")

CSS = Path(r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\stage2\formatted-项目上线交付运行手册.html")

TOC = [
    "方案速览", "一、方案说明", "二、量化目标", "三、环境与数据",
    "四、正常场景（N，48 条）", "五、边界场景（B，42 条）", "六、对抗场景（A，36 条）",
    "七、权限场景（P，28 条）", "八、故障场景（F，32 条）", "九、执行计划", "十、准出判定",
    "附录 A 用例编号与写法", "附录 B 用例统计总表", "附录 C 自动化脚本清单", "附录 D 测试数据样本清单",
]

COVER_META = [
    ("文档编号", "PCC-QA-PLAN-001"),
    ("版本", "v1.0（评审版）"),
    ("适用系统", "薪酬服务变更核对系统（仓库代号 my_tool，命令入口 payroll-check）"),
    ("场景覆盖", "正常 / 边界 / 对抗 / 权限 / 故障，共 186 条用例"),
    ("读者", "QA、后端与前端开发、安全、SRE、上线评审"),
    ("编制日期", "2026 年 11 月 6 日"),
]


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def inline(s):
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r'<span class="mono">\1</span>', s)
    return s


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def build_table(rows):
    head = rows[0]
    body = rows[2:] if len(rows) > 2 else []
    out = ['<table class="tbl">', "  <thead>", "    <tr>"]
    for c in head:
        out.append("      <th>%s</th>" % inline(c))
    out.append("    </tr>")
    out.append("  </thead>")
    out.append("  <tbody>")
    for r in body:
        cells = r + [""] * (len(head) - len(r))
        out.append("    <tr>" + "".join("<td>%s</td>" % inline(c) for c in cells[: len(head)]) + "</tr>")
    out.append("  </tbody>")
    out.append("</table>")
    return "\n".join(out)


def main():
    lines = SRC.read_text(encoding="utf-8").splitlines()
    body = []
    i = 0
    n = len(lines)
    code_titles = []
    in_exec = False

    while i < n:
        raw = lines[i]
        line = raw.rstrip()

        # 跳过元信息行
        if line.startswith("**文档编号**") or line.startswith("**适用系统**") or line.startswith("**编制日期**"):
            i += 1
            continue

        # 一级标题（封面已处理）
        if line.startswith("# "):
            i += 1
            continue

        # 分隔线
        if line.strip() == "---":
            i += 1
            continue

        # 代码块
        if line.startswith("```"):
            i += 1
            block = []
            while i < n and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            first = next((b for b in block if b.strip() and not b.strip().startswith("#")), "")
            title = "命令" if not first else "命令与输出"
            code_titles.append(title)
            body.append('<table class="code">\n  <thead>\n    <tr><th>%s</th></tr>\n  </thead>\n  <tbody>\n    <tr><td>' % title)
            for b in block:
                cls = "code-cmt" if b.strip().startswith("#") else "code-line"
                txt = esc(b) if b.strip() else " "
                body.append('<p class="%s">%s</p>' % (cls, txt))
            body.append("</td></tr>\n  </tbody>\n</table>")
            continue

        # 表格
        if line.strip().startswith("|") and i + 1 < n and re.match(r"^\s*\|[\s:\-\|]+\|\s*$", lines[i + 1]):
            tbl = []
            while i < n and lines[i].strip().startswith("|"):
                tbl.append(split_row(lines[i]))
                i += 1
            body.append(build_table(tbl))
            continue

        # 标题
        if line.startswith("#### "):
            body.append("<h4>%s</h4>" % inline(line[5:].strip()))
            i += 1
            continue
        if line.startswith("### "):
            body.append("<h3>%s</h3>" % inline(line[4:].strip()))
            i += 1
            continue
        if line.startswith("## "):
            title = line[3:].strip()
            if in_exec:
                body.append("</div>")
                in_exec = False
            if title == "方案速览":
                body.append('<div class="executive-summary">')
                in_exec = True
            body.append("<h2>%s</h2>" % inline(title))
            i += 1
            continue

        # 引用
        if line.startswith("> "):
            body.append('<p class="note">&nbsp;&nbsp;%s</p>' % inline(line[2:].strip()))
            i += 1
            continue

        # 无序列表
        if re.match(r"^- ", line):
            items = []
            while i < n and re.match(r"^- ", lines[i]):
                items.append(lines[i][2:].strip())
                i += 1
            body.append("<ul>" + "".join("<li>%s</li>" % inline(x) for x in items) + "</ul>")
            continue

        # 有序列表
        if re.match(r"^\d+\. ", line):
            items = []
            while i < n and re.match(r"^\d+\. ", lines[i]):
                items.append(re.sub(r"^\d+\. ", "", lines[i]).strip())
                i += 1
            body.append("<ol>" + "".join("<li>%s</li>" % inline(x) for x in items) + "</ol>")
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 段落
        body.append("<p>%s</p>" % inline(line.strip()))
        i += 1

    if in_exec:
        body.append("</div>")

    # 取已验证的 CSS（复用运行手册的 style 块）
    css = re.search(r"<style>(.*?)</style>", CSS.read_text(encoding="utf-8"), re.S).group(1)
    css += """
span.mono { font-family: var(--ff-code); font-size: var(--fs-small); color: var(--color-code-text); }
"""

    cover_meta_rows = "\n".join(
        '    <tr><td class="cover-key">%s</td><td>%s</td></tr>' % (k, v) for k, v in COVER_META
    )
    toc = "\n".join('<p class="toc-ch">%s</p>' % t for t in TOC)

    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="docx-page-size" content="A4">
<title>薪酬服务变更核对系统 测试方案</title>
<style>%s</style>
</head>
<body>

<section role="cover">
  <p class="cover-category">测试与质量保障</p>
  <p class="cover-title">薪酬服务变更核对系统</p>
  <p class="cover-subtitle">测试方案</p>
  <p class="cover-desc">五类场景 · 186 条用例 · 九项覆盖率 · 八项一票否决</p>
  <table class="cover-meta">
    <thead><tr><th>项目</th><th>内容</th></tr></thead>
    <tbody>
%s
    </tbody>
  </table>
</section>

<section role="body" data-page-restart="1">

<p class="toc-title">目录</p>
%s

%s

</section>
</body>
</html>
""" % (css, cover_meta_rows, toc, "\n".join(body))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print("tables-parsed, code blocks:", len(code_titles))


main()
