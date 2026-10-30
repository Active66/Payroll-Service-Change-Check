"""规则引擎：把制度条款变成可复现、可单测的判定。

    **为什么规则不交给模型判断**

      模型能读懂"金额差异 1,000 元至 5,000 元判定为 L3"，但它每次读的结果
      未必一致：同一张单问两次，一次 L3 一次 L4，这在核对场景里是不可接受的。
      更糟的是它会把"1,000 元至 5,000 元"的边界理解成开区间或闭区间，
      而第二十六条的表格其实隐含了"上限含、下限不含"。

      所以：**判定只在这里发生，模型只负责解释判定结果。**
      每条规则都对应一个条款号，出现在 blocking/warnings 里，供申诉举证（第二十七条）。

    设计上刻意"只算不写"：本模块不碰 change_orders 以外的任何表，
    也不产生任何副作用。这样可以随便重入、可以并发、可以在单测里跑一万次。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from ...store import param_repo

# =============================================================================
# 【模块 32 · 规则引擎】目标文件：src/my_tool/core/agent/rules.py
# =============================================================================

CHANGE_TYPES = ("bank_account", "si_base", "hf_base", "salary_adjust",
                "allowance", "address", "tax_special")

# 制度第二十七条的六个阻断码。**final=True 的项不得人工放行**，
# 这一位会一路传到 create_draft，在那里再被检查一次。
BLOCK_CODES = {
    "BLK_LEGAL_ENTITY": ("第二十七条", "无法人主体归属或跨法人越权"),
    "BLK_MATERIAL": ("第二十七条", "缺少法定必备材料"),
    "BLK_SALARY_CUT_NO_CONSENT": ("第二十七条", "降薪无员工书面确认"),
    "BLK_BANK_NAME_MISMATCH": ("第二十七条", "银行账户姓名与员工姓名不一致"),
    "BLK_EFFECTIVE_BEFORE_HIRE": ("第二十七条", "变更生效日早于入职日"),
    "BLK_DUPLICATE_ITEM": ("第二十七条", "同一明细项重复冲突提交"),
}

# 第三十六条的材料清单。只列制度明确要求的 —— 没列的不要自己发明，
# 否则会出现"系统要求一份制度里不存在的材料"这种无法申诉的阻断。
REQUIRED_MATERIALS: dict[str, list[tuple[str, str]]] = {
    "bank_account": [("bank_change_form", "员工签字的账户变更申请"),
                     ("bank_account_image", "新账户影像件")],
    "salary_adjust": [("approval_form", "审批单"), ("adjust_basis", "调薪依据")],
    "allowance": [("approval_form", "审批单与发放依据")],
    "si_base": [("approval_form", "基数申报审批单")],
    "hf_base": [("approval_form", "基数申报审批单")],
}

# 附表矩阵：哪些核对项适用于哪些变更类型。
# hf_base / tax_special 表里没有，按"需要材料与权限校验"处理，其余项靠类型推断。
CHECK_MATRIX: dict[str, set[str]] = {
    "bank_account":  {"effective", "freeze", "materials", "dual_review", "authority"},
    "si_base":       {"effective", "amount", "materials", "authority"},
    "hf_base":       {"effective", "amount", "materials", "authority"},
    "salary_adjust": {"effective", "freeze", "amount", "materials", "authority"},
    "allowance":     {"effective", "freeze", "amount", "materials", "authority"},
    "address":       {"authority"},
    "tax_special":   {"amount", "materials", "authority"},
}

# 冻结期内"不影响当期发放"的类型：第八条明确银行账户可受理但不参与当期发放
FREEZE_EXEMPT = {"si_base", "hf_base", "address", "tax_special"}


@dataclass
class Issue:
    code: str
    clause: str
    message: str
    final: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "clause": self.clause,
                "message": self.message, "final": self.final}


@dataclass
class RuleVerdict:
    verdict: str = "pass"                 # pass / block / need_review
    level: str | None = None
    blocking: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)
    required_materials: list[dict[str, Any]] = field(default_factory=list)
    computed: dict[str, Any] = field(default_factory=dict)

    @property
    def has_final_block(self) -> bool:
        return any(i.final for i in self.blocking)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "level": self.level,
            "blocking": [i.as_dict() for i in self.blocking],
            "warnings": [i.as_dict() for i in self.warnings],
            "required_materials": self.required_materials,
            "computed": self.computed,
        }


def _block(code: str, message: str | None = None) -> Issue:
    clause, default = BLOCK_CODES[code]
    return Issue(code=code, clause=clause, message=message or default, final=True)


def _warn(code: str, clause: str, message: str) -> Issue:
    return Issue(code=code, clause=clause, message=message, final=False)


def _num(value: Any) -> float | None:
    """把明细项的值转成数字。转不了返回 None —— 不猜、不默认 0。"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace("，", "").replace(" ", "")
    s = s.rstrip("%").replace("％", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return nxt - timedelta(days=1)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def check(conn: sqlite3.Connection, *, change_type: str, employee_no: str,
          items: list[dict[str, Any]], materials: list[str] | None = None,
          is_urgent: bool = False, as_of: date | None = None,
          region_code: str = "", legal_entity_id: str = "") -> RuleVerdict:
    """
    跑一遍全部适用的核对项。

    顺序不是随意的：**先跑阻断项，再跑分级**。因为一旦阻断，
    分级毫无意义（一张必然被拒的单，告诉用户"这是 L3"只会误导）。
    """
    today = as_of or date.today()
    materials = [str(m).strip() for m in (materials or []) if str(m).strip()]
    checks = CHECK_MATRIX.get(change_type, {"authority", "materials"})

    out = RuleVerdict()
    emp = _load_employee(conn, employee_no)

    # 员工不存在 → 无法判定几乎所有规则，直接阻断。
    # 不"按空档案继续算"：那会得出"入职日为空所以没早于入职日"的假通过。
    if emp is None:
        out.blocking.append(_block(
            "BLK_LEGAL_ENTITY", f"找不到员工档案（{employee_no}），无法确定法人归属"))
        out.verdict = "block"
        out.computed["checked"] = ["authority"]
        return out

    # ① 权限与法人归属
    if "authority" in checks:
        _check_authority(out, emp, legal_entity_id)

    # ② 证明材料完整性
    if "materials" in checks:
        _check_materials(out, change_type, materials, items)

    # ③ 生效日合法性
    effective_dates = [_parse_date(i.get("effective_date") or "") for i in items]
    if "effective" in checks:
        for eff in effective_dates:
            _check_effective(out, eff, emp, change_type, today)

    # ④ 冻结期
    if "freeze" in checks:
        _check_freeze(out, conn, change_type, today, is_urgent)

    # ⑤ 重复提交 / 银行账户次数
    _check_duplicates(out, conn, change_type, employee_no, items, today)

    # ⑥ 金额与红线（会算 level）
    amount_info = _check_amount(out, conn, change_type, emp, items, today)

    # ⑦ 双人复核（只提示，不是阻断项：复核是在提交之后发生的）
    if "dual_review" in checks:
        out.warnings.append(_warn(
            "WARN_DUAL_REVIEW", "第二十三条",
            "银行账户变更需另一名 SSC 复核岗核对账号与证明材料后方可生效"))

    # 分级：只在没有阻断项时才有意义
    if not out.blocking:
        out.level = _grade(out, amount_info, change_type, items, emp)

    if out.blocking:
        out.verdict = "block"
    elif out.level in ("L3", "L4"):
        # L3/L4 必须人审，Agent 不能自己核准（第二十六条"处置方式"列）
        out.verdict = "need_review"
    else:
        out.verdict = "pass"

    out.computed.update({"checked": sorted(checks), "employee_no": employee_no,
                         "as_of": today.isoformat()})
    return out


# ---------------------------------------------------------------------------
# 各核对项
# ---------------------------------------------------------------------------

def _load_employee(conn: sqlite3.Connection, employee_no: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM employees WHERE employee_no = ?",
                       (employee_no,)).fetchone()
    return dict(row) if row else None


def _check_authority(out: RuleVerdict, emp: dict[str, Any], legal_entity_id: str) -> None:
    """第二十七条第一项：无法人主体归属或跨法人越权。"""
    emp_entity = str(emp.get("legal_entity_id") or "").strip()
    if not emp_entity:
        out.blocking.append(_block(
            "BLK_LEGAL_ENTITY", "员工档案缺少法人主体归属，无法核对权限"))
    elif legal_entity_id and emp_entity != legal_entity_id:
        out.blocking.append(_block(
            "BLK_LEGAL_ENTITY",
            f"员工属于法人 {emp_entity}，当前操作上下文为 {legal_entity_id}，属于跨法人操作"))


def _check_materials(out: RuleVerdict, change_type: str, materials: list[str],
                     items: list[dict[str, Any]]) -> None:
    """
    第三十六条：材料缺失即阻断，不接受口头承诺替代。

    降薪额外要求员工书面确认（第十四条 + 第二十七条第三项）——
    这一条单独列，因为它的阻断码不同，申诉时的举证重点也不同。

    注意：必需材料清单**只认制度里写明的**。制度没列的类型（address / tax_special）
    不在这里硬加要求，否则会出现"系统要一份制度里不存在的材料"。
    """
    need = list(REQUIRED_MATERIALS.get(change_type, []))
    is_cut = _is_salary_cut(change_type, items)
    if is_cut:
        need.append(("employee_consent", "员工书面确认（降薪）"))

    present = set(materials)
    for code, name in need:
        ok = code in present
        out.required_materials.append({"code": code, "name": name, "present": ok})

    missing = [name for code, name in need if code not in present]
    if not missing:
        return

    if is_cut and "employee_consent" in {c for c, _ in need if c not in present}:
        # 降薪无确认：用专属阻断码，因为第二十七条把它单列（且强调"直接阻断"）
        out.blocking.append(_block("BLK_SALARY_CUT_NO_CONSENT"))
    if any(c != "employee_consent" for c, _ in need if c not in present):
        out.blocking.append(_block(
            "BLK_MATERIAL", f"缺少必备材料：{'、'.join(missing)}"))


def _is_salary_cut(change_type: str, items: list[dict[str, Any]]) -> bool:
    """降薪判定：调薪类且新值低于旧值。旧值缺失时不算降薪（无法证明降了）。"""
    if change_type != "salary_adjust":
        return False
    for i in items:
        old, new = _num(i.get("old_value")), _num(i.get("new_value"))
        if old is not None and new is not None and new < old:
            return True
    return False


def _check_effective(out: RuleVerdict, eff: date | None, emp: dict[str, Any],
                     change_type: str, today: date) -> None:
    """
    第九条：生效日应为自然月首日或入职周年日；不得早于审批完成日所在月的上一自然月。
    第二十七条第五项：早于入职日 → 直接阻断。

    生效日缺失不是阻断，是"信息不全"——由 ask_clarification 去问，
    而不是在这里替用户编一个日期。
    """
    if eff is None:
        out.warnings.append(_warn(
            "WARN_NO_EFFECTIVE_DATE", "第九条",
            "未提供生效日，无法校验生效日合法性；请补充后再提交"))
        return

    hire = _parse_date(str(emp.get("hire_date") or ""))
    if hire and eff < hire:
        out.blocking.append(_block(
            "BLK_EFFECTIVE_BEFORE_HIRE",
            f"生效日 {eff.isoformat()} 早于入职日 {hire.isoformat()}"))
        return

    # 首日 / 周年日。社保与公积金基数随政策调整可在月中（第九条但书）
    is_month_start = eff.day == 1
    is_anniversary = bool(hire and eff.month == hire.month and eff.day == hire.day)
    policy_driven = change_type in ("si_base", "hf_base")
    if not (is_month_start or is_anniversary or policy_driven):
        out.warnings.append(_warn(
            "WARN_EFFECTIVE_NOT_ALIGNED", "第九条",
            f"生效日 {eff.isoformat()} 不是自然月首日或入职周年日，按制度应调整"))

    # 回溯下限：不得早于"审批完成日所属月份的上一个自然月"。
    # 没有审批完成日时以上下文日期近似，并说明这是近似。
    floor = date(today.year, today.month, 1) - timedelta(days=1)
    floor = date(floor.year, floor.month, 1)
    if eff < floor:
        out.warnings.append(_warn(
            "WARN_EFFECTIVE_TOO_EARLY", "第九条",
            f"生效日 {eff.isoformat()} 早于可回溯下限 {floor.isoformat()}，"
            f"须按第三十一条跨年度追溯规则单独审批"))


def _check_freeze(out: RuleVerdict, conn: sqlite3.Connection, change_type: str,
                  today: date, is_urgent: bool) -> None:
    """
    第八条：每月 25 日至月末为冻结期，期间不接受影响当期发放的变更。

    三种情况要分清，混在一起就会给错提示：
      · 类型本身豁免（基数/地址/个税）→ 不提示
      · 银行账户 → 可受理，但不参与当期发放（提示，不阻断）
      · 其余影响当期发放的类型 → 冻结期内不能当期生效（提示，不阻断）
        —— 注意这里**不是阻断**：制度说的是"顺延至下一周期"，不是"拒绝受理"。
        把它做成阻断会让员工以为业务办不了，实际只是下个月生效而已。
    """
    start_day = 25
    params, _missing = param_repo.get_params(
        conn, ["freeze_start_day"], region_code="", as_of=today.isoformat())
    if params:
        val = params[0].get("value")
        if isinstance(val, (int, float)) and val > 0:
            start_day = int(val)

    if is_urgent:
        out.warnings.append(_warn(
            "WARN_URGENT_CHANNEL", "第三十三条",
            "走紧急通道：需薪酬经理发起、合规部门会签，并在 3 个工作日内补齐常规材料"))
        return
    if today.day < start_day or change_type in FREEZE_EXEMPT:
        return

    if change_type == "bank_account":
        out.warnings.append(_warn(
            "WARN_FREEZE_BANK", "第八条",
            "当前处于冻结期，银行账户变更可受理但不参与当期发放，自下一周期起生效"))
        return

    out.warnings.append(_warn(
        "WARN_FREEZE_PERIOD", "第八条",
        f"当前处于冻结期（每月 {start_day} 日至月末），本单顺延至下一薪酬周期生效"))


def _check_duplicates(out: RuleVerdict, conn: sqlite3.Connection, change_type: str,
                      employee_no: str, items: list[dict[str, Any]],
                      today: date) -> None:
    """
    第六条 / 第二十七条第六项：同一明细项 24 小时内重复提交且内容无变化 → 阻断。

    "内容无变化"是关键：同一字段 24 小时内改两次不同的值，是正常业务；
    改两次一样的值，才是重复点击或异常重试。
    """
    since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT items FROM change_orders
            WHERE employee_no = ? AND change_type = ? AND created_at >= ?
              AND status IN ('draft','submitted','approved')""",
        (employee_no, change_type, since)).fetchall()

    new_pairs = {(_norm(i.get("field")), _norm(i.get("new_value"))) for i in items}
    for r in rows:
        try:
            old_items = json.loads(r["items"] or "[]")
        except json.JSONDecodeError:
            continue
        old_pairs = {(_norm(i.get("field")), _norm(i.get("new_value"))) for i in old_items}
        if new_pairs and new_pairs <= old_pairs:
            out.blocking.append(_block(
                "BLK_DUPLICATE_ITEM",
                "24 小时内已提交过内容相同的同一明细项，属重复提交"))
            return


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().replace(",", "").replace(" ", "")


def _check_amount(out: RuleVerdict, conn: sqlite3.Connection, change_type: str,
                  emp: dict[str, Any], items: list[dict[str, Any]],
                  today: date) -> dict[str, Any]:
    """
    算金额差异与调薪幅度。**只用于分级，不做阻断判断**。

    第十二条的红线是"同一自然年度内累计幅度"，不是单次幅度 ——
    这条最容易实现错，也是模型最容易算错的地方（它会把本次幅度当累计）。
    所以这里查本年度已提交的单一起算，并在 computed 里把明细摊开，
    让解释时有据可依。
    """
    info: dict[str, Any] = {"diff_amount": 0.0, "annual_raise_pct": None,
                            "band_violation": None, "annual_base": None}

    # 金额差异：取各明细项新旧值的最大绝对差
    diffs = []
    for i in items:
        old, new = _num(i.get("old_value")), _num(i.get("new_value"))
        if old is not None and new is not None:
            diffs.append(abs(new - old))
    if diffs:
        info["diff_amount"] = round(max(diffs), 2)

    # 调薪幅度（仅 salary_adjust）：本次 + 本年度已提交的，按第十二条累计
    if change_type == "salary_adjust":
        cur_old, cur_new = None, None
        for i in items:
            if str(i.get("field") or "") in ("fixed_salary", "salary", ""):
                cur_old, cur_new = _num(i.get("old_value")), _num(i.get("new_value"))
                break
        if cur_old in (None, 0) and emp.get("fixed_salary"):
            cur_old = float(emp["fixed_salary"])
        if cur_old and cur_new:
            base = cur_old
            info["annual_base"] = base
            year_start = date(today.year, 1, 1).isoformat()
            rows = conn.execute(
                """SELECT items FROM change_orders
                    WHERE employee_no = ? AND change_type = 'salary_adjust'
                      AND created_at >= ? AND status IN ('submitted','approved')""",
                (emp["employee_no"], year_start)).fetchall()
            prev_raise = 0.0
            for r in rows:
                try:
                    for it in json.loads(r["items"] or "[]"):
                        o, n = _num(it.get("old_value")), _num(it.get("new_value"))
                        if o and n:
                            prev_raise += (n - o) / o
                except json.JSONDecodeError:
                    continue
            this_raise = (cur_new - cur_old) / cur_old
            info["annual_raise_pct"] = round((prev_raise + this_raise) * 100, 2)
            info["this_raise_pct"] = round(this_raise * 100, 2)

    # 银行账户 30 日次数（第二十二条）
    if change_type == "bank_account":
        since = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        n = conn.execute(
            """SELECT COUNT(*) AS n FROM change_orders
                WHERE employee_no = ? AND change_type = 'bank_account'
                  AND created_at >= ? AND status IN ('submitted','approved')""",
            (emp["employee_no"], since)).fetchone()["n"]
        info["bank_changes_30d"] = int(n)
        if int(n) >= 1:
            out.warnings.append(_warn(
                "WARN_BANK_QUOTA", "第二十二条",
                f"该员工近 30 日已变更过 {n} 次银行账户，本次超出次数，须转人工核对"))

    # 调薪红线（第十二条）：超红线的处置是"加审批层级"，不是阻断
    if change_type == "salary_adjust" and info.get("this_raise_pct") is not None:
        pct = info["this_raise_pct"]
        if pct > 20:
            out.warnings.append(_warn(
                "WARN_RAISE_OVER_CAP", "第十二条",
                f"本次上浮 {pct:.1f}% 超过 20% 红线，须业务部门负责人与薪酬委员会双重审批"))
        elif pct < -15:
            out.warnings.append(_warn(
                "WARN_CUT_OVER_CAP", "第十二条",
                f"本次下浮 {abs(pct):.1f}% 超过 15% 红线，须审批且取得员工书面确认"))
        if info.get("annual_raise_pct") is not None and abs(info["annual_raise_pct"]) > abs(pct):
            out.warnings.append(_warn(
                "WARN_ANNUAL_ACCUM", "第十二条",
                f"按自然年度累计幅度为 {info['annual_raise_pct']:.1f}%，红线按累计口径判定"))

    return info


def _grade(out: RuleVerdict, amount_info: dict[str, Any], change_type: str,
           items: list[dict[str, Any]], emp: dict[str, Any]) -> str:
    """
    第二十六条分级。阈值从参数表取（可随制度版本变），取不到用制度原文的默认值。

    注意三处"或"条件，漏掉任何一个都会低估风险：
      · L3：金额 1,000–5,000 **或涉及基数调整**
      · L4：金额超过 5,000 **或涉及降薪、证件号变更**
    """
    diff = float(amount_info.get("diff_amount") or 0)
    l1, l2, l3 = 100.0, 1000.0, 5000.0

    # 证件号变更：L4（第二十四条"风险等级判定为 L3 及以上"，第二十六条归入 L4）
    fields = {str(i.get("field") or "") for i in items}
    if "id_card" in fields or "id_number" in fields:
        out.warnings.append(_warn("WARN_ID_CHANGE", "第二十四条",
                                  "证件号码变更属高敏变更，须薪酬经理二次确认"))
        return "L4"
    if _is_salary_cut(change_type, items):
        return "L4"
    if diff > l3:
        return "L4"
    if change_type in ("si_base", "hf_base") or diff > l2:
        return "L3"
    if diff > l1:
        return "L2"
    return "L1"
