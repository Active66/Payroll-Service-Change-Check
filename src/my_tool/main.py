"""命令行入口。作为"壳"存在：解析参数、装配组件、打印结果，不含业务逻辑。

    payroll-check build     建索引
    payroll-check ask "..." 单次提问
    payroll-check chat      交互问答
    payroll-check serve     启动 Web 服务（员工问答入口）
    payroll-check eval      检索评测
    payroll-check info      查看配置与索引状态
"""

import argparse
import json
import secrets

from .core.config import Settings
from .core.llm import build_chat_model
from .core.orchestrator import Orchestrator, RAGAnswer
from .core.pipeline import build_bm25, build_index, load_index
from .core.retrieval.embedding import build_embedding_model
from .core.retrieval.vector_store import VectorStore
from .evaluation import run_eval
from .utils.console import SEP, _init_console, log, section
from .utils.env import HAS_JIEBA, faiss, np


def render_answer(res: RAGAnswer, show_debug: bool = False) -> None:
    """把结果打印成人能读的样子：答案 + 引用 + 护栏结论。"""
    print()
    print(SEP)
    print(f"问题：{res.question}")
    print(SEP)
    print(res.answer)
    print()
    print("引用来源：")
    if not res.citations:
        print("  （无）")
    for c in res.citations:
        hit = next((h for h in res.hits if h.chunk.chunk_id == c.chunk_id), None)
        ch = hit.channels if hit else "-"
        print(f"  [{c.index}] {c.chunk_id}  {c.clause_no or '-'}  {ch}  "
              f"score={c.score:.5f}")
        print(f"      {c.heading_path}")
    print()
    g = res.guard
    if g:
        flag = "通过" if g.passed else "需关注"
        print(f"护栏：{flag}｜状态={g.status}｜引用 {g.cited_indexes or '无'}"
              + (f"｜未溯源数字 {g.unverified_numbers}" if g.unverified_numbers else ""))
        for w in g.warnings:
            print(f"  ! {w}")
    print(f"耗时 {res.latency_ms} ms｜模型 {res.model}｜prompt {res.prompt_version}｜"
          f"tokens in/out {res.tokens_in}/{res.tokens_out}")
    if show_debug:
        print("\n检索调试：")
        print(json.dumps(res.debug, ensure_ascii=False, indent=2))
        print("Top-K 明细：")
        for i, h in enumerate(res.hits, start=1):
            print(f"  [{i}] {h.chunk.chunk_id} fused={h.fused_score:.6f} "
                  f"vec={h.vector_score:.4f}(#{h.vector_rank}) "
                  f"bm25={h.bm25_score:.3f}(#{h.bm25_rank}) {h.chunk.clause_no}")
    print(SEP)

def cmd_chat(orch: Orchestrator, show_debug: bool) -> None:
    """交互式问答。支持 /sources 查看上一条完整原文、/rebuild 重建索引。"""
    print("\n薪酬变更核对制度 · 问答原型（阶段 1）")
    print("输入问题回车即可；/sources 看完整原文，/debug 切换调试信息，/quit 退出")
    last: RAGAnswer | None = None
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in ("/quit", "/exit", "q"):
            break
        if q == "/debug":
            show_debug = not show_debug
            print(f"调试信息：{'开' if show_debug else '关'}")
            continue
        if q == "/sources" and last:
            for i, h in enumerate(last.hits, start=1):
                print(f"\n[{i}] {h.chunk.chunk_id} {h.chunk.heading_path}")
                print(h.chunk.content)
            continue
        last = orch.answer(q)
        render_answer(last, show_debug=show_debug)

