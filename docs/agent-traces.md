# Agent 决策轨迹说明与 3 个典型流程

本文说明"每轮决策记录到日志表"的落法，并给出 3 个典型对话流程的预期轨迹。

相关代码：`core/agent/loop.py`（循环）、`store/trace_repo.py`（日志）、
`store/db.py` 的 `agent_runs` / `agent_traces` 两张表。

---

## 0. 两张日志表的分工

| 表 | 记录什么 | 频率 | 关键字段 |
|---|---|---|---|
| `agent_runs` | **模型那一步**：它说了什么、选了哪些工具、花了多少 token | 每轮一条 | `turn_no, thought, chose_tools, tokens_in/out, finish_reason` |
| `agent_traces` | **工具那一步**：参数、结果摘要、耗时 | 每次工具调用一条 | `tool_name, args, result.summary, ok, latency_ms` |

两者按 `(session_id, turn_no)` 对齐，`trace_repo.replay()` 可拼成一条时间线。
分两张而不是一张，是因为"模型想做什么"和"工具实际做了什么"可能不一致
（模型选了一个不存在的工具、或跳过校验想直接建单）——分开才看得见这种偏差。

**循环上限**：每轮一次模型生成，最多 **5 轮**（`AGENT_MAX_TURNS`，可配）。
触顶或超预算不报错，降级到固定 RAG。

---

## 1. 轨迹一：正常调薪（建单前被拦截）

**对话**：员工 "帮我把固定薪资从 18500 调到 19000，12 月 1 日生效"

| 轮 | 模型决策（agent_runs.thought） | 工具调用（agent_traces） | 结果 |
|---|---|---|---|
| 1 | "先查员工档案确认职级与参保地" | `get_employee(E1001)` | ✓ E1001 爱丽丝，region=310000 |
| 2 | "查调薪红线与分级阈值" | `lookup_param([raise_cap_pct, diff_l2_max…], region=310000)` | ✓ 2 个参数 |
| 3 | "跑规则校验，材料已齐" | `check_rules(salary_adjust, items=[18500→19000], materials=[approval_form, adjust_basis])` | ✓ pass L2，签发 validation_token |
| 4 | "建草稿" | `create_draft(validation_token=…)` | ⛔ **被拦截**，不落库 |

**第 4 轮的拦截**：循环检测到 `create_draft` 标了 `requires_human_confirm`，
且模型带了合法凭证，于是在真正执行前强制中断——把草稿预览流式发给员工
（SSE 事件 `confirmation_required`），状态置 `awaiting_confirmation`，
**change_orders 里一张单都没有**。

**员工确认后**：前端调 `POST /api/agent/confirm-draft`（携带预览里回传的
validation_token），复用 create_draft 的五道闸门落库，返回单号 `CC…`。

日志表最终形态：

```
agent_runs:  turn=1 thought="先查员工档案…" chose_tools=[get_employee] finish_reason=tool_calls
             turn=2 thought="查调薪红线…"    chose_tools=[lookup_param]   …
             turn=3 thought="跑规则校验…"    chose_tools=[check_rules]    …
             turn=4 thought="建草稿"        chose_tools=[create_draft]   …（拦截在 trace 层）
agent_traces: get_employee → ✓ / lookup_param → ✓ / check_rules → ✓ / create_draft → "已拦截：等待员工确认草稿"
```

---

## 2. 轨迹二：信息缺失（先澄清，不猜）

**对话**：员工 "帮我改一下银行卡"

| 轮 | 模型决策 | 工具调用 | 结果 |
|---|---|---|---|
| 1 | "缺账号和生效日，不能猜，先问" | `ask_clarification([{field:bank_account}, {field:effective_date}])` | ✓ 结束本轮，向员工提问 |

**员工补充** "改成 6217…，下月 1 号生效" 后继续：

| 轮 | 模型决策 | 工具调用 | 结果 |
|---|---|---|---|
| 2 | "查档案：30 日内改过几次、入职日" | `get_employee(E1001)` | ✓ |
| 3 | "查账户变更的校验规则与材料" | `lookup_policy(银行账户变更 双人复核)` | ✓ 第二十二/二十三条 |
| 4 | "跑校验" | `check_rules(bank_account, …)` | 按结果分叉：pass → 拦截建单 / block → 转人工 |

**要点**：模型没有编造账号或生效日。缺信息时走 `ask_clarification`，而不是用
"合理猜测"继续——这是提示词里"信息不足时提问，不要猜"的落点。

---

## 3. 轨迹三：降薪无书面确认（阻断，转人工）

**对话**：员工 "把 E1001 薪资降到 15000"

| 轮 | 模型决策 | 工具调用 | 结果 |
|---|---|---|---|
| 1 | "查档案" | `get_employee(E1001)` | ✓ |
| 2 | "跑校验" | `check_rules(salary_adjust, 18500→15000, materials=[approval_form, adjust_basis])` | ⛔ block：`BLK_SALARY_CUT_NO_CONSENT`（第二十七条） |
| 3 | "存在 final 阻断，转人工" | `escalate_to_human(reason=blocked_final, summary=降薪无书面确认, context={clause:第二十七条})` | ✓ 结束 |

**要点**：
- `check_rules` 返回 block 后，`next_allowed` 收窄为 `[escalate_to_human, ask_clarification]`，
  模型下一轮看不到 `create_draft`——即使它想建单也调不了。
- 转人工是**正常收尾**，不是失败。模型把阻断码和条款号带进 `context`，
  人工接手不必从零查。

日志表：

```
agent_runs:  turn=1 … chose_tools=[get_employee]
             turn=2 … chose_tools=[check_rules]
             turn=3 … chose_tools=[escalate_to_human] finish_reason=tool_calls
agent_traces: get_employee → ✓ / check_rules → ✗ block 1 项 / escalate_to_human → ✓
```

---

## 4. 决策日志怎么查

```bash
# 单条会话的完整时间线（决策 + 工具调用，按 turn/seq 排序）
python -c "
import sys; sys.path.insert(0,'src')
from my_tool.core.config import Settings
from my_tool.store import db, trace_repo
s = Settings.from_env()
c = db.connect(s.chat_db_path)
for item in trace_repo.replay(c, '<session_id>'):
    print(item['kind'], item['turn'], item.get('tool_name') or item.get('thought'))
"

# 只看模型每轮选了什么工具
python -c "
import sys; sys.path.insert(0,'src')
from my_tool.core.config import Settings
from my_tool.store import db, trace_repo
s = Settings.from_env()
c = db.connect(s.chat_db_path)
for r in trace_repo.list_runs(c, '<session_id>'):
    print(r['turn'], r['chose_tools'], r['finish_reason'], r['tokens_in']+r['tokens_out'])
"
```

阶段 5 的分析（"它为什么这么判断"）就建立在这两份日志上：
`agent_runs` 回答"模型想做什么"，`agent_traces` 回答"系统实际做了什么"，
两者对不上之处，正是需要调提示词或补规则的地方。
