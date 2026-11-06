# -*- coding: utf-8 -*-
"""按 html-review 的 actionable_feedback 对 HTML 做一次定向修正。"""
import io
import re

SRC = r"E:\code\PayrollServiceChangeCheck\docs\output\8f3c1d2e-7a45-4b91-9c6e-2d5a10b7e4c8\stage2\formatted-项目上线交付运行手册.html"

NEW_STYLE = """
@page { @bottom-center { content: counter(page); } }
@page cover { @bottom-center { content: none; } }
section[role="cover"] { page: cover; }

:root {
  --fs-cover-title: 26pt;
  --fs-cover-sub: 16pt;
  --fs-cover-cat: 11pt;
  --fs-cover-desc: 10pt;
  --fs-chapter: 17pt;
  --fs-section: 12pt;
  --fs-sub: 10.5pt;
  --fs-body: 10pt;
  --fs-small: 8.5pt;
  --fs-tiny: 7.5pt;
  --fs-toc: 9.5pt;
  --fs-note: 9.5pt;
  --fs-code: 8.5pt;
  --fs-num: 16pt;
  --fs-review: 13pt;
  --lh-body: 1.7;
  --lh-tight: 1.45;
  --lh-code: 1.5;
  --lh-toc: 1.9;
  --sp-cover-top: 3cm;
  --sp-h1-top: 1.5em;
  --sp-h1-bottom: 0.5em;
  --sp-h1-pad: 4pt;
  --sp-h2-top: 1.1em;
  --sp-h2-bottom: 0.4em;
  --sp-h3-top: 0.9em;
  --sp-h3-bottom: 0.3em;
  --sp-p: 0.55em;
  --sp-li: 0.25em;
  --sp-list-pad: 1.5em;
  --sp-table: 0.4em;
  --sp-table-bottom: 0.9em;
  --sp-cell: 4pt 5pt;
  --sp-code-cell: 6pt 8pt;
  --sp-note-pad: 6pt 9pt;
  --sp-sum-cell: 7pt 9pt;
  --sp-meta-cell: 5pt 4pt;
  --sp-cover-gap: 2.2em;
  --sp-toc-t: 0.6em;
  --color-primary: #1565C0;
  --color-heading: #1A237E;
  --color-text: #212121;
  --color-muted: #616161;
  --color-border: #D9DEE5;
  --color-bg: #FFFFFF;
  --color-surface: #F2F6FA;
  --color-code-bg: #F7F9FB;
  --color-code-text: #263238;
  --color-code-cmt: #6A737D;
  --color-warn: #C62828;
  --color-warn-bg: #FBF3F3;
  --ff-heading: "微软雅黑", "Microsoft YaHei", sans-serif;
  --ff-body: "微软雅黑", "Microsoft YaHei", sans-serif;
  --ff-code: Consolas, "Courier New", monospace;
  --page-content-width: 16cm;
}

body { font-family: var(--ff-body); font-size: var(--fs-body); line-height: var(--lh-body); color: var(--color-text); background: var(--color-bg); max-width: var(--page-content-width); margin-left: auto; margin-right: auto; }
p { margin: 0 0 var(--sp-p) 0; text-align: left; }

h1 { font-family: var(--ff-heading); font-size: var(--fs-cover-title); font-weight: 700; color: var(--color-heading); text-align: left; margin: 0 0 var(--sp-h1-bottom) 0; border-bottom: none; padding-bottom: 0; }
h2 { font-family: var(--ff-heading); font-size: var(--fs-chapter); font-weight: 700; color: var(--color-heading); text-align: left; margin: var(--sp-h1-top) 0 var(--sp-h1-bottom) 0; padding-bottom: var(--sp-h1-pad); border-bottom: 1.2pt solid var(--color-primary); }
h3 { font-family: var(--ff-heading); font-size: var(--fs-section); font-weight: 700; color: var(--color-primary); text-align: left; margin: var(--sp-h2-top) 0 var(--sp-h2-bottom) 0; }
h4 { font-family: var(--ff-heading); font-size: var(--fs-sub); font-weight: 700; color: var(--color-text); text-align: left; margin: var(--sp-h3-top) 0 var(--sp-h3-bottom) 0; }
strong { font-weight: 700; color: var(--color-heading); }
ul, ol { margin: 0 0 var(--sp-p) 0; padding-left: var(--sp-list-pad); }
li { margin-bottom: var(--sp-li); line-height: var(--lh-tight); }

section[role="cover"] { padding-top: var(--sp-cover-top); }
p.cover-category { font-family: var(--ff-heading); font-size: var(--fs-cover-cat); font-weight: 700; color: var(--color-primary); letter-spacing: 0.12em; text-align: left; margin-bottom: var(--sp-cover-gap); }
p.cover-title { font-family: var(--ff-heading); font-size: var(--fs-cover-title); font-weight: 700; color: var(--color-heading); text-align: left; margin: 0 0 var(--sp-h1-bottom) 0; }
p.cover-subtitle { font-family: var(--ff-heading); font-size: var(--fs-cover-sub); font-weight: 700; color: var(--color-primary); text-align: left; margin: 0 0 var(--sp-toc-t) 0; }
p.cover-desc { font-size: var(--fs-cover-desc); color: var(--color-muted); text-align: left; margin-bottom: var(--sp-cover-gap); }
table.cover-meta { width: 13cm; border-collapse: collapse; margin-top: var(--sp-toc-t); }
table.cover-meta th { border-top: 0.5pt solid var(--color-border); padding: var(--sp-meta-cell); font-size: var(--fs-small); color: var(--color-muted); text-align: left; font-weight: 400; width: 3.6cm; }
table.cover-meta td { border-top: 0.5pt solid var(--color-border); padding: var(--sp-meta-cell); font-size: var(--fs-small); text-align: left; line-height: var(--lh-tight); }

p.toc-title { font-family: var(--ff-heading); font-size: var(--fs-review); font-weight: 700; color: var(--color-heading); text-align: left; margin: 0 0 var(--sp-toc-t) 0; }
p.toc-ch { font-size: var(--fs-toc); line-height: var(--lh-toc); margin: 0; color: var(--color-text); }
span.toc-sub { color: var(--color-muted); font-size: var(--fs-small); }

div.executive-summary { border-left: 3pt solid var(--color-primary); background: var(--color-surface); padding: var(--sp-note-pad); margin: var(--sp-table) 0 var(--sp-table-bottom) 0; }
table.summary { width: 100%; border-collapse: collapse; margin-top: var(--sp-toc-t); }
table.summary th { background: var(--color-bg); color: var(--color-muted); font-size: var(--fs-small); font-weight: 400; border: 0.5pt solid var(--color-border); padding: var(--sp-sum-cell); text-align: left; }
table.summary td { border: 0.5pt solid var(--color-border); padding: var(--sp-sum-cell); font-size: var(--fs-note); line-height: var(--lh-tight); text-align: left; vertical-align: top; }
table.summary td.sum-v { font-family: var(--ff-heading); font-size: var(--fs-num); font-weight: 700; color: var(--color-primary); }

table.tbl { width: 100%; border-collapse: collapse; margin: var(--sp-table) 0 var(--sp-table-bottom) 0; font-size: var(--fs-small); }
table.tbl th { background: var(--color-surface); color: var(--color-heading); font-weight: 700; font-size: var(--fs-small); border: 0.5pt solid var(--color-border); padding: var(--sp-cell); text-align: left; line-height: var(--lh-tight); }
table.tbl td { border: 0.5pt solid var(--color-border); padding: var(--sp-cell); vertical-align: top; line-height: var(--lh-tight); text-align: left; }
table.tbl td.k { font-weight: 700; color: var(--color-heading); }

table.code { width: 100%; border-collapse: collapse; margin: var(--sp-table) 0 var(--sp-table-bottom) 0; }
table.code th { background: var(--color-surface); color: var(--color-muted); font-size: var(--fs-tiny); font-weight: 400; border: 0.5pt solid var(--color-border); padding: var(--sp-cell); text-align: left; }
table.code td { border: 0.5pt solid var(--color-border); padding: var(--sp-code-cell); background: var(--color-code-bg); }
p.code-line { font-family: var(--ff-code); font-size: var(--fs-code); line-height: var(--lh-code); color: var(--color-code-text); margin: 0; text-align: left; }
p.code-cmt { font-family: var(--ff-code); font-size: var(--fs-code); line-height: var(--lh-code); color: var(--color-code-cmt); margin: 0; text-align: left; }

p.note { border-left: 3pt solid var(--color-primary); background: var(--color-surface); padding: var(--sp-note-pad); margin: var(--sp-table) 0 var(--sp-table-bottom) 0; font-size: var(--fs-note); line-height: var(--lh-tight); }
p.warn { border-left: 3pt solid var(--color-warn); background: var(--color-warn-bg); padding: var(--sp-note-pad); margin: var(--sp-table) 0 var(--sp-table-bottom) 0; font-size: var(--fs-note); line-height: var(--lh-tight); }
p.plain { font-size: var(--fs-small); color: var(--color-muted); }
"""

