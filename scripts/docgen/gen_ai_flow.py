# -*- coding: utf-8 -*-
"""生成《薪酬服务变更核对》AI 数智化后目标流程图为 SVG（供 Chrome 渲染为 PNG/JPG）"""
import io
import os

W, H = 1680, 2130

INK = "#101418"
SUB = "#5b6472"
MUTED = "#8b95a5"
LINE = "#e4e8ee"
CANVAS = "#fcfcfb"
FONT = "'Microsoft YaHei','PingFang SC','Segoe UI',sans-serif"
MONO = "Consolas,'Courier New',monospace"

CAT = {
    "auto":  dict(accent="#2563eb", tint="#eef4ff", label="AUTO"),
    "coop":  dict(accent="#b45309", tint="#fff7e8", label="AI+人"),
    "human": dict(accent="#0f766e", tint="#eefaf7", label="人工"),
    "audit": dict(accent="#475569", tint="#f1f5f9", label="留痕"),
}

p = []


def tw(s, size):
    """粗略文字宽度估算"""
    return sum(size * (1.0 if ord(c) > 127 else 0.56) for c in s)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def txt(x, y, s, size=13, fill=INK, weight=400, anchor="start", ls=0, font=FONT, op=1):
    extra = f' letter-spacing="{ls}"' if ls else ""
    extra += f' opacity="{op}"' if op != 1 else ""
    p.append(
        f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" fill="{fill}" '
        f'font-weight="{weight}" text-anchor="{anchor}"{extra}>{esc(s)}</text>'
    )


# ---------- 基础图元 ----------
def node(x, y, w, h, title, sub, cat, num):
    c = CAT[cat]
    p.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="#ffffff" stroke="{LINE}" stroke-width="1.2"/>')
    p.append(f'<circle cx="{x+30}" cy="{y+h/2}" r="13" fill="{c["tint"]}" stroke="{c["accent"]}" stroke-width="1.2" opacity="0.95"/>')
    txt(x + 30, y + h / 2 + 4, num, 11, c["accent"], 600, "middle", font=MONO)
    txt(x + 56, y + 25, title, 15.5, INK, 600)
    txt(x + 56, y + 46, sub, 12, SUB, 400)
    txt(x + w - 16, y + 24, c["label"], 9.5, c["accent"], 600, "end", ls=0.6)


def diamond(cx, cy, w, h, label, num):
    p.append(f'<path d="M{cx},{cy-h/2} L{cx+w/2},{cy} L{cx},{cy+h/2} L{cx-w/2},{cy} Z" fill="#fffbeb" stroke="#e0a94a" stroke-width="1.4"/>')
    txt(cx - tw(label, 13.5) / 2, cy + 5, label, 13.5, "#8a5209", 600)
    txt(cx - w / 2 + 12, cy - h / 2 + 15, num, 9.5, "#b4884a", 600, font=MONO)


def arrow(x1, y1, x2, y2, dash=False, color="#94a3b8"):
    d = ' stroke-dasharray="5 5"' if dash else ""
    mid = "ar" if not dash else "ard"
    p.append(f'<path d="M{x1},{y1} L{x2},{y2}" stroke="{color}" stroke-width="1.5" fill="none"{d} marker-end="url(#{mid})"/>')


def poly(points, dash=False, color="#94a3b8", head=True):
    d = "M" + " L".join(f"{x},{y}" for x, y in points)
    dash_attr = ' stroke-dasharray="5 5"' if dash else ""
    mid = ' marker-end="url(#ar)"' if head else ""
    if dash and head:
        mid = ' marker-end="url(#ard)"'
    p.append(f'<path d="{d}" stroke="{color}" stroke-width="1.5" fill="none"{dash_attr}{mid}/>')


def card(x, y, w, h, title, lines, accent="#b45309", tint="#fffdf7", dashed=True):
    da = ' stroke-dasharray="6 5"' if dashed else ""
    p.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{tint}" stroke="{accent}" stroke-width="1.1" opacity="1"{da}/>')
    p.append(f'<rect x="{x}" y="{y}" width="4" height="{h}" rx="2" fill="{accent}"/>')
    txt(x + 20, y + 26, title, 14, accent, 600)
    for i, ln in enumerate(lines):
        txt(x + 20, y + 52 + i * 21, ln, 12, SUB, 400)


