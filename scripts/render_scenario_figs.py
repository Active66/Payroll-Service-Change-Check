# -*- coding: utf-8 -*-
"""把 scenario-traces.json 画成 4 张图（PNG），供测试过程文档嵌入。

为什么用 PIL 手画而不是 matplotlib：
  · 这些图是"流程卡 + 徽标"的信息图，不是数据图表，PIL 的矩形/圆角/箭头足够，
    且能精确控制中文换行与对齐；
  · matplotlib 默认中文字体要额外配置，反而更容易出豆腐块。

产出（docs/assets/）：
  fig-overview.png   三场景对照
  fig-scenario-a.png 正常调薪：拦截点与落库
  fig-scenario-b.png 信息缺失：两轮对话
  fig-scenario-c.png 降薪阻断：工具集收窄

用法：python scripts/render_scenario_figs.py
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
DATA = ASSETS / "scenario-traces.json"

FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_B = "C:/Windows/Fonts/msyhbd.ttc"

INK = (31, 41, 55)
MUTED = (107, 114, 128)
LINE = (226, 232, 240)
BG = (255, 255, 255)
BLUE = (37, 99, 235)
BLUE_SOFT = (239, 246, 255)
GREEN = (5, 150, 105)
GREEN_SOFT = (236, 253, 245)
AMBER = (180, 83, 9)
AMBER_SOFT = (255, 251, 235)
RED = (185, 28, 28)
RED_SOFT = (254, 242, 242)
GREY_SOFT = (248, 250, 252)


def f(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_B if bold else FONT, size)


def text(d: ImageDraw.ImageDraw, xy, s: str, size=22, color=INK, bold=False, anchor="la"):
    d.text(xy, s, font=f(size, bold), fill=color, anchor=anchor)


def wrap(d: ImageDraw.ImageDraw, s: str, size: int, width: int, bold=False) -> list[str]:
    """按像素宽度折行。中文按字符切，英文按空格切——够用且不会把词切碎。"""
    font = f(size, bold)
    lines: list[str] = []
    # 显式换行先切开：PIL 的 textlength 不接受含 \n 的字符串，
    # 而卡片副标题就是"工具名\n说明"这种两行结构
    for para in str(s).split("\n"):
        cur = ""
        for token in _tokens(para):
            trial = cur + token
            if d.textlength(trial, font=font) <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = token.lstrip()
        if cur:
            lines.append(cur)
    return lines


def _tokens(s: str) -> list[str]:
    out, buf = [], ""
    for ch in s:
        if ch == " ":
            if buf:
                out.append(buf + " ")
                buf = ""
            else:
                out.append(" ")
        else:
            buf += ch
    if buf:
        out.append(buf)
    return out


def card(d: ImageDraw.ImageDraw, box, title: str, sub: str, tag: str,
         color=BLUE, soft=BLUE_SOFT, tag_color=None) -> None:
    """一张流程卡：左侧色条 + 工具名 + 一行结果 + 右上角状态徽标。"""
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=14, fill=soft, outline=color, width=2)
    d.rounded_rectangle((x0, y0, x0 + 6, y1), radius=3, fill=color)
    pad = 20
    lines = wrap(d, title, 26, x1 - x0 - pad * 2 - 8, bold=True)
    yy = y0 + pad
    for ln in lines[:2]:
        text(d, (x0 + pad, yy), ln, 26, INK, bold=True)
        yy += 34
    if sub:
        for ln in wrap(d, sub, 20, x1 - x0 - pad * 2 - 8)[:3]:
            text(d, (x0 + pad, yy + 2), ln, 20, MUTED)
            yy += 28
    if tag:
        tw = d.textlength(tag, font=f(18, True))
        tx0 = x1 - tw - 26
        ty0 = y1 - 42
        d.rounded_rectangle((tx0, ty0, x1 - 12, y1 - 12), radius=10,
                            fill=BG, outline=tag_color or color, width=1)
        text(d, (tx0 + 7, ty0 + 5), tag, 18, tag_color or color, bold=True)


def arrow(d: ImageDraw.ImageDraw, p0, p1, color=MUTED, width=3, dash=False) -> None:
    (x0, y0), (x1, y1) = p0, p1
    if dash:
        _dashed(d, p0, p1, color, width)
    else:
        d.line([p0, p1], fill=color, width=width)
    import math
    ang = math.atan2(y1 - y0, x1 - x0)
    L, W = 13, 7
    d.polygon([(x1, y1),
               (x1 - L * math.cos(ang) + W * math.sin(ang),
                y1 - L * math.sin(ang) - W * math.cos(ang)),
               (x1 - L * math.cos(ang) - W * math.sin(ang),
                y1 - L * math.sin(ang) + W * math.cos(ang))], fill=color)


def _dashed(d, p0, p1, color, width, seg=12, gap=9):
    import math
    x0, y0 = p0
    x1, y1 = p1
    total = math.hypot(x1 - x0, y1 - y0)
    if total <= 0:
        return
    ux, uy = (x1 - x0) / total, (y1 - y0) / total
    t = 0.0
    while t < total:
        e = min(t + seg, total)
        d.line([(x0 + ux * t, y0 + uy * t), (x0 + ux * e, y0 + uy * e)],
               fill=color, width=width)
        t = e + gap


def chip(d: ImageDraw.ImageDraw, xy, label: str, color=BLUE, soft=BLUE_SOFT) -> int:
    """小圆角标签，返回宽度。"""
    w = d.textlength(label, font=f(19, True)) + 24
    d.rounded_rectangle((xy[0], xy[1], xy[0] + w, xy[1] + 34), radius=8,
                        fill=soft, outline=color, width=1)
    text(d, (xy[0] + 12, xy[1] + 6), label, 19, color, bold=True)
    return int(w)


def load() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def tools_of(turn: dict) -> list[dict]:
    return [e["data"] for e in turn["events"] if e["kind"] == "tool_call"]


def results_of(turn: dict) -> list[dict]:
    return [e["data"] for e in turn["events"] if e["kind"] == "tool_result"]


# --------------------------------------------------------------------------- #
# 图 1：三场景对照
# --------------------------------------------------------------------------- #
def fig_overview(data: dict) -> None:
    W, H = 1800, 760
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    text(d, (80, 56), "三个典型对话场景 · 实际执行轨迹", 34, INK, bold=True)
    text(d, (80, 106), "脚本化模型驱动，走的是同一套 AgentLoop / 工具 / 校验闸门代码", 22, MUTED)

    lanes = data["scenarios"]
    y = 170
    for sc in lanes:
        t = sc["turns"][-1]
        names = [x["name"] for x in tools_of(t)]
        d.rounded_rectangle((80, y, W - 80, y + 150), radius=16, fill=GREY_SOFT, outline=LINE, width=1)
        text(d, (110, y + 22), f"场景 {sc['id']}", 26, INK, bold=True)
        for i, ln in enumerate(wrap(d, sc["title"], 19, 300)[:2]):
            text(d, (110, y + 60 + i * 26), ln, 20, MUTED)

        cx = 450
        for i, n in enumerate(names):
            w = chip(d, (cx, y + 50), n, BLUE, BLUE_SOFT)
            cx += w + 10
            if i < len(names) - 1:
                arrow(d, (cx, y + 67), (cx + 22, y + 67), MUTED, 2)
                cx += 34

        status = t["final"].get("status") or ""
        if sc["id"] == "A":
            note, col, soft = (f"拦截后 {sc['orders_before']} 张单 → 确认后 {sc['orders_after']} 张",
                               GREEN, GREEN_SOFT)
        elif sc["id"] == "B":
            note, col, soft = ("先澄清再执行，未编造缺失信息", AMBER, AMBER_SOFT)
        else:
            note, col, soft = ("命中最终阻断，转人工", RED, RED_SOFT)
        nw = d.textlength(note, font=f(20, True)) + 30
        d.rounded_rectangle((W - 110 - nw, y + 92, W - 110, y + 134), radius=10,
                            fill=soft, outline=col, width=1)
        text(d, (W - 110 - nw + 15, y + 100), note, 20, col, bold=True)
        text(d, (W - 110 - nw, y + 52), f"status = {status}", 19, MUTED)
        y += 180

    img.save(ASSETS / "fig-overview.png", quality=95)


# --------------------------------------------------------------------------- #
# 图 2：场景 A 主流程
# --------------------------------------------------------------------------- #
def fig_a(data: dict) -> None:
    sc = next(x for x in data["scenarios"] if x["id"] == "A")
    t = sc["turns"][0]
    calls, res = tools_of(t), results_of(t)

    W, H = 1800, 780
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    text(d, (80, 50), "场景 A · 正常调薪：校验通过，但建单前被强制拦截", 32, INK, bold=True)
    text(d, (80, 98), "提问：" + t["question"], 22, MUTED)

    labels = {
        "get_employee": ("查员工档案", "职级 / 参保地 / 入职日"),
        "lookup_param": ("查制度参数", "调薪红线与分级阈值"),
        "check_rules": ("规则校验", "只算不写；通过则签发凭证"),
        "create_draft": ("建草稿", "需合法凭证 + 人工确认"),
    }
    x, y, w, h = 80, 190, 320, 210
    for i, c in enumerate(calls):
        r = res[i] if i < len(res) else {}
        name = c["name"]
        title, sub = labels.get(name, (name, ""))
        ok = bool(r.get("ok"))
        # create_draft 被拦下时根本没有 tool_result 事件，不能按 ok=False 判成"失败"：
        # 拦截不是失败，是设计好的暂停点。
        blocked = ("拦截" in str(r.get("summary") or "")
                   or (name == "create_draft" and bool(t.get("draft_preview"))))
        if blocked:
            color, soft, tag = AMBER, AMBER_SOFT, "已拦截"
            sub = "未执行落库，等待员工确认"
        elif ok:
            color, soft, tag = GREEN, GREEN_SOFT, "通过"
        else:
            color, soft, tag = RED, RED_SOFT, "失败"
        card(d, (x, y, x + w, y + h), f"{i + 1}. {title}", f"{name}\n{sub}", tag, color, soft)

        # 结果摘要（真实返回值，截断）
        summary = str(r.get("summary") or "")
        if summary:
            for j, ln in enumerate(wrap(d, summary, 18, w - 40)[:2]):
                text(d, (x + 20, y + h + 14 + j * 24), ln, 18, MUTED)

        if i < len(calls) - 1:
            arrow(d, (x + w + 8, y + h // 2), (x + w + 52, y + h // 2), MUTED, 3)
        x += w + 60

    # 拦截点标注
    ax = 80 + 3 * (w + 60)
    d.rounded_rectangle((ax, y - 46, ax + w, y - 12), radius=8, fill=AMBER_SOFT, outline=AMBER, width=1)
    text(d, (ax + 14, y - 42), "← 拦截点：库里 0 张单", 19, AMBER, bold=True)

    # 员工确认 → 落库
    cy = y + h + 150
    d.rounded_rectangle((ax - 260, cy, ax + w + 260, cy + 170), radius=16,
                        fill=GREEN_SOFT, outline=GREEN, width=2)
    text(d, (ax - 238, cy + 22), "员工在界面点击「确认建单」", 24, GREEN, bold=True)
    body = (f"POST /api/agent/confirm-draft 携带预览里的 validation_token，"
            f"复用五道闸门落库 → 单号 {sc['confirmed'].get('order_no') or '—'}")
    for i, ln in enumerate(wrap(d, body, 20, w + 480)[:2]):
        text(d, (ax - 238, cy + 62 + i * 30), ln, 20, INK)
    text(d, (ax - 238, cy + 126),
         f"落库校验：change_orders {sc['orders_before']} 张 → {sc['orders_after']} 张，"
         f"状态 {sc['order'].get('status', '—')} / 风险 {sc['order'].get('risk_level', '—')}",
         20, MUTED)

    arrow(d, (ax + w // 2, y + h + 62), (ax + w // 2, cy - 6), GREEN, 3, dash=True)
    text(d, (ax + w // 2 + 14, y + h + 96), "确认后", 18, GREEN, bold=True)

    img.save(ASSETS / "fig-scenario-a.png", quality=95)


# --------------------------------------------------------------------------- #
# 图 3：场景 B 两轮
# --------------------------------------------------------------------------- #
def fig_b(data: dict) -> None:
    sc = next(x for x in data["scenarios"] if x["id"] == "B")
    W, H = 1800, 820
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    text(d, (80, 50), "场景 B · 信息缺失：先澄清，不猜", 32, INK, bold=True)

    x0, w, h = 150, 330, 170

    def row(y, title, turn, color, soft):
        text(d, (80, y - 34), title, 22, color, bold=True)
        calls = tools_of(turn)
        x = x0
        for i, c in enumerate(calls):
            name = c["name"]
            if name == "ask_clarification":
                q = (c["args"].get("questions") or [{}])
                sub = "、".join(str(qq.get("field")) for qq in q)
                tag, tagc = "已提问", AMBER
                soft2, color2 = AMBER_SOFT, AMBER
            else:
                sub = ""
                tag, tagc = "通过", GREEN
                soft2, color2 = soft, color
                if name == "create_draft":
                    tag, tagc = "已拦截", AMBER
                    soft2, color2 = AMBER_SOFT, AMBER
            card(d, (x, y, x + w, y + h), name, sub, tag, color2, soft2, tagc)
            if i < len(calls) - 1:
                arrow(d, (x + w + 8, y + h // 2), (x + w + 46, y + h // 2), MUTED, 3)
            x += w + 54
        return x

    t1, t2 = sc["turns"][0], sc["turns"][1]
    end1 = row(230, f'第一轮 · "{t1["question"]}"', t1, AMBER, AMBER_SOFT)
    # 第一轮收尾说明
    for i, ln in enumerate(wrap(d, "模型判断缺新卡号与生效日 → 提问后退回，不继续编造",
                                20, 640)[:2]):
        text(d, (x0, 230 + h + 18 + i * 28), ln, 20, MUTED)

    # 员工补充
    d.rounded_rectangle((80, 470, W - 80, 526), radius=10, fill=BLUE_SOFT, outline=BLUE, width=1)
    text(d, (100, 486), '员工补充："卡号 6217001234567890，12 月 1 日生效"', 22, BLUE, bold=True)

    row(590, f'第二轮 · 信息补全后', t2, GREEN, GREEN_SOFT)
    text(d, (x0, 590 + h + 18), "补全后仍是同一条链路：查档案 → 查条款 → 校验 → 建单前拦截",
         20, MUTED)

    img.save(ASSETS / "fig-scenario-b.png", quality=95)


# --------------------------------------------------------------------------- #
# 图 4：场景 C 阻断
# --------------------------------------------------------------------------- #
def fig_c(data: dict) -> None:
    sc = next(x for x in data["scenarios"] if x["id"] == "C")
    t = sc["turns"][0]
    W, H = 1800, 700
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    text(d, (80, 50), "场景 C · 降薪缺书面确认：命中最终阻断，转人工", 32, INK, bold=True)

    calls, res = tools_of(t), results_of(t)
    x, y, w, h = 80, 190, 400, 190
    for i, c in enumerate(calls):
        r = res[i] if i < len(res) else {}
        summary = str(r.get("summary") or "")
        blocked = (r.get("ok") is False) or summary.startswith("block") or "need_review" in summary
        if c["name"] == "escalate_to_human":
            color, soft, tag = BLUE, BLUE_SOFT, "已转人工"
        elif blocked:
            color, soft, tag = RED, RED_SOFT, "阻断"
        else:
            color, soft, tag = GREEN, GREEN_SOFT, "通过"
        sub = summary
        card(d, (x, y, x + w, y + h), c["name"], sub, tag, color, soft)
        if i < len(calls) - 1:
            arrow(d, (x + w + 10, y + h // 2), (x + w + 66, y + h // 2), MUTED, 3)
        x += w + 76

    # 工具集收窄示意
    by = y + h + 90
    d.rounded_rectangle((80, by, W - 80, by + 230), radius=16, fill=GREY_SOFT, outline=LINE, width=1)
    text(d, (110, by + 24), "阻断后模型能看到的工具（next_allowed 收窄）", 24, INK, bold=True)

    text(d, (110, by + 74), "阻断前：", 20, MUTED, bold=True)
    x = 220
    for n in ("create_draft", "submit_draft", "check_rules", "escalate_to_human", "…共 9 个"):
        x += chip(d, (x, by + 66), n, MUTED, GREY_SOFT) + 10

    text(d, (110, by + 132), "阻断后：", 20, RED, bold=True)
    x = 220
    for n in ("escalate_to_human", "ask_clarification"):
        x += chip(d, (x, by + 124), n, RED, RED_SOFT) + 10
    text(d, (x + 10, by + 130), "→ create_draft 不在列表里，模型拿不到就建不了单", 20, RED)

    text(d, (110, by + 186),
         "模型带走的 context：clause=第二十七条，code=BLK_SALARY_CUT_NO_CONSENT —— 人工接手不用从零查",
         19, MUTED)

    img.save(ASSETS / "fig-scenario-c.png", quality=95)


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    data = load()
    fig_overview(data)
    fig_a(data)
    fig_b(data)
    fig_c(data)
    for p in sorted(ASSETS.glob("fig-*.png")):
        print(f"{p.name:26} {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
