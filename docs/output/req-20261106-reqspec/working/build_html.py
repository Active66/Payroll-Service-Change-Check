import io, os, re, html

BASE = r"E:\code\PayrollServiceChangeCheck\docs\output\req-20261106-reqspec"
SRC = os.path.join(BASE, "stage1", "final_draft.md")
OUT_DIR = os.path.join(BASE, "stage2")
os.makedirs(OUT_DIR, exist_ok=True)

lines = io.open(SRC, encoding="utf-8").read().split("\n")

# ---------- split intro block ----------
# intro: h1 title, 文档信息 table, 编制依据 paragraph, 证据性质声明 paragraph
i = 0
while i < len(lines) and not lines[i].strip().startswith("# "):
    i += 1
title_line = lines[i]
body_start = i + 1
# skip blank + table (5 lines) + blank + two paragraphs
j = body_start
tbl_start = None
while j < len(lines):
    if lines[j].strip().startswith("|"):
        tbl_start = j
        break
    j += 1
tbl_end = tbl_start
while tbl_end < len(lines) and lines[tbl_end].strip().startswith("|"):
    tbl_end += 1
k = tbl_end
paras = []
while k < len(lines) and len(paras) < 2:
    if lines[k].strip():
        paras.append(lines[k].strip())
    k += 1
rest = lines[k:]

info_rows = []
for ln in lines[tbl_start:tbl_end]:
    cells = [c.strip() for c in ln.strip().strip("|").split("|")]
    if set("".join(cells)) <= set("-: "):
        continue
    info_rows.append(cells)

# ---------- inline ----------
def inline(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r'<code class="mono">\1</code>', s)
    return s

# ---------- body blocks ----------
blocks = []
buf = []
mode = None

def flush_p():
    global buf
    if buf:
        txt = "".join(buf).strip()
        if txt:
            blocks.append("<p>%s</p>" % inline(txt))
        buf = []