def cmd_user(args: argparse.Namespace, s: Settings) -> int:
    """
    账号管理（本期不做用户管理页面，两条角色撑不起一套 CRUD 界面）。

    密码可以显式传，也可以留空由系统随机生成 —— 后者更适合"给同事开个号"：
    管理员不必自己想密码，也就不会全公司用同一个。
    """
    from .core import security
    from .store import db, user_repo

    db.init_db(s.chat_db_path)
    conn = db.connect(s.chat_db_path)
    try:
        if args.action == "list":
            rows = user_repo.list_users(conn)
            if not rows:
                log("还没有任何账号。用 payroll-check user add 创建第一个管理员。", "warn")
                return 0
            print(f"\n{'用户名':<16}{'姓名':<12}{'角色':<10}{'状态':<8}{'最近登录':<22}工号")
            print("-" * 84)
            for r in rows:
                print(f"{r['username']:<16}{(r['display_name'] or ''):<12}{r['role']:<10}"
                      f"{r['status']:<8}{(r['last_login_at'] or '—'):<22}{r['employee_no'] or ''}")
            print()
            return 0

        if args.action == "add":
            if user_repo.get_by_username(conn, args.username):
                log(f"用户名已存在：{args.username}", "err")
                return 1
            password = args.password or secrets.token_urlsafe(9)
            ok, reason = security.is_strong_password(password)
            if not ok:
                log(f"密码不符合要求：{reason}", "err")
                return 1
            user_repo.create_user(
                conn, username=args.username, display_name=args.name or args.username,
                role=args.role, password_hash=security.hash_password(password),
                employee_no=args.employee_no, department=args.department,
            )
            log(f"已创建 {args.role}：{args.username}", "ok")
            if not args.password:
                log(f"初始密码：{password}（仅显示这一次，请转告本人后让其修改）", "warn")
            return 0

        if args.action == "reset":
            row = user_repo.get_by_username(conn, args.username)
            if row is None:
                log(f"找不到用户：{args.username}", "err")
                return 1
            password = args.password or secrets.token_urlsafe(9)
            ok, reason = security.is_strong_password(password)
            if not ok:
                log(f"密码不符合要求：{reason}", "err")
                return 1
            user_repo.update_password(conn, row["id"], security.hash_password(password))
            log(f"已重置 {args.username} 的密码", "ok")
            if not args.password:
                log(f"新密码：{password}（仅显示这一次）", "warn")
            return 0
    finally:
        conn.close()
    return 0


def cmd_org(args: argparse.Namespace, s: Settings) -> int:
    """
    部门与 API Key 管理。

    这两件事都只有几步操作，做页面不划算（同 user 命令的取舍），
    但都必须能自助完成 —— 否则"新来一个部门要改代码上线"就太荒唐了。
    """
    from .store import api_key_repo, db, org_repo, user_repo

    db.init_db(s.chat_db_path)
    conn = db.connect(s.chat_db_path)
    try:
        # ---------- dept ----------
        if args.cmd == "dept":
            if args.action == "list":
                nodes = org_repo.tree(conn)
                if not nodes:
                    log("还没有部门。用 payroll-check dept add --name 华东区域 创建。", "warn")
                    return 0
                for n in nodes:
                    print(f"- {n['name']}  ({n['code'] or '无编码'})  id={n['id'][:8]}")
                    for c in n["children"]:
                        print(f"    ├─ {c['name']}  ({c['code'] or '无编码'})  id={c['id'][:8]}")
                print()
                return 0

            if args.action == "add":
                parent_id = None
                if args.parent:
                    parent = _resolve_dept(conn, org_repo, args.parent)
                    if parent is None:
                        log(f"找不到上级部门：{args.parent}", "err")
                        return 1
                    parent_id = parent["id"]
                dept = org_repo.create_department(
                    conn, name=args.name, parent_id=parent_id, code=args.code)
                log(f"已创建部门：{args.name}（id={dept['id'][:8]}）", "ok")
                return 0

            if args.action == "assign":
                user = user_repo.get_by_username(conn, args.username)
                if user is None:
                    log(f"找不到用户：{args.username}", "err")
                    return 1
                dept = _resolve_dept(conn, org_repo, args.dept)
                if dept is None:
                    log(f"找不到部门：{args.dept}", "err")
                    return 1
                org_repo.assign_user(conn, user["id"], dept["id"], job_title=args.title)
                log(f"已把 {args.username} 挂到 {dept['name']}", "ok")
                return 0

        # ---------- apikey ----------
        if args.cmd == "apikey":
            if args.action == "list":
                rows = api_key_repo.list_keys(conn)
                if not rows:
                    log("还没有 API Key。用 payroll-check apikey add --name hr-portal 签发。", "warn")
                    return 0
                print(f"\n{'ID':<10}{'名称':<20}{'前缀':<16}{'限额/分':<10}{'状态':<10}最近使用")
                print("-" * 80)
                for r in rows:
                    print(f"{r['id'][:8]:<10}{r['name']:<20}{r['prefix']:<16}"
                          f"{r['rate_limit']:<10}{r['status']:<10}{r['last_used_at'] or '—'}")
                print()
                return 0

            if args.action == "add":
                raw, rec = api_key_repo.create_key(
                    conn, name=args.name, owner=args.owner, rate_limit=args.rate_limit)
                log(f"已签发：{rec['name']}（id={rec['id'][:8]}，{rec['rate_limit']}/分钟）", "ok")
                log(f"Key：{raw}", "warn")
                log("这串只显示这一次，库里只存哈希。丢了就吊销重发。", "warn")
                return 0

            if args.action == "revoke":
                # 支持短 id（列表里显示的就是前 8 位，让运维照抄即可）
                target = next((r for r in api_key_repo.list_keys(conn)
                               if r["id"].startswith(args.id)), None)
                if target is None:
                    log(f"找不到 API Key：{args.id}", "err")
                    return 1
                if api_key_repo.revoke(conn, target["id"]):
                    log(f"已吊销：{target['name']}", "ok")
                    return 0
                log("该 Key 已被吊销", "warn")
                return 0
    finally:
        conn.close()
    return 0