def rail(x, y, code, name, note, accent):
    p.append(f'<rect x="{x}" y="{y}" width="248" height="68" rx="10" fill="#ffffff" stroke="{LINE}" stroke-width="1.1"/>')
    p.append(f'<rect x="{x}" y="{y}" width="4" height="68" rx="2" fill="{accent}"/>')
    txt(x + 18, y + 24, code, 10.5, MUTED, 600, ls=1, font=MONO)
    txt(x + 18, y + 44, name, 14.5, INK, 600)
    txt(x + 18, y + 60, note, 10.5, accent, 500)


# ---------- 画布 ----------
p.append(f'<rect width="{W}" height="{H}" fill="{CANVAS}"/>')
p.append('<defs>'
         '<linearGradient id="topbar" x1="0" y1="0" x2="1" y2="0">'
         '<stop offset="0" stop-color="#2563eb"/><stop offset="0.55" stop-color="#2563eb"/>'
         '<stop offset="0.56" stop-color="#0f766e"/><stop offset="1" stop-color="#0f766e"/></linearGradient>'
         '<marker id="ar" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">'
         '<path d="M0,0 L6,3 L0,6 z" fill="#94a3b8"/></marker>'
         '<marker id="ard" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">'
         '<path d="M0,0 L6,3 L0,6 z" fill="#c2740b"/></marker>'
         '</defs>')
p.append(f'<rect x="0" y="0" width="{W}" height="4" fill="url(#topbar)"/>')

# ---------- 页眉 ----------
txt(56, 66, "《薪酬服务变更核对》", 17, "#7c8598", 500, ls=1.5)
txt(56, 110, "AI 数智化后 · 目标流程图", 34, INK, 700, ls=0.5)
txt(56, 144, "系统承接确定性劳动，判断权与人工底线 0 处移交 ｜ 对照改造前 14 步手工流程 ｜ 依据 f01 / f14 / f22 / f24 / f31 / f33 / f34 / f42 / f44",
    12.5, SUB, 400)

# 右上统计
stats = [("8", "自动执行", "#2563eb"), ("5", "人机协同", "#b45309"),
         ("3", "必须人工", "#0f766e"), ("3", "阻断出口", "#b42318")]
bx = 1312
for i, (n, lab, col) in enumerate(stats):
    x = bx + i * 104
    txt(x, 82, n, 26, col, 700)
    txt(x, 100, lab, 11, MUTED, 400)
    if i:
        p.append(f'<line x1="{x-22}" y1="62" x2="{x-22}" y2="100" stroke="{LINE}" stroke-width="1"/>')

# 图例
leg = [("#2563eb", "自动执行（AI / 规则）"), ("#b45309", "人机协同（AI 给候选）"),
       ("#0f766e", "必须人工（判断权）"), ("#d97706", "判断点"), ("#b42318", "阻断 / 不出结果")]
lx = 56
for col, lab in leg:
    p.append(f'<circle cx="{lx+4}" cy="{182}" r="4.5" fill="{col}"/>')
    txt(lx + 15, 186, lab, 11.5, SUB, 400)
    lx += 15 + tw(lab, 11.5) + 26

p.append(f'<line x1="56" y1="208" x2="1624" y2="208" stroke="{LINE}" stroke-width="1"/>')

# ---------- 主列几何 ----------
NX, NW = 360, 470
CX = NX + NW / 2
RX = 900          # 右侧通道左边界
RW = 724          # 724 -> 1624

# ---------- P1 ----------
rail(56, 240, "P1 输入", "采集与归一化", "AI 承接 · 检索成本 ↓↓", "#2563eb")

node(NX, 240, NW, 62, "变更单只读接入", "邮件 / 台账 / 文件库三处来源，只读协议接入", "auto", "01")
node(NX, 336, NW, 62, "AI 三元组归一化", "客户编号 · 薪资周期 · 变更类型 → 内部唯一单号", "auto", "02")
node(NX, 432, NW, 62, "三源候选版本自动聚合", "同一张单的三处版本同屏，附来源路径与抓取时间", "auto", "03")
diamond(CX, 559, 350, 78, "是否在客户截止前？", "D1")

