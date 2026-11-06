# -*- coding: utf-8 -*-
"""报价与投入产出测算（数字全部由脚本生成，避免手算误差）"""
import json

PD = 4000.0          # 元/人天
UNIT_PRICE = 8000.0  # 元/资产单元
MGMT, RISK, PROFIT, TAX = 0.15, 0.10, 0.20, 0.06

def tier(name, rows, units):
    """rows: [(科目, 条目, 人天)] ; units: 资产单元数"""
    pd_sum = sum(r[2] for r in rows)
    labor = pd_sum * PD
    unit_amt = units * UNIT_PRICE
    D = labor + unit_amt
    mgmt = D * MGMT
    risk = D * RISK
    B = D + mgmt + risk
    profit = B * PROFIT
    net = B + profit
    tax = net * TAX
    return dict(name=name, pd=pd_sum, labor=labor, units=units, unit_amt=unit_amt,
                D=D, mgmt=mgmt, risk=risk, B=B, profit=profit, net=net, tax=tax, gross=net+tax)

lite = tier("低 · Lite", [
    ("C02", "业务诊断与场景梳理（单批次、单来源）", 8),
    ("C03", "方案与架构设计（简版：数据模型 + 状态机）", 5),
    ("C05", "单源只读接入 + 映射键 + 四类断言 + 一页缺项清单", 30),
    ("C06", "评测与合规（四类断言用例 + 权限冒烟）", 5),
    ("C08", "部署与培训（单角色）", 4),
], units=3)

std = tier("中 · Standard（推荐）", [
    ("C02", "业务诊断与场景梳理（三源、P0 门禁）", 10),
    ("C03", "方案与架构设计（含 M1~M10 验收判据定义）", 12),
    ("C04", "历史单据清洗与口径对齐（40 条）", 8),
    ("C05", "三源只读接入 + 核对核心 + 交接看板 + 人工队列 + OPA + 哈希链审计 + 一页清单 + 只读导出", 62),
    ("C06", "评测与合规（M1~M10 全量 + 越权 5 条 + 审计链完整性）", 14),
    ("C08", "部署与培训（3 类角色 + 应急演练）", 8),
], units=8)

full = tier("高 · Full", [
    ("C02", "业务诊断与场景梳理（含二期范围预研）", 12),
    ("C03", "方案与架构设计（含 BPMN 审批对接预设计）", 18),
    ("C04", "历史清洗 + 季度口径复核（含多客户模板化）", 12),
    ("C05", "Standard 全量 + BPMN 审批对接 + 多批次/多客户 + 冗余通道 + 二期扩展", 88),
    ("C06", "评测与合规（全量 + 年度复测 + 越权全场景）", 22),
    ("C08", "部署与培训（3 类角色 + 跟班 2 周）", 16),
], units=8)

# ---------- ROI ----------
N_PERIOD = 12          # 期/年
Q = 300                # 条/期（虚构基线 f12）
H = 18                 # 人时/期（虚构基线 f12）
RATE = 250.0           # 元/人时（客户内部综合成本）

def roi(E=50000, F=2, k=0.80, q=Q, h=H, r1=0.35, b=0.30, r2=0.40, aud=40, aud_cut=0.60):
    yearly_h = h * N_PERIOD
    save_review = yearly_h * r1 * RATE
    save_rework = yearly_h * b * r2 * RATE
    save_err = E * F * k
    save_aud = aud * aud_cut * RATE
    total = save_review + save_rework + save_err + save_aud
    return dict(yearly_h=yearly_h, save_review=save_review, save_rework=save_rework,
                save_err=save_err, save_aud=save_aud, total=total)

base = roi()
cal  = roi(E=150000, F=4)
multi= roi()   # 5 客户，工时与差错 ×5

def cash(net_once, maint_year, years=3, r=0.08, benefit=0.0, extra_once=0.0):
    pv_b = sum(benefit / (1+r)**t for t in range(1, years+1))
    pv_c = net_once + extra_once
    for t in range(1, years+1):
        pv_c += maint_year / (1+r)**t
    total_c = net_once + extra_once + maint_year*years
    total_b = benefit*years
    return dict(pv_b=pv_b, pv_c=pv_c, npv=pv_b-pv_c, total_c=total_c, total_b=total_b,
                net=total_b-total_c)

c_base = cash(std["net"], std_maint := 72000, benefit=base["total"])
c_cal  = cash(std["net"], std_maint, benefit=cal["total"])
c_multi= cash(std["net"], std_maint, benefit=multi["total"]*5 - 0, extra_once=576000)

# 盈亏平衡：3 年回本所需的年化收益
need = c_base["total_c"]/3
need_err = need - base["save_review"] - base["save_rework"] - base["save_aud"]
breakeven_E = need_err / (2*0.8)
breakeven_F = need_err / (50000*0.8)

# 单位经济
per_item_cost = std["net"] / (N_PERIOD*Q)
per_item_manual = (H/Q)*RATE

out = {
 "tiers": {t["name"]: {k: round(v,2) for k,v in t.items() if isinstance(v,(int,float))} for t in (lite,std,full)},
 "base": {k: round(v,2) for k,v in base.items()},
 "cal": {k: round(v,2) for k,v in cal.items()},
 "c_base": {k: round(v,2) for k,v in c_base.items()},
 "c_cal": {k: round(v,2) for k,v in c_cal.items()},
 "c_multi": {k: round(v,2) for k,v in c_multi.items()},
 "need_year": round(need,2), "need_err": round(need_err,2),
 "be_E": round(breakeven_E,2), "be_F": round(breakeven_F,2),
 "per_item_cost": round(per_item_cost,2), "per_item_manual": round(per_item_manual,2),
 "payback_base": round(std["net"]/base["total"],2),
 "payback_cal": round(std["net"]/cal["total"],2),
}
# 单价敏感性
sens = {}
for p in (3500, 4000, 4500):
    for t, us in ((lite,3),(std,8),(full,8)):
        pdv = t["pd"]*p + us*UNIT_PRICE
        net = pdv*1.25*1.2
        sens[f"{t['name']}@{p}"] = round(net*1.06)
out["sens"] = sens
out["rush_std_net"] = round(std["net"]*1.15)
out["rush_std_gross"] = round(std["net"]*1.15*1.06)

with open(r"E:\code\PayrollServiceChangeCheck\docs\.workbuddy\tmp_quote_out.json","w",encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("ok")