INTRO = {
    "一、文档说明": "本章交代手册的用途、读者与术语口径，是后面所有操作步骤的前提。",
    "二、系统交付概述": "先建立整体印象：交给运维的是什么、它不做什么、当前哪些能力还没补齐。",
    "三、环境与部署": "从零把系统跑起来。按顺序执行，每步都有可验证的产出。",
    "四、系统初始化配置": "系统起来之后，把制度、参数、账号与对外凭证配到位。",
    "五、日常运行": "每天、每周要做什么，以及制度换版、索引重建、备份这些高频动作的标准做法。",
    "六、监控与告警": "盯哪些数、阈值是多少、超了找谁。技术指标与业务质量指标要一起看。",
    "七、故障处理": "按现象定位、按错误码处置。先恢复服务，再找根因。",
    "八、应急响应": "五类最坏情况的预案，以及六条不可商量的停止条件。",
    "九、安全与合规运行要求": "权限、脱敏、密钥、审计、留存五件事的硬要求。",
    "十、变更与发布管理": "什么时间能发、什么改动风险最高、评审要回答哪些问题。",
    "十一、值班与支持": "谁负责什么、出事找谁、承诺多久恢复。",
    "附录 A 常用命令速查": "日常操作最常用的命令，按用途分组，可直接复制。",
    "附录 D 上线检查签字页": "上线评审时逐项打勾并签字。未通过的阻塞项必须写明补偿措施。",
    "附录 E 巡检记录表模板": "每日巡检的记录格式，建议按周归档。",
}