def _resolve_dept(conn, org_repo, ref: str) -> dict | None:
    """按名称或 id（可只给前 8 位）找部门。运维从列表里抄的就是前 8 位。"""
    depts = org_repo.list_departments(conn)
    for d in depts:
        if d["id"] == ref or d["name"] == ref or d["id"].startswith(ref):
            return d
    return None


def cmd_data(args: argparse.Namespace, s: Settings) -> int:
    """制度参数与人事档案。这两张表是规则引擎的输入，坏了所有判定都会错。"""
    from .core.agent import seed
    from .store import db, param_repo

    db.init_db(s.chat_db_path)
    conn = db.connect(s.chat_db_path)
    try:
        if args.action == "seed":
            n = seed.seed_params(conn)
            m = seed.seed_employees(conn)
            log(f"参数 {n} 条、档案 {m} 条（幂等，已存在的会被覆盖为种子值）", "ok")
            return 0

        if args.action == "params":
            rows = param_repo.list_params(conn)
            if not rows:
                log("参数表为空。先执行：payroll-check data seed", "warn")
                return 0
            print(f"\n{'参数名':<24}{'取值':<12}{'单位':<10}{'地区':<8}出处")
            print("-" * 84)
            for r in rows:
                print(f"{r['param_key']:<24}{r['value']:<12}{r['unit']:<10}"
                      f"{(r['region_code'] or '全国'):<8}{r['source_clause']}")
            print()
            return 0

        if args.action == "employees":
            rows = conn.execute(
                "SELECT * FROM employees ORDER BY employee_no").fetchall()
            if not rows:
                log("档案表为空。生产环境应接 HR 主数据，演示可用 data seed", "warn")
                return 0
            print(f"\n{'工号':<10}{'姓名':<10}{'入职日':<14}{'职级':<8}"
                  f"{'固定薪资':<12}{'参保地':<10}法人")
            print("-" * 88)
            for r in rows:
                flag = "" if r["legal_entity_id"] else "  ← 缺法人归属（会被阻断）"
                print(f"{r['employee_no']:<10}{r['display_name']:<10}"
                      f"{(r['hire_date'] or '—'):<14}{(r['grade'] or '—'):<8}"
                      f"{(r['fixed_salary'] or 0):<12.0f}{(r['region_code'] or '—'):<10}"
                      f"{r['legal_entity_id'] or '—'}{flag}")
            print()
            return 0
    finally:
        conn.close()
    return 0


