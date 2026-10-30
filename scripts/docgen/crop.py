# -*- coding: utf-8 -*-
"""按 viewBox 裁切渲染 SVG，用于局部细节校验。

用法：先生成 docs/assets/ai_flow.svg（见 gen_ai_flow.py），再用本脚本放大局部截图。
依赖本机 Chrome，路径在下方 CHROME 中按需修改。
"""
import io
import os
import re
import subprocess

# scripts/docgen/ → 仓库根 → docs/assets
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASSETS = os.path.join(REPO_ROOT, "docs", "assets")
svg = io.open(os.path.join(ASSETS, "ai_flow.svg"), encoding="utf-8").read()
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def crop(name, x, y, w, h, scale=2):
    s = re.sub(r'width="\d+" height="\d+" viewBox="0 0 \d+ \d+"',
               f'width="{w}" height="{h}" viewBox="{x} {y} {w} {h}"', svg, count=1)
    html = (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<style>html,body{{margin:0;padding:0;background:#fcfcfb}}svg{{display:block}}</style>'
            f'</head><body>{s}</body></html>')
    hp = os.path.join(ASSETS, f"_{name}.html")
    io.open(hp, "w", encoding="utf-8").write(html)
    op = os.path.join(ASSETS, f"_{name}.png")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--force-device-scale-factor={scale}", f"--window-size={w},{h}",
                    f"--screenshot={op}", "file:///" + hp.replace("\\", "/")],
                   capture_output=True)
    print(name, op)


if __name__ == "__main__":
    crop("crop_head", 0, 0, 1680, 230)
    crop("crop_foot", 0, 1900, 1680, 230)
    crop("crop_queue", 820, 1050, 830, 400)
    crop("crop_mid", 0, 470, 900, 460)