CODE_TITLES = [
    "分层结构",
    "目录结构",
    "部署命令",
    "账号命令",
    "初始化命令",
    "组织命令",
    "Key 命令",
    "备份与恢复",
    "查询语句",
    "排障步骤",
    "升级命令",
    "扫描命令",
    "命令速查",
]


def main():
    with io.open(SRC, "r", encoding="utf-8") as f:
        html = f.read()

    # 1. 替换 style 块
    html = re.sub(r"<style>.*?</style>", "<style>\n" + NEW_STYLE + "\n</style>", html, count=1, flags=re.S)

    # 2. 封面标题 h1 -> p（保证全文只有一个 h1 在正文之外）
    html = html.replace(
        '<h1 class="cover-title">薪酬服务变更核对系统</h1>',
        '<p class="cover-title">薪酬服务变更核对系统</p>',
    )

    # 3. 封面元信息表加 thead
    html = html.replace(
        '<table class="cover-meta">\n    <tr><td class="cover-key">文档编号</td>',
        '<table class="cover-meta">\n    <thead><tr><th>项目</th><th>内容</th></tr></thead>\n    <tbody>\n    <tr><td class="cover-key">文档编号</td>',
    )
    html = html.replace(
        "    <tr><td class=\"cover-key\">编制日期</td><td>2026 年 11 月 6 日</td></tr>\n  </table>",
        "    <tr><td class=\"cover-key\">编制日期</td><td>2026 年 11 月 6 日</td></tr>\n    </tbody>\n  </table>",
    )

    # 4. 摘要区重构为 executive-summary
    old_sum = re.search(r"<h2>交付摘要</h2>.*?</table>", html, re.S)
    new_sum = """<div class="executive-summary">
<h2>交付摘要</h2>
<p>本手册面向接手的运维与业务运营，只讲操作：命令、路径、阈值、判定条件、处置动作、责任人。手册不重复业务价值与技术选型理由，那些在项目方案与设计说明中。</p>
<table class="summary">
  <thead>
    <tr><th>上线阻塞缺口</th><th>上线检查项</th><th>每日巡检</th><th>回滚目标</th></tr>
  </thead>
  <tbody>
    <tr><td class="sum-v">6 项</td><td class="sum-v">25 项</td><td class="sum-v">10 分钟</td><td class="sum-v">15 分钟</td></tr>
    <tr><td colspan="4">本期交付的是可运行的单实例系统：只读接入、双时间戳、版本有效期与确认状态双条件判定、全链路留痕。系统不写回业务系统、不自动审批、不编造状态、不对外发消息——这四条边界不是配置项，不能靠调参绕过。</td></tr>
  </tbody>
</table>
</div>"""
    html = html[: old_sum.start()] + new_sum + html[old_sum.end():]

    # 5. 普通表格补 thead/tbody
    def fix_tbl(m):
        inner = m.group(1)
        rows = re.findall(r"<tr>.*?</tr>", inner, re.S)
        if not rows:
            return m.group(0)
        head = rows[0]
        body = "".join(rows[1:])
        return (
            '<table class="tbl">\n  <thead>\n    '
            + head
            + "\n  </thead>\n  <tbody>\n    "
            + "\n    ".join(rows[1:])
            + "\n  </tbody>\n</table>"
        )

    html = re.sub(r'<table class="tbl">(.*?)</table>', fix_tbl, html, flags=re.S)

    # 6. 代码块补 thead/tbody（按顺序命名）
    counter = {"i": 0}

    def fix_code(m):
        idx = counter["i"]
        counter["i"] += 1
        title = CODE_TITLES[idx] if idx < len(CODE_TITLES) else "示例"
        inner = m.group(1)
        return (
            '<table class="code">\n  <thead>\n    <tr><th>'
            + title
            + "</th></tr>\n  </thead>\n  <tbody>\n    <tr><td>"
            + inner
            + "</td></tr>\n  </tbody>\n</table>"
        )

    html = re.sub(r'<table class="code"><tr><td>(.*?)</td></tr></table>', fix_code, html, flags=re.S)

    # 7. 章标题后补导语
    for title, text in INTRO.items():
        old = "<h1>%s</h1>" % title
        if old in html:
            html = html.replace(old, old + "\n<p>" + text + "</p>")

    # 8. 小节标题后补导语（避免 h2 直接接 h3）
    for old, text in [
        ("<h2>7.3 常见故障场景</h2>", "以下是试点期最可能遇到的八类故障，按发生概率排序。每类都给出定位顺序与处置动作。"),
        ("<h2>8.2 五类场景预案</h2>", "预案不是流程文件，是当场照着做的东西。每一条都以“先做什么”开头。"),
    ]:
        if old in html:
            html = html.replace(old, old + "\n<p>" + text + "</p>")

    # 9. 正文标题整体下移一级（h1->h2, h2->h3, h3->h4），仅限 body section
    idx = html.find('<section role="body"')
    head, body = html[:idx], html[idx:]
    body = re.sub(
        r"<(/?)h([1-3])>",
        lambda m: "<%sh%d>" % (m.group(1), int(m.group(2)) + 1),
        body,
    )
    html = head + body

    with io.open(SRC, "w", encoding="utf-8") as f:
        f.write(html)

    print("code blocks:", counter["i"])


main()