def cmd_check(args: argparse.Namespace, s: Settings) -> int:
    """
    命令行跑一次规则校验。

    存在的意义：不用起服务、不用调模型，就能验证"这套规则判得对不对"。
    规则改完之后第一件事就是拿几个真实案例跑一遍这个命令。
    """
    import json as _json
    from datetime import date as _date

    from .core.agent import rules
    from .store import db

    db.init_db(s.chat_db_path)
    as_of = None
    if args.as_of:
        try:
            as_of = _date.fromisoformat(args.as_of)
        except ValueError:
            log(f"--as-of 格式不对：{args.as_of}（应为 YYYY-MM-DD）", "err")
            return 1

    items = [{"field": args.field, "old_value": args.old or "", "new_value": args.new,
              "effective_date": args.effective or ""}]
    materials = [m.strip() for m in args.materials.split(",") if m.strip()]

    conn = db.connect(s.chat_db_path)
    try:
        v = rules.check(conn, change_type=args.type, employee_no=args.emp,
                        items=items, materials=materials, as_of=as_of,
                        legal_entity_id=s.legal_entity_id)
    finally:
        conn.close()

    if args.json:
        print(_json.dumps(v.as_dict(), ensure_ascii=False, indent=2))
        return 0 if v.verdict == "pass" else 1

    mark = {"pass": "✓ 通过", "need_review": "! 需人工复核",
            "block": "× 阻断"}[v.verdict]
    log(f"判定：{mark}　风险等级：{v.level or '—'}", 
        "ok" if v.verdict == "pass" else ("warn" if v.verdict == "need_review" else "err"))

    if v.blocking:
        print("\n阻断项（第二十七条，不得人工放行）：")
        for b in v.blocking:
            print(f"  [{b.code}] {b.clause}　{b.message}")
    if v.warnings:
        print("\n提示项（不阻断，但会影响时效或审批层级）：")
        for w in v.warnings:
            print(f"  [{w.code}] {w.clause}　{w.message}")
    if v.required_materials:
        print("\n必备材料：")
        for m in v.required_materials:
            print(f"  {'✓' if m['present'] else '×'} {m['name']}（{m['code']}）")
    print()
    return 0 if v.verdict == "pass" else 1