h2_list = []
h2_idx = 0
n = 0
while n < len(rest):
    ln = rest[n]
    s = ln.strip()

    if not s:
        flush_p(); n += 1; continue

    if s.startswith("### "):
        flush_p()
        blocks.append("<h3>%s</h3>" % inline(s[4:]))
        n += 1; continue

    if s.startswith("## "):
        flush_p()
        h2_idx += 1
        t = s[3:]
        h2_list.append((h2_idx, t))
        blocks.append('<h2 id="section-%d">%s</h2>' % (h2_idx, inline(t)))
        n += 1; continue

    if s.startswith("|"):
        flush_p()
        trows = []
        while n < len(rest) and rest[n].strip().startswith("|"):
            trows.append(rest[n].strip())
            n += 1
        header = [c.strip() for c in trows[0].strip("|").split("|")]
        bodyrows = []
        for r in trows[1:]:
            cells = [c.strip() for c in r.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            bodyrows.append(cells)
        out = ['<table class="grid">', "<thead><tr>"]
        for c in header:
            out.append("<th>%s</th>" % inline(c))
        out.append("</tr></thead><tbody>")
        for r in bodyrows:
            out.append("<tr>")
            for c in r:
                out.append("<td>%s</td>" % inline(c))
            out.append("</tr>")
        out.append("</tbody></table>")
        blocks.append("".join(out))
        continue

    if s.startswith("```"):
        flush_p()
        n += 1
        code = []
        while n < len(rest) and not rest[n].strip().startswith("```"):
            code.append(rest[n].rstrip())
            n += 1
        n += 1
        rows = "".join('<p class="mono-line">%s</p>' % (html.escape(c) if c else "&nbsp;") for c in code)
        blocks.append('<table class="code-card"><tr><td>%s</td></tr></table>' % rows)
        continue

    if s == "---":
        flush_p(); n += 1; continue

    m = re.match(r"^(\d+)\.\s+(.*)$", s)
    if m:
        flush_p()
        items = []
        while n < len(rest):
            mm = re.match(r"^(\d+)\.\s+(.*)$", rest[n].strip())
            if not mm:
                break
            items.append("<li>%s</li>" % inline(mm.group(2)))
            n += 1
        blocks.append("<ol>%s</ol>" % "".join(items))
        continue

    if s.startswith("- "):
        flush_p()
        items = []
        while n < len(rest) and rest[n].strip().startswith("- "):
            items.append("<li>%s</li>" % inline(rest[n].strip()[2:]))
            n += 1
        blocks.append("<ul>%s</ul>" % "".join(items))
        continue

    buf.append(s + " ")
    n += 1

flush_p()

# ---------- cover ----------
cover_info = "".join(
    "<tr><td class='ci-k'>%s</td><td class='ci-v'>%s</td></tr>" % (html.escape(a), html.escape(b))
    for a, b in zip(info_rows[0], info_rows[1])
)

toc_items = "".join(
    '<li><a href="#section-%d">%s</a></li>' % (i, html.escape(t).replace("&amp;", "&"))
    for i, t in h2_list
)

decl = (
    '<table class="abstract-card"><tr><td>'
    "<p>%s</p>"
    "<p>%s</p>"
    "</td></tr></table>"
) % (inline(paras[0]), inline(paras[1]))

CSS = """
:root {
  --typography-fontFamily-heading: "微软雅黑", "Microsoft YaHei", "PingFang SC";
  --typography-fontFamily-body: "微软雅黑", "Microsoft YaHei", "PingFang SC";
  --typography-fontFamily-mono: "Consolas", "Courier New", monospace;
  --typography-fontSize-title: 24pt;
  --typography-fontSize-subtitle: 15pt;
  --typography-fontSize-h1: 18pt;
  --typography-fontSize-h2: 14pt;
  --typography-fontSize-h3: 11pt;
  --typography-fontSize-body: 10pt;
  --typography-fontSize-small: 9pt;
  --typography-fontSize-tiny: 8pt;
  --typography-lineHeight-body: 1.55;
  --typography-lineHeight-heading: 1.3;
  --typography-fontWeight-heading: 700;
  --typography-fontWeight-body: 400;
  --color-primary: #1565C0;
  --color-secondary: #0288D1;
  --color-accent: #00897B;
  --color-text: #212121;
  --color-muted: #616161;
  --color-heading: #123A63;
  --color-border: #D6DCE4;
  --color-bg: #FFFFFF;
  --color-highlight: #F4F7FB;
  --spacing-paragraph: 0.55em;
  --spacing-section: 1.2em;
  --spacing-block: 0.6em;
  --layout-margin: 2.4cm;

  --fs-h1: var(--typography-fontSize-h1);
  --fs-h2: var(--typography-fontSize-h2);
  --fs-h3: var(--typography-fontSize-h3);
  --fs-body: var(--typography-fontSize-body);
  --fs-small: var(--typography-fontSize-small);
  --fs-title: var(--typography-fontSize-title);
  --fs-subtitle: var(--typography-fontSize-subtitle);
  --ff-heading: var(--typography-fontFamily-heading);
  --ff-body: var(--typography-fontFamily-body);
  --ff-mono: var(--typography-fontFamily-mono);
  --lh-body: var(--typography-lineHeight-body);
  --lh-heading: var(--typography-lineHeight-heading);
  --fw-bold: var(--typography-fontWeight-heading);
  --fw-normal: var(--typography-fontWeight-body);
  --page-content-width: 15.6cm;
}

@page { @bottom-center { content: counter(page); } }
@page cover { @bottom-center { content: none; } }
section[role="cover"] { page: cover; }

body {
  max-width: var(--page-content-width);
  margin-left: auto; margin-right: auto;
  font-family: var(--ff-body);
  font-size: var(--fs-body);
  line-height: var(--lh-body);
  color: var(--color-text);
  background: var(--color-bg);
}

/* ---- cover ---- */
.cover-category
  { text-align: center; font-family: var(--ff-body); font-size: var(--fs-small);
    color: var(--color-primary); letter-spacing: 2pt; font-weight: var(--fw-bold);
    margin: 5.2em 0 1.4em 0; }
section[role="cover"] > h1
  { text-align: center; font-family: var(--ff-heading); font-size: var(--fs-title);
    color: var(--color-heading); font-weight: var(--fw-bold); line-height: var(--lh-heading);
    margin: 0 0 0.5em 0; }
.cover-subtitle
  { text-align: center; font-family: var(--ff-heading); font-size: var(--fs-subtitle);
    color: var(--color-primary); font-weight: var(--fw-bold); margin: 0 0 0.5em 0; }
.cover-subtitle2
  { text-align: center; font-family: var(--ff-body); font-size: var(--fs-body);
    color: var(--color-muted); margin: 0 0 4.5em 0; }
table.cover-info
  { width: 11.6cm; margin: 0 auto 2.4em auto; border-collapse: collapse; }
table.cover-info td
  { font-family: var(--ff-body); font-size: var(--fs-small); padding: 0.42em 0.6em;
    border-bottom: 0.5pt solid var(--color-border); }
td.ci-k { color: var(--color-muted); width: 3.1cm; text-align: left; }
td.ci-v { color: var(--color-text); text-align: left; font-weight: var(--fw-bold); }
.cover-foot
  { text-align: center; font-family: var(--ff-body); font-size: var(--fs-tiny);
    color: var(--color-muted); margin: 2.2em 0 0 0; }

/* ---- headings ---- */
h2 {
  font-family: var(--ff-heading); font-size: var(--fs-h2); color: var(--color-heading);
  font-weight: var(--fw-bold); line-height: var(--lh-heading);
  border-left: 4pt solid var(--color-primary);
  padding-left: 0.55em;
  margin: 1.5em 0 0.6em 0;
}
h3 {
  font-family: var(--ff-heading); font-size: var(--fs-h3); color: var(--color-primary);
  font-weight: var(--fw-bold); line-height: var(--lh-heading);
  margin: 1.05em 0 0.4em 0;
}

/* ---- text ---- */
p { font-family: var(--ff-body); font-size: var(--fs-body); color: var(--color-text);
    line-height: var(--lh-body); text-align: justify;
    margin: 0 0 var(--spacing-paragraph) 0; }
ol, ul { margin: 0.2em 0 0.7em 0; padding-left: 1.5em; }
li { font-family: var(--ff-body); font-size: var(--fs-body); color: var(--color-text);
     line-height: var(--lh-body); margin: 0 0 0.25em 0; }
strong { font-weight: var(--fw-bold); color: var(--color-heading); }
code.mono { font-family: var(--ff-mono); font-size: var(--fs-small); color: var(--color-accent); }

/* ---- toc ---- */
nav.doc-toc { margin: 0.6em 0 1.2em 0; }
p.toc-title { font-family: var(--ff-heading); font-size: var(--fs-h3); color: var(--color-heading);
              font-weight: var(--fw-bold); margin: 0 0 0.5em 0; }
ol.toc-list, ol.toc-list ol, ol.toc-list ul { list-style: none; list-style-type: none;
              padding-left: 0; margin: 0; }
ol.toc-list li { font-family: var(--ff-body); font-size: var(--fs-small);
                 color: var(--color-text); margin: 0 0 0.34em 0; }
ol.toc-list a { color: var(--color-text); text-decoration: none; }

/* ---- tables ---- */
table.grid { width: 100%; border-collapse: collapse; margin: 0.5em 0 0.9em 0; }
table.grid th {
  font-family: var(--ff-heading); font-size: var(--fs-small); font-weight: var(--fw-bold);
  color: var(--color-heading); background: var(--color-highlight);
  border: 0.5pt solid var(--color-border); padding: 0.35em 0.42em; text-align: left;
  line-height: var(--lh-heading);
}
table.grid td {
  font-family: var(--ff-body); font-size: var(--fs-small); color: var(--color-text);
  border: 0.5pt solid var(--color-border); padding: 0.32em 0.42em; text-align: left;
  line-height: 1.42; vertical-align: top;
}

/* ---- blocks ---- */
table.abstract-card { width: 100%; border-collapse: collapse; margin: 0 0 1em 0; }
table.abstract-card td {
  background: var(--color-highlight); border-left: 3pt solid var(--color-primary);
  border-top: 0.5pt solid var(--color-border); border-right: 0.5pt solid var(--color-border);
  border-bottom: 0.5pt solid var(--color-border);
  padding: 0.7em 0.85em; font-family: var(--ff-body); font-size: var(--fs-small);
  color: var(--color-text); line-height: 1.5;
}
table.abstract-card td p { font-size: var(--fs-small); text-align: justify; margin: 0 0 0.35em 0; }

table.code-card { width: 100%; border-collapse: collapse; margin: 0.4em 0 0.9em 0; }
table.code-card td {
  background: var(--color-highlight); border: 0.5pt solid var(--color-border);
  padding: 0.55em 0.7em;
}
p.mono-line { font-family: var(--ff-mono); font-size: var(--fs-tiny);
              color: var(--color-text); line-height: 1.35; margin: 0; text-align: left; }
"""

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="docx-page-size" content="A4">
<title>薪酬服务变更核对系统 · 需求分析方案</title>
<style>%s</style>
</head>
<body>

<section role="cover">
  <p class="cover-category">需求规格说明书</p>
  <h1>薪酬服务变更核对系统</h1>
  <p class="cover-subtitle">需求分析方案</p>
  <p class="cover-subtitle2">痛点诊断 · AI 改造与优先级 · 需求边界与规格 · 追溯矩阵 · 验收标准</p>
  <table class="cover-info">%s</table>
  <p class="cover-foot">上海外服（集团）有限公司 · 薪酬服务变更核对 · 合成演练场景</p>
</section>

<section role="body" data-page-restart="1">
%s
<nav class="doc-toc" aria-label="文档目录">
  <p class="toc-title">目录</p>
  <ol class="toc-list">%s</ol>
</nav>
%s
</section>

</body>
</html>
""" % (CSS, cover_info, decl, toc_items, "\n".join(blocks))

out_html = os.path.join(OUT_DIR, "formatted-需求分析方案.html")
io.open(out_html, "w", encoding="utf-8", newline="\n").write(HTML)
print("html:", out_html, len(HTML), "chars; h2:", len(h2_list), "blocks:", len(blocks), "tables:", HTML.count("<table"))