p.append(f'<line x1="{CX}" y1="302" x2="{CX}" y2="336" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
p.append(f'<line x1="{CX}" y1="398" x2="{CX}" y2="432" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
p.append(f'<line x1="{CX}" y1="494" x2="{CX}" y2="520" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')

# D1 -> 是 -> N4
txt(CX + 13, 616, "是", 11, "#0f766e", 600)
p.append(f'<line x1="{CX}" y1="598" x2="{CX}" y2="624" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')

# D1 -> 否 -> 右侧卡片
poly([(CX + 175, 559), (RX, 559)], color="#d97706")
txt(CX + 182, 550, "否", 11, "#b45309", 600)

card(RX, 500, RW, 132, "客户截止后补发 · 补发草稿",
     ["双时间戳并记：业务发生时间 / 系统接收时间",
      "occurred_missing 单列，不与「未确认」混用",
      "一线确认后才进入核对，不自动放行"],
     accent="#b45309", tint="#fffdf7")

node(NX, 624, NW, 62, "锁定快照 · 登记交接", "交接三要素：谁、何时、交了什么；接收人签收", "coop", "04")

# ---------- P2 ----------
rail(56, 720, "P2 校验", "确定性规则校验", "规则承接 · 校验成本 ↓↓", "#2563eb")

node(NX, 720, NW, 62, "四类断言自动执行", "重复编号 · 空时间 · 版本断链 · 单位差异", "auto", "05")
diamond(CX, 847, 350, 78, "四类断言全部通过？", "D2")

p.append(f'<line x1="{CX}" y1="686" x2="{CX}" y2="720" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
p.append(f'<line x1="{CX}" y1="782" x2="{CX}" y2="808" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')

txt(CX + 13, 900, "是", 11, "#0f766e", 600)
p.append(f'<line x1="{CX}" y1="886" x2="{CX}" y2="912" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
poly([(CX + 175, 847), (RX, 847)], color="#d97706")
txt(CX + 182, 838, "否", 11, "#b45309", 600)

card(RX, 790, RW, 118, "缺项清单一页出",
     ["缺什么 · 在哪 · 原始行定位，不再回头翻三处",
      "一线补录后重跑断言，回退只回到 05"],
     accent="#b45309", tint="#fffdf7")

node(NX, 912, NW, 62, "校验报告随单留痕", "通过与未通过项全记录，不覆盖原值", "audit", "06")

# ---------- P3 ----------
rail(56, 1008, "P3 判定", "版本与差异", "人机协同 · 判断权保留", "#0f766e")

node(NX, 1008, NW, 62, "版本候选智能排序", "仅按 有效期 + 确认状态 排序，不输出结论", "coop", "07")
diamond(CX, 1135, 350, 78, "版本可确认？", "D3")
p.append(f'<line x1="{CX}" y1="974" x2="{CX}" y2="1008" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
p.append(f'<line x1="{CX}" y1="1070" x2="{CX}" y2="1096" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
txt(CX + 13, 1188, "是", 11, "#0f766e", 600)
p.append(f'<line x1="{CX}" y1="1174" x2="{CX}" y2="1200" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')

node(NX, 1200, NW, 62, "金额差异逐行溯源", "回溯到原始行，生成解释候选，不替换原值", "coop", "08")
diamond(CX, 1327, 350, 78, "差异可解释？", "D4")
p.append(f'<line x1="{CX}" y1="1262" x2="{CX}" y2="1288" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
txt(CX + 13, 1380, "是", 11, "#0f766e", 600)
p.append(f'<line x1="{CX}" y1="1366" x2="{CX}" y2="1392" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')

# ---------- 右侧人工队列 ----------
QX, QY, QW, QH = RX, 1080, RW, 360
QC = "#b42318"
p.append(f'<rect x="{QX}" y="{QY}" width="{QW}" height="{QH}" rx="12" fill="#fffafa" stroke="{QC}" stroke-width="1.1" stroke-dasharray="6 5"/>')
p.append(f'<rect x="{QX}" y="{QY}" width="4" height="{QH}" rx="2" fill="{QC}"/>')
txt(QX + 20, QY + 26, "人工核对队列（悬停态，非回退）", 14, QC, 600)
for i, ln in enumerate(["接收：版本不可确认 · 差异不可解释 · 授权不明",
                        "规则：禁止默认通过，队列内没有自动放行路径",
                        "决策通过后回到 09 授权校验，重新过一遍规则",
                        "回填：判断依据 · 判断人 · 时间，一并进审计链"]):
    txt(QX + 20, QY + 52 + i * 21, ln, 12, SUB, 400)
p.append(f'<line x1="{QX+20}" y1="{QY+140}" x2="{QX+QW-20}" y2="{QY+140}" stroke="#f1d6d4" stroke-width="1"/>')
txt(QX + 20, QY + 164, "队列观察指标（试点单独盯，不能只看总工时）", 11, QC, 600)
mets = [("队列长度", "超上限即触发重评"), ("等待时长", "按天数计，非工时"), ("返工次数", "同一单重复进入")]
for i, (k, v) in enumerate(mets):
    mx = QX + 20 + i * 240
    p.append(f'<rect x="{mx}" y="{QY+176}" width="210" height="42" rx="8" fill="#ffffff" stroke="#f1d6d4" stroke-width="1"/>')
    txt(mx + 14, QY + 194, k, 11, INK, 600)
    txt(mx + 14, QY + 210, v, 10.5, MUTED, 400)
p.append(f'<line x1="{QX+20}" y1="{QY+240}" x2="{QX+QW-20}" y2="{QY+240}" stroke="#f1d6d4" stroke-width="1"/>')
txt(QX + 20, QY + 264, "队列是成本黑洞：单张截止后补发单可能耗掉半天，积压会直接击穿试点预算", 11.5, SUB, 400)
txt(QX + 20, QY + 288, "因此试点不只看总工时，要同时盯队列长度与返工率（f51），否则会误判效果", 11.5, SUB, 400)
txt(QX + 20, QY + 312, "边界：队列内无授权人不得做出对外承诺，判断依据必须回填进审计链", 11.5, SUB, 400)

poly([(CX + 175, 1135), (RX, 1135)], color="#d97706")
txt(CX + 182, 1126, "否", 11, "#b45309", 600)
poly([(CX + 175, 1327), (RX, 1327)], color="#d97706")
txt(CX + 182, 1318, "否", 11, "#b45309", 600)

# 队列回流（对齐 09 授权校验节点垂直中心）
poly([(RX, 1423), (830, 1423)], dash=True, color="#c2740b")
txt(865, 1415, "决策通过", 9.5, "#b45309", 500, "middle")

# ---------- P4 ----------
rail(56, 1392, "P4 授权", "授权与两级确认", "必须人工 · 等待成本为主", "#0f766e")

node(NX, 1392, NW, 62, "授权策略自动校验", "5 条越权规则：非授权人 / 超边界 / 重复确认…", "auto", "09")
node(NX, 1488, NW, 62, "双人核对（底线）", "两人独立比对，系统只并排展示，不代签不代判", "human", "10")
node(NX, 1584, NW, 62, "正式确认", "业务负责人在其授权范围内签字确认", "human", "11")
node(NX, 1680, NW, 62, "对外口径确认", "用途与解释由客户协同确认，不推测客户意图", "human", "12")
for y0, y1 in [(1454, 1488), (1550, 1584), (1646, 1680)]:
    p.append(f'<line x1="{CX}" y1="{y0}" x2="{CX}" y2="{y1}" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')

# ---------- P5 ----------
rail(56, 1776, "P5 输出", "出具与留痕", "系统承接 · 零人工", "#475569")

node(NX, 1776, NW, 62, "出具结果", "只读输出，不回写客户侧任何系统", "auto", "13")
node(NX, 1872, NW, 62, "哈希链审计归档", "每次变更生成 SHA-256 链，可回溯到原始行", "audit", "14")
p.append(f'<line x1="{CX}" y1="1742" x2="{CX}" y2="1776" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')
p.append(f'<line x1="{CX}" y1="1838" x2="{CX}" y2="1872" stroke="#94a3b8" stroke-width="1.5" marker-end="url(#ar)"/>')

# 右侧收益说明（浮于 P4/P5 右侧空白）
p.append(f'<rect x="{RX}" y="1470" width="{RW}" height="292" rx="12" fill="#ffffff" stroke="{LINE}" stroke-width="1.1"/>')
txt(RX + 24, 1504, "AI 数智化到底改了什么", 14, INK, 600)
rows = [
    ("承接", "跨三源取证、四类断言、版本排序、差异溯源", "#2563eb"),
    ("保留", "版本可确认 · 差异可解释 · 双人核对 · 两级确认", "#0f766e"),
    ("不变", "只读边界 · 双时间戳 · 不覆盖原值 · 无自动审批", "#b45309"),
]
for i, (k, v, c) in enumerate(rows):
    yy = 1542 + i * 34
    p.append(f'<rect x="{RX+24}" y="{yy-14}" width="46" height="20" rx="5" fill="{c}" opacity="0.10"/>')
    txt(RX + 47, yy, k, 11, c, 600, "middle")
    txt(RX + 82, yy, v, 11.5, SUB, 400)
p.append(f'<line x1="{RX+24}" y1="1632" x2="{RX+RW-24}" y2="1632" stroke="{LINE}" stroke-width="1"/>')
txt(RX + 24, 1660, "对照改造前：4 步检索/比对由系统承担，4 处判断权 0 处移交；", 11.5, MUTED, 400)
txt(RX + 24, 1682, "减少的是工时与返工，不是核对本身。等待时间要靠排队治理，系统省不出来。", 11.5, MUTED, 400)
txt(RX + 24, 1704, "所以试点验收看返工率与周期，而不是看「上线了几个功能」。", 11.5, MUTED, 400)

# ---------- 底部约束带 ----------
txt(56, 1966, "贯穿全链的五条硬约束（不随自动化程度变化）", 12.5, INK, 600)
cons = [
    ("只读边界", "不回写客户侧任何系统", "f32", "#2563eb"),
    ("建议不覆盖原值", "AI 结论只作候选", "f14", "#b45309"),
    ("双时间戳并记", "业务发生 + 系统接收", "f34", "#475569"),
    ("occurred_missing", "与「未确认」严格分列", "f31", "#b42318"),
    ("无自动审批", "双人核对不削弱", "f01 / f22", "#0f766e"),
]
cw, gap = 304, 12
for i, (t, s, ref, c) in enumerate(cons):
    x = 56 + i * (cw + gap)
    p.append(f'<rect x="{x}" y="1982" width="{cw}" height="66" rx="10" fill="#ffffff" stroke="{LINE}" stroke-width="1.1"/>')
    p.append(f'<rect x="{x}" y="1982" width="4" height="66" rx="2" fill="{c}"/>')
    txt(x + 18, 2006, t, 12.5, INK, 600)
    txt(x + 18, 2026, s, 11, SUB, 400)
    txt(x + cw - 16, 2026, ref, 9.5, MUTED, 500, "end", font=MONO)

# ---------- 页脚 ----------
p.append(f'<line x1="56" y1="2078" x2="1624" y2="2078" stroke="{LINE}" stroke-width="1"/>')
txt(56, 2104, "《薪酬服务变更核对》 · AI 数智化后目标流程图", 11, MUTED, 400)
txt(1624, 2104, "依据 discovery-report 访谈事实 ｜ 2026-10-23", 11, MUTED, 400, "end")

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
       f'viewBox="0 0 {W} {H}" font-family="{FONT}">' + "".join(p) + "</svg>")

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
out_dir = os.path.join(repo_root, "docs", "assets")
os.makedirs(out_dir, exist_ok=True)
svg_path = os.path.join(out_dir, "ai_flow.svg")
html_path = os.path.join(out_dir, "ai_flow.html")

with io.open(svg_path, "w", encoding="utf-8") as f:
    f.write(svg)

html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>html,body{{margin:0;padding:0;background:{CANVAS};}}svg{{display:block;}}</style>
</head><body>{svg}</body></html>"""
with io.open(html_path, "w", encoding="utf-8") as f:
    f.write(html)

print("SVG:", svg_path, len(svg), "chars")
print("HTML:", html_path)
