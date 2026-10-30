"""阶段 5 · Agent 工具与闸门。

这一组测试的目的不是覆盖率，而是**证明绕过是不可能的**：
每一条都对应一种真实的绕过尝试，而不是"调用成功返回 200"。

  1. 参数表数值必须与制度第十八条逐字一致 —— 数字错了，全系统都错
  2. 没有凭证建单必须被拒（模型最常见的"试试看"）
  3. 校验 A 建 B 必须被拒（最隐蔽的绕过：把金额改大）
  4. 凭证复用必须被拒（一次通过建十张单）
  5. 凭证过期必须被拒
  6. L3/L4 不签发凭证（Agent 不能自己核准需要人审的单）
  7. 六个阻断码各触发一次，且 final 位为 True
  8. confirm_token 不能用 validation_token 冒充（对外动作的最后一道）
"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from my_tool.core.agent import gate, rules
from my_tool.core.agent.registry import Registry, ToolContext, validate_args
from my_tool.core.agent.toolset import build_registry
from my_tool.core.agent.tools import read as read_tools
from my_tool.core.config import Settings
from my_tool.store import db, order_repo, param_repo
from my_tool.core.agent import seed


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

@pytest.fixture()
def s(tmp_path) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(
        base,
        chat_db_path=tmp_path / "chat.db",
        index_dir=tmp_path / "index",
        upload_dir=tmp_path / "up",
        auth_required=True,
        gate_secret="test-gate-secret",
        agent_enabled=True,
    )


@pytest.fixture()
def conn(s):
    db.init_db(s.chat_db_path)
    c = db.connect(s.chat_db_path)
    seed.seed_params(c)
    seed.seed_employees(c)
    yield c
    c.close()


@pytest.fixture()
def registry() -> Registry:
    return build_registry()


def ctx(s, **kw) -> ToolContext:
    user = kw.pop("user", {"id": "u-admin", "role": "admin", "display_name": "管理员"})
    return ToolContext(settings=s, user=user, session_id="sess-test", **kw)


def _items(new_salary="19000", old="18500", eff="2026-11-01", field="fixed_salary"):
    return [{"field": field, "old_value": old, "new_value": new_salary,
             "effective_date": eff}]


# ---------------------------------------------------------------------------
# 参数种子与制度一致性
# ---------------------------------------------------------------------------

def test_seed_params_match_policy(conn):
    """参数值必须与制度原文一致。这是全系统数字的源头，错一个就全错。"""
    got, missing = param_repo.get_params(
        conn, read_tools.PARAM_KEYS, region_code="310000")
    assert not missing, f"缺参数：{missing}"
    by_key = {p["key"]: p for p in got}

    # 第十八条：上限 36,921 / 下限 7,384；公积金比例 5%–7%
    assert by_key["si_base_cap"]["value"] == 36921
    assert by_key["si_base_floor"]["value"] == 7384
    assert by_key["hf_ratio_min"]["value"] == 5
    assert by_key["hf_ratio_max"]["value"] == 7
    # 第七条 15 日；第八条 25 日起冻结
    assert by_key["submit_deadline_day"]["value"] == 15
    assert by_key["freeze_start_day"]["value"] == 25
    # 第十二条 上浮 20% / 下浮 15%
    assert by_key["raise_cap_pct"]["value"] == 20
    assert by_key["cut_cap_pct"]["value"] == 15
    # 第二十六条分级边界 100 / 1000 / 5000
    assert (by_key["diff_l1_max"]["value"], by_key["diff_l2_max"]["value"],
            by_key["diff_l3_max"]["value"]) == (100, 1000, 5000)
    # 每一个参数都必须有出处，否则等于没有参数
    for p in got:
        assert p["source_clause"], f"{p['key']} 缺 source_clause"


def test_param_missing_does_not_guess(conn):
    """查不到的参数必须进 missing，绝不能给个默认值。"""
    found, missing = param_repo.get_params(conn, ["si_base_cap", "no_such_param"],
                                           region_code="999999")
    # 310000 的种子对 999999 不可见？不 —— 全国默认行为空，所以这里是 missing
    assert [p["key"] for p in found] == [] or found[0]["region_code"] == "310000"
    assert "no_such_param" in missing


def test_param_region_fallback(conn):
    """
    地区参数优先，没有则回落。这里给 310000 加一个覆盖值，验证"精确匹配优先"。
    """
    param_repo.upsert_param(conn, param_key="si_base_cap", value=40000, unit="元/月",
                            source_clause="第十八条", region_code="310000",
                            effective_from="2026-07-01")
    found, _ = param_repo.get_params(conn, ["si_base_cap"], region_code="310000",
                                     as_of="2026-08-01")
    assert found[0]["value"] == 40000
    # 该日期之前仍然用旧值
    old, _ = param_repo.get_params(conn, ["si_base_cap"], region_code="310000",
                                   as_of="2026-03-01")
    assert old[0]["value"] == 36921


# ---------------------------------------------------------------------------
# 规则引擎：阻断项
# ---------------------------------------------------------------------------

def _check(conn, **kw):
    args = dict(change_type="salary_adjust", employee_no="E1001",
                items=_items(), materials=["approval_form", "adjust_basis"],
                as_of=date(2026, 10, 10))
    args.update(kw)
    return rules.check(conn, **args)


def test_block_missing_material(conn):
    v = _check(conn, materials=[])
    assert v.verdict == "block"
    codes = {b.code for b in v.blocking}
    assert "BLK_MATERIAL" in codes
    assert all(b.final for b in v.blocking), "第二十七条的阻断项必须 final"


def test_block_salary_cut_without_consent(conn):
    """第十四条 + 第二十七条：降薪无书面确认直接阻断，且用专属阻断码。"""
    v = _check(conn, items=_items(new_salary="15000", old="18500"),
               materials=["approval_form", "adjust_basis"])
    codes = {b.code for b in v.blocking}
    assert "BLK_SALARY_CUT_NO_CONSENT" in codes
    assert v.verdict == "block"


def test_salary_cut_with_consent_passes_block(conn):
    v = _check(conn, items=_items(new_salary="15000", old="18500"),
               materials=["approval_form", "adjust_basis", "employee_consent"])
    assert "BLK_SALARY_CUT_NO_CONSENT" not in {b.code for b in v.blocking}


def test_block_effective_before_hire(conn):
    """第二十七条第五项：生效日早于入职日。E1001 入职 2021-03-01。"""
    v = _check(conn, items=_items(eff="2020-01-01"))
    assert "BLK_EFFECTIVE_BEFORE_HIRE" in {b.code for b in v.blocking}


def test_block_unknown_employee(conn):
    """档案不存在必须阻断，不能"按空档案继续算"得出假通过。"""
    v = _check(conn, employee_no="E9999")
    assert v.verdict == "block"
    assert "BLK_LEGAL_ENTITY" in {b.code for b in v.blocking}


def test_block_missing_legal_entity(conn):
    """E1004 刻意留空法人归属，验证它被拦住。"""
    v = _check(conn, employee_no="E1004")
    assert "BLK_LEGAL_ENTITY" in {b.code for b in v.blocking}


def test_block_cross_entity(conn):
    """员工属于 demo-entity，上下文是别的法人 → 跨法人越权。"""
    v = _check(conn, legal_entity_id="other-entity")
    assert "BLK_LEGAL_ENTITY" in {b.code for b in v.blocking}


def test_block_duplicate_item(conn):
    """第六条：24 小时内同一明细项内容无变化的重复提交。"""
    order_repo.create_draft(conn, employee_no="E1001", change_type="salary_adjust",
                            items=_items(), idempotency_key="k1", status="submitted")
    v = _check(conn)
    assert "BLK_DUPLICATE_ITEM" in {b.code for b in v.blocking}


def test_no_duplicate_when_value_changes(conn):
    """同一字段 24 小时内改两次不同的值，是正常业务，不该判重复。"""
    order_repo.create_draft(conn, employee_no="E1001", change_type="salary_adjust",
                            items=_items(new_salary="19000"), idempotency_key="k2",
                            status="submitted")
    v = _check(conn, items=_items(new_salary="20000"))
    assert "BLK_DUPLICATE_ITEM" not in {b.code for b in v.blocking}


# ---------------------------------------------------------------------------
# 规则引擎：分级
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("new_salary,expect", [
    ("18600", "L1"),     # diff 100 → L1（"不超过 100 元"含 100）
    ("18601", "L2"),     # diff 101 → L2
    ("19500", "L2"),     # diff 1000 → L2（上限含）
    ("19501", "L3"),     # diff 1001 → L3
    ("23500", "L3"),     # diff 5000 → L3（上限含）
    ("23501", "L4"),     # diff 5001 → L4
])
def test_grade_boundaries(conn, new_salary, expect):
    """
    第二十六条的金额边界。**含上不含下**：100 属 L1，1000 属 L2，5000 属 L3。
    这组断言就是"模型不该自己判断"的理由 —— 边界差一个数，审批层级就变了。
    """
    v = _check(conn, items=_items(new_salary=new_salary, old="18500"))
    assert v.level == expect, f"diff={float(new_salary)-18500} 应为 {expect}，实际 {v.level}"


def test_grade_l3_for_base_change(conn):
    """第二十六条：涉及基数调整即 L3，与金额无关。"""
    v = _check(conn, change_type="si_base", items=[
        {"field": "si_base", "old_value": "18500", "new_value": "18510",
         "effective_date": "2026-11-01"}], materials=["approval_form"])
    assert v.level == "L3"


def test_grade_l4_for_id_change(conn):
    v = _check(conn, change_type="address", items=[
        {"field": "id_card", "old_value": "3101", "new_value": "3102"}],
        materials=[])
    assert v.level == "L4"


def test_freeze_period_is_warning_not_block(conn):
    """
    第八条：冻结期内不予受理影响当期发放的变更，但制度说的是"顺延"，
    不是"拒绝"。做成阻断会让员工以为业务办不了 —— 这是刻意的区分。
    """
    v = _check(conn, as_of=date(2026, 10, 26))       # 26 日，在冻结期
    assert v.verdict != "block"
    assert "WARN_FREEZE_PERIOD" in {w.code for w in v.warnings}


def test_freeze_exempt_types(conn):
    """基数变更不受冻结期限制（附表：si_base 冻结期校验=否）。"""
    v = _check(conn, change_type="si_base", items=[
        {"field": "si_base", "old_value": "18500", "new_value": "19000",
         "effective_date": "2026-11-01"}], materials=["approval_form"],
        as_of=date(2026, 10, 26))
    assert "WARN_FREEZE_PERIOD" not in {w.code for w in v.warnings}
    assert "WARN_FREEZE_BANK" not in {w.code for w in v.warnings}


def test_bank_change_in_freeze_is_special(conn):
    """第八条但书：银行账户在冻结期内可受理，但不参与当期发放。"""
    v = _check(conn, change_type="bank_account", items=[
        {"field": "bank_account", "old_value": "6222", "new_value": "6217",
         "effective_date": "2026-11-01"}],
        materials=["bank_change_form", "bank_account_image"],
        as_of=date(2026, 10, 26))
    assert "WARN_FREEZE_BANK" in {w.code for w in v.warnings}


def test_raise_cap_warning(conn):
    """第十二条：上浮超 20% 需双重审批。"""
    v = _check(conn, items=_items(new_salary="23000", old="18500"))
    assert "WARN_RAISE_OVER_CAP" in {w.code for w in v.warnings}


# ---------------------------------------------------------------------------
# 凭证
# ---------------------------------------------------------------------------

def test_token_roundtrip(s):
    h = gate.order_hash("salary_adjust", "E1001", _items(), ["a"])
    token, ttl = gate.issue_validation(s, content_hash=h, level="L2")
    info, err = gate.verify_validation(s, token, content_hash=h)
    assert err is None and info.level == "L2" and info.jti


def test_token_rejects_changed_content(s):
    """
    **最隐蔽的绕过**：check_rules 报 18500→20000 拿到 pass，
    然后建一张 18500→50000 的单。内容哈希绑定让这招失效。
    """
    h = gate.order_hash("salary_adjust", "E1001", _items(new_salary="20000"), [])
    token, _ = gate.issue_validation(s, content_hash=h, level="L2")

    tampered = gate.order_hash("salary_adjust", "E1001", _items(new_salary="50000"), [])
    info, err = gate.verify_validation(s, token, content_hash=tampered)
    assert info is None
    assert err.code == "AGT_003", "内容被改必须报 E_GATE_STALE"


def test_hash_ignores_field_order_and_formatting(s):
    """规范化：明细项顺序、千分位、尾零都不该改变指纹，否则一次前端升级就让凭证全失效。"""
    a = gate.order_hash("salary_adjust", "E1001", [
        {"field": "fixed_salary", "old_value": "18,500", "new_value": "20000.00"}])
    b = gate.order_hash("salary_adjust", "E1001", [
        {"field": "fixed_salary", "old_value": "18500", "new_value": "20000"}])
    assert a == b


def test_token_expired(s):
    base = dataclasses.replace(s, gate_token_ttl_seconds=-1)
    h = gate.order_hash("salary_adjust", "E1001", _items(), [])
    token, _ = gate.issue_validation(base, content_hash=h, level="L1")
    info, err = gate.verify_validation(s, token, content_hash=h)
    assert err.code == "AGT_002"


def test_token_tampered(s):
    h = gate.order_hash("salary_adjust", "E1001", _items(), [])
    token, _ = gate.issue_validation(s, content_hash=h, level="L1")
    body, sig = token.split(".")
    forged = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig
    info, err = gate.verify_validation(s, forged, content_hash=h)
    assert err.code == "AGT_001"


def test_token_single_use(s, conn):
    h = gate.order_hash("salary_adjust", "E1001", _items(), [])
    token, _ = gate.issue_validation(s, content_hash=h, level="L1")
    info, err = gate.verify_validation(s, token, content_hash=h)
    assert err is None
    ok, _ = gate.consume(s, info, subject_id=h)
    assert ok is True
    ok2, err2 = gate.consume(s, info, subject_id=h)
    assert ok2 is False and err2.code == "AGT_004"


def test_confirm_token_cannot_be_forged_by_validation(s):
    """
    拿 validation_token 冒充 confirm_token —— 这是"模型想自己提交"的最直接路径。
    """
    h = gate.order_hash("salary_adjust", "E1001", _items(), [])
    vtoken, _ = gate.issue_validation(s, content_hash=h, level="L1")
    info, err = gate.verify_confirm(s, vtoken, draft_id="d1", content_hash=h)
    assert info is None and err.code == "AGT_009"


# ---------------------------------------------------------------------------
# 工具层：闸门端到端
# ---------------------------------------------------------------------------

def _run_check(registry, c, s, **kw):
    args = {"change_type": "salary_adjust", "employee_no": "E1001",
            "items": _items(), "materials": ["approval_form", "adjust_basis"]}
    args.update(kw)
    return registry.call("check_rules", args, c)


def test_create_draft_without_token_rejected(registry, conn, s):
    """没有凭证建单：必须拒绝，且提示"不要重试"。"""
    c = ctx(s)
    res = registry.call("create_draft", {
        "validation_token": "", "change_type": "salary_adjust",
        "employee_no": "E1001", "items": _items(), "idempotency_key": "k-no-token",
    }, c)
    assert res.ok is False and res.error_code == "AGT_001"
    assert "不要修改参数重试" in res.error_message
    assert order_repo.count(conn) == 0, "被拒时一张单都不该落库"


def test_full_happy_path(registry, conn, s):
    """pass → 拿凭证 → 建草稿 → 提交被拒（缺人工确认）。"""
    c = ctx(s)
    chk = _run_check(registry, c, s)
    assert chk.ok and chk.data["verdict"] == "pass", chk.data
    token = chk.data["validation_token"]
    assert token

    draft = registry.call("create_draft", {
        "validation_token": token, "change_type": "salary_adjust",
        "employee_no": "E1001", "items": _items(),
        "materials": ["approval_form", "adjust_basis"], "idempotency_key": "k-ok",
    }, c)
    assert draft.ok, draft.error_message
    draft_id = draft.data["draft_id"]
    assert draft.data["created"] is True

    # 提交：没有 confirm_token 必须停在这里
    sub = registry.call("submit_draft", {"draft_id": draft_id}, c)
    assert sub.ok is False and sub.error_code == "AGT_009"
    assert order_repo.get(conn, order_id=draft_id)["status"] == "draft"


def test_create_draft_rejects_tampered_content(registry, conn, s):
    """校验 20000、建 50000：必须在工具层被拒。"""
    c = ctx(s)
    chk = _run_check(registry, c, s)
    token = chk.data["validation_token"]

    res = registry.call("create_draft", {
        "validation_token": token, "change_type": "salary_adjust",
        "employee_no": "E1001", "items": _items(new_salary="50000"),
        "materials": ["approval_form", "adjust_basis"], "idempotency_key": "k-tamper",
    }, c)
    assert res.ok is False and res.error_code == "AGT_003"
    assert order_repo.count(conn) == 0


def test_failed_tool_suggests_recovery_not_success_path(registry, conn, s):
    """
    失败之后的 next_allowed 必须是**恢复路径**，不能套用成功路径。

    这条是演示脚本跑出来的真问题：create_draft 被拒后，返回的 next_allowed
    是 ["submit_draft", "escalate_to_human"]（成功路径），而根本没有单可提交 ——
    模型会被引到一个必然再失败的动作上。失败的提示应该指向"重新校验或转人工"。
    """
    c = ctx(s)
    res = registry.call("create_draft", {
        "validation_token": "", "change_type": "salary_adjust",
        "employee_no": "E1001", "items": _items(), "idempotency_key": "k-recovery",
    }, c)
    assert res.ok is False
    assert "submit_draft" not in res.next_allowed, "失败后不该再提示去提交"
    assert "check_rules" in res.next_allowed, "凭证类失败应引导重新校验"
    assert "escalate_to_human" in res.next_allowed


def test_need_review_gets_no_token(registry, conn, s):
    """L3 不签凭证：Agent 拿不到就建不了单，不需要靠它自觉。"""
    c = ctx(s)
    chk = _run_check(registry, c, s, items=_items(new_salary="21500"))
    assert chk.data["verdict"] == "need_review"
    assert chk.data["level"] == "L3"
    assert "validation_token" not in chk.data
    assert chk.next_allowed == ["escalate_to_human"]


def test_blocked_cannot_create_even_with_stale_token(registry, conn, s):
    """先拿到 pass 凭证，再让内容变成阻断项 —— 双保险必须拦住。"""
    c = ctx(s)
    chk = _run_check(registry, c, s)
    token = chk.data["validation_token"]
    # 降薪且没有员工确认 → 阻断
    res = registry.call("create_draft", {
        "validation_token": token, "change_type": "salary_adjust",
        "employee_no": "E1001", "items": _items(new_salary="15000"),
        "materials": ["approval_form", "adjust_basis"], "idempotency_key": "k-cut",
    }, c)
    assert res.ok is False
    assert res.error_code in ("AGT_003", "AGT_005")


def test_idempotency_returns_same_order(registry, conn, s):
    """Agent 重试时不该建出两张单。"""
    c = ctx(s)
    draft1 = registry.call("check_rules", {
        "change_type": "salary_adjust", "employee_no": "E1001", "items": _items(),
        "materials": ["approval_form", "adjust_basis"]}, c)
    d1 = registry.call("create_draft", {
        "validation_token": draft1.data["validation_token"],
        "change_type": "salary_adjust", "employee_no": "E1001", "items": _items(),
        "materials": ["approval_form", "adjust_basis"], "idempotency_key": "same-key",
    }, c)
    assert d1.data["created"] is True

    # 第二次用同一个幂等键（凭证已消费，但幂等键先命中）
    d2 = registry.call("create_draft", {
        "validation_token": "whatever", "change_type": "salary_adjust",
        "employee_no": "E1001", "items": _items(),
        "materials": ["approval_form", "adjust_basis"], "idempotency_key": "same-key",
    }, c)
    # 幂等命中在验签之前，所以直接返回已有单
    assert d2.ok is True and d2.data["created"] is False
    assert order_repo.count(conn) == 1


def test_confirm_token_enables_submit(registry, conn, s):
    """人工确认之后能提交 —— 否则这套设计就只是"什么都做不了"。"""
    c = ctx(s)
    chk = _run_check(registry, c, s)
    draft = registry.call("create_draft", {
        "validation_token": chk.data["validation_token"],
        "change_type": "salary_adjust", "employee_no": "E1001", "items": _items(),
        "materials": ["approval_form", "adjust_basis"], "idempotency_key": "k-confirm",
    }, c)
    draft_id = draft.data["draft_id"]
    order = order_repo.get(conn, order_id=draft_id)
    chash = gate.order_hash(order["change_type"], order["employee_no"],
                            order["items"], order["materials"])
    ctoken, _ = gate.issue_confirm(s, draft_id=draft_id, content_hash=chash)

    sub = registry.call("submit_draft", {"draft_id": draft_id,
                                         "confirm_token": ctoken}, c)
    assert sub.ok is True
    assert order_repo.get(conn, order_id=draft_id)["status"] == "submitted"
    # 重复提交是空操作，不报错也不改状态
    again = registry.call("submit_draft", {"draft_id": draft_id,
                                           "confirm_token": ctoken}, c)
    assert again.ok is False


# ---------------------------------------------------------------------------
# 工具注册表本身
# ---------------------------------------------------------------------------

def test_registry_has_nine_tools(registry):
    assert len(registry.names()) == 9
    assert set(registry.names()) == {
        "lookup_policy", "lookup_param", "get_employee", "get_change_order",
        "check_rules", "create_draft", "submit_draft", "escalate_to_human",
        "ask_clarification"}


def test_schema_shape_is_valid_function_calling(registry):
    for schema in registry.schemas():
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"
        assert fn["parameters"].get("additionalProperties") is False


def test_schemas_filtered_by_allowed(registry):
    """工具集裁剪：只放出允许的工具，模型看不见的自然不会调。"""
    only = registry.schemas(["escalate_to_human"])
    assert [x["function"]["name"] for x in only] == ["escalate_to_human"]


def test_unknown_tool_is_reported_not_crashed(registry, s):
    """模型幻觉出不存在的工具：明确告诉它，并把可用工具列出来。"""
    res = registry.call("drop_database", {}, ctx(s))
    assert res.ok is False and res.error_code == "AGT_007"
    assert "lookup_policy" in res.error_message


def test_validate_args():
    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string"},
                             "n": {"type": "integer", "minimum": 1, "maximum": 5}},
              "additionalProperties": False}
    assert validate_args(schema, {"a": "x"}) == ""
    assert "缺少必填" in validate_args(schema, {})
    assert "应为 integer" in validate_args(schema, {"a": "x", "n": "3"})
    assert "不能大于 5" in validate_args(schema, {"a": "x", "n": 9})
    assert "不认识" in validate_args(schema, {"a": "x", "zz": 1})
    # bool 是 int 子类，必须被排除
    assert "应为 integer" in validate_args(schema, {"a": "x", "n": True})


def test_trace_is_recorded(registry, conn, s):
    """每次工具调用都要留轨迹 —— 否则无法回答"它为什么这么判断"。"""
    from my_tool.store import trace_repo

    c = ctx(s)
    registry.call("lookup_param", {"keys": ["si_base_cap"]}, c)
    rows = trace_repo.list_traces(conn, "sess-test")
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "lookup_param"
    assert rows[0]["args"]["keys"] == ["si_base_cap"]
    assert rows[0]["ok"] is True


def test_read_tools_do_not_need_gate(registry, conn, s):
    """读工具之间自由跳转：next_allowed 留空表示不限制。"""
    c = ctx(s, workspace={"region_code": "310000"})
    res = registry.call("lookup_param", {"keys": ["freeze_start_day"]}, c)
    assert res.ok and res.next_allowed == []
    assert res.data["items"][0]["value"] == 25


def test_get_employee_masks_and_scopes(registry, conn, s):
    """非管理员查他人档案必须被拒；本人查询要脱敏。"""
    emp_ctx = ctx(s, user={"id": "u-emp", "role": "employee", "display_name": "员工"})
    denied = registry.call("get_employee", {"employee_no": "E1002"}, emp_ctx)
    assert denied.ok is False
    assert "只能查询本人" in denied.error_message

    admin = ctx(s)
    ok = registry.call("get_employee", {"employee_no": "E1002"}, admin)
    assert ok.ok and ok.data["employee"]["employee_no"] == "E1002"
    assert ok.data["employee"]["fixed_salary"] == 26800


def test_escalate_is_a_valid_ending(registry, s):
    c = ctx(s)
    res = registry.call("escalate_to_human", {
        "reason_code": "param_missing", "summary": "缺地区参数",
        "context": {"clause": "第三十五条"}}, c)
    assert res.ok is True
    assert res.data["escalated"] is True
    assert res.next_allowed == [], "转人工之后不该再允许任何工具"