def cmd_agent(args: argparse.Namespace, s: Settings) -> int:
    """
    跑一次 Agent 循环。

    默认用**脚本化假模型**：它不思考，只按剧本返回工具调用。
    这样能在不接大模型的情况下看到完整链路 —— 循环、工具集收窄、
    闸门拒绝、预算刹车、降级，全都是真的，只有"决策"是假的。

    想换真模型：把 LLM_PROVIDER 设成 openai_compat 并配好 base_url 与 key，
    本命令会自动改用它（见 _build_agent_model）。
    """
    from .core.agent import seed
    from .core.agent.loop import AgentLoop, LoopEvent
    from .core.agent.registry import ToolContext
    from .core.agent.toolset import build_registry
    from .core.llm.mock_agent import (MockToolCallingModel, script_bad_args,
                                      script_happy_path, script_infinite_retry,
                                      script_skip_gate)
    from .store import db

    db.init_db(s.chat_db_path)
    conn = db.connect(s.chat_db_path)
    try:
        seed.seed_params(conn)
        seed.seed_employees(conn)
    finally:
        conn.close()

    scripts = {
        "happy": script_happy_path,
        "skip": script_skip_gate,
        "loop": script_infinite_retry,
        "badargs": script_bad_args,
    }
    # 模型选择：mock provider 用脚本化假 Agent（离线演示）；
    # 否则用真实兼容实现 —— 配了 LLM_PROVIDER=deepseek 等就直接走真模型
    if s.llm_provider == "mock":
        model = MockToolCallingModel(scripts[args.script](args.emp))
        log(f"模型：{model.model}（剧本 {args.script}）", "warn")
    else:
        from .core.llm import build_chat_model
        model = build_chat_model(s)
        log(f"模型：{model.provider}/{model.model}（真实 function calling）", "ok")

    # 尝试加载索引：Agent 的 lookup_policy 与降级通道都需要它。
    # 加载失败不阻塞 —— 模型会在 lookup_policy 报"索引未就绪"时转人工
    retriever = None
    fallback = None
    try:
        from .core.llm import build_chat_model as _bcm
        from .core.orchestrator import Orchestrator
        from .core.pipeline import build_bm25, load_index
        from .core.retrieval.embedding import build_embedding_model

        store, _ = load_index(s)
        orch = Orchestrator(store, build_bm25(store), build_embedding_model(s),
                            _bcm(s), s)
        retriever = orch.retriever
        fallback = orch
        log("索引已加载，lookup_policy 与降级通道可用", "ok")
    except Exception as ex:                        # noqa: BLE001
        log(f"索引未加载（{type(ex).__name__}），lookup_policy 不可用；先执行 build", "warn")

    loop = AgentLoop(registry=build_registry(), model=model, settings=s,
                     retriever=retriever, fallback=fallback)
    ctx = ToolContext(settings=s,
                      user={"id": "cli", "role": "admin", "display_name": "CLI"},
                      session_id="cli-session", retriever=retriever)

    def on_event(ev: LoopEvent) -> None:
        if ev.kind == "start":
            print(f"\n用户：{ev.data['question']}\n")
        elif ev.kind == "tool_call":
            print(f"  → 调 {ev.data['name']}({_brief(ev.data['args'])})")
        elif ev.kind == "tool_result":
            d = ev.data
            mark = "✓" if d["ok"] else "✗"
            tail = "" if d["ok"] else f"  [{d['error_code']}]"
            nxt = f" → 下一步：{'/'.join(d['next_allowed'])}" if d["next_allowed"] else ""
            print(f"  {mark} {d['summary']}{tail}{nxt}")
        elif ev.kind == "thought" and ev.data.get("text"):
            print(f"  「{ev.data['text'][:60]}」")
        elif ev.kind == "confirmation_required":
            d = ev.data.get("draft") or {}
            print("\n  ┌─ 草稿待确认（已拦截，未建单）")
            print(f"  │ 员工 {d.get('employee_name') or d.get('employee_no')}"
                  f" · {d.get('change_type')} · 风险 {d.get('risk_level') or '—'}")
            for it in (d.get("items") or []):
                eff = f"（{it.get('effective_date')} 生效）" if it.get("effective_date") else ""
                print(f"  │ {it.get('field')}: {it.get('old_value') or '—'} → "
                      f"{it.get('new_value') or '—'}{eff}")
            for w in (d.get("warnings") or []):
                print(f"  │ ⚠ {w.get('message')}")
            print("  └─ 员工确认后由 /api/agent/confirm-draft 落库")
        elif ev.kind == "error":
            print(f"  ! {ev.data['message']}")

    res = loop.run(args.question, ctx=ctx, on_event=on_event)

    print(f"\n助手：{res.answer}")
    if res.citations:
        print("\n引用：")
        for c in res.citations:
            print(f"  [{c['index']}] {c['doc_title']} {c['clause_no']}")
    meta = [f"状态 {res.status}", f"{res.turns} 轮",
            f"{len(res.tool_calls)} 次工具调用", f"{res.latency_ms}ms"]
    if res.degraded:
        meta.append(f"已降级（{res.degrade_reason}）")
    if res.order_no:
        meta.append(f"单号 {res.order_no}")
    print(f"\n（{' · '.join(meta)}）\n")
    return 0


def _brief(args: dict) -> str:
    """把工具参数压成一行，便于在终端看。"""
    parts = []
    for k, v in list(args.items())[:3]:
        s = str(v)
        if len(s) > 28:
            s = s[:26] + "…"
        parts.append(f"{k}={s}")
    return ", ".join(parts) + ("…" if len(args) > 3 else "")


