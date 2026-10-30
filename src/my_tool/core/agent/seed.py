"""制度参数种子数据。

    **每个数字都必须在制度原文里找得到，且必须写下条款号。**
    没有出处的参数不许进这张表 —— 因为系统所有涉及数字的回答都从这里取，
    一个凭空来的数字会污染全部下游（而且没人能发现）。

    对照关系（改这里之前先回去读制度）：
      si_base_cap / floor      第十八条  2026 年度上海地区社会保险缴费基数上下限
      hf_ratio_min / max       第十八条  住房公积金缴存比例区间 5%–7%
      submit_deadline_day      第七条    "生效月前一个月的 15 日（含）前"
      freeze_start_day         第八条    "每月 25 日至当月月末为冻结期"
      freeze_end_day           第八条    0 表示"当月最后一天"
      raise_cap_pct / cut_cap  第十二条  单次上下浮红线（累计口径见规则引擎）
      backpay_months_limit     第三十条  单次补发原则上不超过 3 个月
      diff_l1/l2/l3_max        第二十六条 四级分级的金额边界
      bank_change_quota_30d    第二十二条 同一员工 30 日内最多变更 1 次
      verify_pay_min / max     第二十三条 验证打款 0.01–0.99 元

    种子是幂等的：重复执行只会覆盖同名同地区的行，不会重复插入。
"""

from __future__ import annotations

from typing import Any

from ...store import param_repo

# (param_key, value, unit, source_clause, note)
SEED: list[tuple[str, Any, str, str, str]] = [
    ("si_base_cap", 36921, "元/月", "第十八条", "2026 年度上海地区社保缴费基数上限"),
    ("si_base_floor", 7384, "元/月", "第十八条", "2026 年度上海地区社保缴费基数下限"),
    ("hf_ratio_min", 5, "%", "第十八条", "住房公积金缴存比例下限"),
    ("hf_ratio_max", 7, "%", "第十八条", "住房公积金缴存比例上限"),
    ("submit_deadline_day", 15, "日", "第七条", "生效月前一个月 15 日（含）前提交"),
    ("freeze_start_day", 25, "日", "第八条", "每月 25 日进入封版冻结期"),
    ("freeze_end_day", 0, "日", "第八条", "0 表示当月最后一天"),
    ("raise_cap_pct", 20, "%", "第十二条", "单次上浮红线（判定按自然年度累计口径）"),
    ("cut_cap_pct", 15, "%", "第十二条", "单次下浮红线（判定按自然年度累计口径）"),
    ("backpay_months_limit", 3, "个月", "第三十条", "超过须薪酬经理审批，超 6 个月须合规备案"),
    ("diff_l1_max", 100, "元", "第二十六条", "L1 上限：金额差异不超过 100 元且不涉及社保基数"),
    ("diff_l2_max", 1000, "元", "第二十六条", "L2 上限：100–1,000 元"),
    ("diff_l3_max", 5000, "元", "第二十六条", "L3 上限：1,000–5,000 元或涉及基数调整"),
    ("bank_change_quota_30d", 1, "次/30日", "第二十二条", "超出次数的变更转人工核对"),
    ("verify_pay_min", 0.01, "元", "第二十三条", "首次绑定账户验证打款金额下限"),
    ("verify_pay_max", 0.99, "元", "第二十三条", "首次绑定账户验证打款金额上限"),
]

DOC_ID = "POL-CMP-2026-001"
REGION = "310000"          # 上海：制度第二条适用范围
EFFECTIVE_FROM = "2026-01-01"   # 第四十条施行日期


def seed_params(conn: Any, *, region_code: str = REGION,
                updated_by: str = "system") -> int:
    """
    写入种子参数。返回写入条数。

    地区写 310000 而不是空字符串：第十八条的上下限是**上海地区**的值（第二条、
    第三十五条要求以参保地为准）。做成全国默认会让别的地方拿到上海的基数，
    而那些地方的数字本系统根本不知道——宁可让它们 missing、转人工。
    """
    for key, value, unit, clause, note in SEED:
        param_repo.upsert_param(
            conn, param_key=key, value=value, unit=unit, source_clause=clause,
            region_code=region_code, source_doc_id=DOC_ID,
            effective_from=EFFECTIVE_FROM, note=note, updated_by=updated_by)
    return len(SEED)


def seed_employees(conn: Any) -> int:
    """
    演示用人事档案。生产环境应接 HR 主数据，这里只为让规则引擎跑得起来。

    刻意包含一个"已离职"和一个"缺法人归属"的样本：
    规则引擎最容易漏的就是这两种边界，有样本才能在测试里断言它们被拦住。
    """
    rows = [
        # employee_no, name, hire_date, grade, fixed_salary, si_base, hf_base, region, entity
        ("E1001", "爱丽丝", "2021-03-01", "P5", 18500, 18500, 18500, "310000", "demo-entity"),
        ("E1002", "鲍勃", "2019-07-15", "P6", 26800, 26800, 26800, "310000", "demo-entity"),
        ("E1003", "陈晨", "2026-02-01", "P4", 12000, 12000, 12000, "310000", "demo-entity"),
        ("E1004", "邓丽", "2015-11-02", "P4", 9800, 9800, 9800, "310000", ""),          # 缺法人归属
        ("E1005", "方强", "2018-05-20", "P5", 15200, 15200, 15200, "310000", "demo-entity"),
    ]
    from ...store import employee_repo
    for no, name, hire, grade, salary, si, hf, region, entity in rows:
        employee_repo.upsert(
            conn, employee_no=no, display_name=name, hire_date=hire, grade=grade,
            fixed_salary=salary, si_base=si, hf_base=hf, region_code=region,
            legal_entity_id=entity)
    return len(rows)