def main(argv: list[str] | None = None) -> int:
    _init_console()
    parser = argparse.ArgumentParser(
        description="薪酬变更核对系统 · 阶段 1 RAG 最小原型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  payroll-check build\n"
               "  payroll-check ask \"冻结期内能改银行账户吗\"\n"
               "  payroll-check eval",
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("build", help="解析制度 → 切片 → 向量化 → 写入 FAISS")
    p_ask = sub.add_parser("ask", help="单次提问")
    p_ask.add_argument("question", nargs="+", help="问题文本")
    p_ask.add_argument("--debug", action="store_true", help="打印检索调试信息")
    p_ask.add_argument("--top-k", type=int, default=None, help="召回条数，默认 3")
    p_chat = sub.add_parser("chat", help="交互式问答")
    p_chat.add_argument("--debug", action="store_true")
    p_serve = sub.add_parser("serve", help="启动 Web 服务（员工问答入口）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true", help="开发模式：改代码自动重载")
    sub.add_parser("eval", help="跑内置检索评测")
    sub.add_parser("info", help="查看当前配置与索引状态")

    p_user = sub.add_parser("user", help="账号管理（管理后台用）")
    user_sub = p_user.add_subparsers(dest="action", required=True)
    p_u_add = user_sub.add_parser("add", help="新建账号")
    p_u_add.add_argument("--username", required=True)
    p_u_add.add_argument("--name", default="", help="显示姓名")
    p_u_add.add_argument("--role", choices=("employee", "admin"), default="employee")
    p_u_add.add_argument("--password", default="", help="留空则随机生成并打印")
    p_u_add.add_argument("--employee-no", default=None, help="工号")
    p_u_add.add_argument("--department", default=None, help="部门")
    user_sub.add_parser("list", help="列出账号")
    p_u_reset = user_sub.add_parser("reset", help="重置密码")
    p_u_reset.add_argument("--username", required=True)
    p_u_reset.add_argument("--password", default="", help="留空则随机生成并打印")

    # —— 部门（两级组织）——
    p_dept = sub.add_parser("dept", help="部门管理（问答按部门统计用）")
    dept_sub = p_dept.add_subparsers(dest="action", required=True)
    p_d_add = dept_sub.add_parser("add", help="新建部门")
    p_d_add.add_argument("--name", required=True)
    p_d_add.add_argument("--parent", default="", help="上级部门名或 id；留空为一级部门")
    p_d_add.add_argument("--code", default="", help="部门编码（可选）")
    dept_sub.add_parser("list", help="列出部门（树形）")
    p_d_assign = dept_sub.add_parser("assign", help="把员工挂到部门")
    p_d_assign.add_argument("--username", required=True)
    p_d_assign.add_argument("--dept", required=True, help="部门名或 id")
    p_d_assign.add_argument("--title", default=None, help="岗位（不传则不改）")

    # —— 外部集成 API Key ——
    p_key = sub.add_parser("apikey", help="外部系统 API Key（/api/v1/* 用）")
    key_sub = p_key.add_subparsers(dest="action", required=True)
    p_k_add = key_sub.add_parser("add", help="签发 Key（明文只打印一次）")
    p_k_add.add_argument("--name", required=True, help="用途，如 hr-portal")
    p_k_add.add_argument("--owner", default="", help="负责人")
    p_k_add.add_argument("--rate-limit", type=int, default=60, help="每分钟请求上限")
    key_sub.add_parser("list", help="列出 Key（只显示前缀）")
    p_k_revoke = key_sub.add_parser("revoke", help="吊销 Key")
    p_k_revoke.add_argument("--id", required=True)

    # —— 制度参数与人事档案（Agent 的数据底座）——
    p_data = sub.add_parser("data", help="制度参数与人事档案（Agent 规则引擎的数据来源）")
    data_sub = p_data.add_subparsers(dest="action", required=True)
    data_sub.add_parser("seed", help="写入参数与档案种子（幂等，只补不改）")
    data_sub.add_parser("params", help="列出制度参数")
    data_sub.add_parser("employees", help="列出人事档案")

    # —— 规则校验（不起服务就能验证闸门）——
    p_check = sub.add_parser("check", help="对一份变更跑规则校验，看到阻断项与风险等级")
    p_check.add_argument("--type", required=True, choices=(
        "bank_account", "si_base", "hf_base", "salary_adjust",
        "allowance", "address", "tax_special"))
    p_check.add_argument("--emp", required=True, help="工号")
    p_check.add_argument("--field", required=True, help="字段名，如 fixed_salary")
    p_check.add_argument("--old", default="", help="旧值")
    p_check.add_argument("--new", required=True, help="新值")
    p_check.add_argument("--effective", default="", help="生效日 YYYY-MM-DD")
    p_check.add_argument("--materials", default="", help="已上传材料码，逗号分隔")
    p_check.add_argument("--as-of", default="", help="以该日期判定（默认今天）")
    p_check.add_argument("--json", action="store_true", help="输出 JSON")

    # —— Agent 循环（不接模型也能跑，用脚本化假 Agent）——
    p_agent = sub.add_parser("agent", help="跑一次 Agent 循环（默认用脚本化假模型）")
    agent_sub = p_agent.add_subparsers(dest="action", required=True)
    p_a_run = agent_sub.add_parser("run", help="按剧本跑一次完整循环")
    p_a_run.add_argument("question", nargs="?", default="帮我把固定薪资从 18500 调到 19000",
                         help="用户说的话")
    p_a_run.add_argument("--script", default="happy",
                         choices=("happy", "skip", "loop", "badargs"),
                         help="剧本：happy 正常路径 / skip 跳过校验 / loop 无限重试 / badargs 坏参数")
    p_a_run.add_argument("--emp", default="E1001", help="工号")

    args = parser.parse_args(argv)
    s = Settings.from_env()

    # user / dept / apikey / data / check 只碰数据库，不需要索引，所以放在最前面
    if args.cmd == "user":
        return cmd_user(args, s)
    if args.cmd in ("dept", "apikey"):
        return cmd_org(args, s)
    if args.cmd == "data":
        return cmd_data(args, s)
    if args.cmd == "check":
        return cmd_check(args, s)
    if args.cmd == "agent":
        return cmd_agent(args, s)

    if args.cmd == "build":
        # 登记 + 建索引两步都要做：
        # 只建索引不登记，后台一旦重建（比如上传了新制度），这份制度就会
        # 从索引里消失 —— 因为重建只认 policy_documents 这张表。
        from .core.registry import register_document_file
        from .store import db as _db

        _db.init_db(s.chat_db_path)
        register_document_file(s, s.doc_path)
        build_index(s)
        return 0

    if args.cmd == "info":
        section("当前配置")
        print(json.dumps({
            "doc_path": str(s.doc_path),
            "index_dir": str(s.index_dir),
            "chunk_size": s.chunk_size, "chunk_overlap": s.chunk_overlap,
            "chunk_mode": s.chunk_mode,
            "top_k": s.top_k, "recall_k": s.recall_k, "rrf_k": s.rrf_k,
            "llm_provider": s.llm_provider, "llm_model": s.llm_model or "(未配置)",
            "llm_base_url": s.llm_base_url or "(未配置)",
            "embedding_provider": s.embedding_provider, "embedding_dim": s.embedding_dim,
            "as_of": s.as_of_date().isoformat(),
            "jieba": HAS_JIEBA, "faiss": faiss is not None, "numpy": np is not None,
        }, ensure_ascii=False, indent=2))
        mpath = s.index_dir / VectorStore.MANIFEST_FILE
        if mpath.exists():
            print("\n索引 manifest：")
            print(mpath.read_text(encoding="utf-8"))
        else:
            print("\n索引尚未构建。")
        return 0

    # serve 自己会加载索引（失败也不阻塞启动），所以放在"必须索引"的判断之前
    if args.cmd == "serve":
        import uvicorn

        from .api.app import create_app

        print(f"\n员工问答入口：http://{args.host}:{args.port}/   （Ctrl+C 停止）")
        print("接口自检：/healthz   问答：POST /chat（SSE）\n")
        if args.reload:
            # reload 要求传"导入字符串"而非实例，否则每次重载都会用到旧对象
            uvicorn.run("my_tool.api.app:create_app", factory=True,
                        host=args.host, port=args.port, reload=True)
        else:
            uvicorn.run(create_app(s), host=args.host, port=args.port)
        return 0

    # 其余命令都需要索引
    try:
        store, _ = load_index(s)
    except (FileNotFoundError, RuntimeError) as ex:
        log(str(ex), "err")
        log("先执行：payroll-check build", "step")
        return 1

    bm25 = build_bm25(store)
    embedder = build_embedding_model(s)
    chat_model = build_chat_model(s)
    orch = Orchestrator(store, bm25, embedder, chat_model, s)
    log(f"索引就绪：{len(store.chunks)} 条切片｜检索 top{s.top_k}｜生成模型 {chat_model.provider}/{chat_model.model}")

    if args.cmd == "ask":
        res = orch.answer(" ".join(args.question), top_k=args.top_k)
        render_answer(res, show_debug=args.debug)
        return 0

    if args.cmd == "eval":
        run_eval(orch)
        return 0

    # 无子命令 → 进交互模式，降低试用门槛
    cmd_chat(orch, show_debug=getattr(args, "debug", False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
