# Agent 工具链使用说明

面向：实施、运维、要接 Agent 的开发者。

设计理由见 [`agent-design.md`](agent-design.md)；本文只讲**怎么用**与**为什么要这样用**。

> **进度：批次 1–4 全部完成，已接入 DeepSeek 真模型。**
> 工具、规则引擎、闸门、Agent 循环、SSE 接入都齐了；`LLM_PROVIDER=deepseek`（见 `.env`）
> 时由真模型自主决策，`LLM_PROVIDER=mock` 时用脚本化假模型（离线演示）。
> `/chat` 默认仍走固定 RAG，传 `mode=agent` 或设 `AGENT_DEFAULT_MODE=agent` 切到 Agent。

---

## 1. 一分钟验证

不用起服务、不用调模型，三条命令就能看到规则引擎与闸门在工作：

```bash
python -m my_tool data seed          # 写入制度参数与演示人事档案
python -m my_tool data params        # 16 个参数，每个都带条款出处

# 正常调薪（会通过）
python -m my_tool check --type salary_adjust --emp E1001 \
  --field fixed_salary --old 18500 --new 19000 --effective 2026-11-01 \
  --materials approval_form,adjust_basis --as-of 2026-10-10

# 降薪没有书面确认（会被阻断）
python -m my_tool check --type salary_adjust --emp E1001 \
  --field fixed_salary --old 18500 --new 15000 --effective 2026-11-01 \
  --materials approval_form,adjust_basis --as-of 2026-10-10
```

想直接看 Agent 循环跑起来（还是不需要模型）：

```bash
python -m my_tool agent run --script happy     # 正常路径：查→校验→建单→等确认
python -m my_tool agent run --script skip      # 模型跳过校验：被拒 + 标记"结果待复核"
python -m my_tool agent run --script loop      # 模型无限重试：被预算刹住，降级
python -m my_tool agent run --script badargs   # 模型生成坏参数：不崩溃，返回修正提示
```

想看完整的"绕过尝试"演示（包括伪造凭证、校验 A 建 B）：

```bash
python scripts/agent_demo.py
```

它会跑六个场景，最后打出工具调用轨迹，并告诉你"最终落库几张单"。

---

## 2. 制度参数表

### 2.1 为什么数字必须来自这里

条款原文里就写着"上限为 36,921 元/月"，模型完全可以照抄。但照抄有三个问题：

1. **抄错了没人发现**。`guard.py` 只能事后发现"这个数字不在原文里"，
   而 36,921 抄成 36,9210 是"看起来在原文里"的。
2. **答不出"凭什么"**。审计要的是"这个数来自哪一条、哪一版"。
3. **地区覆盖不了**。第三十五条要求以参保地政策为准，同一参数在不同地区取值不同。

所以：**数值只从 `policy_params` 来，每一行都必须有 `source_clause`。**

### 2.2 16 个参数与条款对照

| 参数 | 值 | 出处 |
|---|---|---|
| `si_base_cap` / `si_base_floor` | 36921 / 7384 元/月 | 第十八条 |
| `hf_ratio_min` / `hf_ratio_max` | 5% / 7% | 第十八条 |
| `submit_deadline_day` | 15 日 | 第七条 |
| `freeze_start_day` / `freeze_end_day` | 25 日 / 0（0=月末） | 第八条 |
| `raise_cap_pct` / `cut_cap_pct` | 20% / 15% | 第十二条 |
| `backpay_months_limit` | 3 个月 | 第三十条 |
| `diff_l1_max` / `l2_max` / `l3_max` | 100 / 1000 / 5000 元 | 第二十六条 |
| `bank_change_quota_30d` | 1 次/30 日 | 第二十二条 |
| `verify_pay_min` / `max` | 0.01 / 0.99 元 | 第二十三条 |

种子写在 `src/my_tool/core/agent/seed.py`，**改之前先回去读制度**。

### 2.3 地区优先级

查某地区 → 先取该地区的行 → 没有则回落全国默认（`region_code=''`）→ 都没有返回 `missing`。

`missing` 是给 Agent 的**行动信号**：它必须转人工，不许猜。
这条是第三十五条"参数缺失时不得由人工臆测填值"的直接落地。

---

## 3. 规则引擎

### 3.1 它判什么

| 核对项 | 依据 | 产出 |
|---|---|---|
| 权限与法人归属 | 第二十七条第一项 | `BLK_LEGAL_ENTITY` |
| 证明材料完整性 | 第三十六条、第十四条 | `BLK_MATERIAL` / `BLK_SALARY_CUT_NO_CONSENT` |
| 生效日合法性 | 第九条、第二十七条第五项 | `BLK_EFFECTIVE_BEFORE_HIRE` + 对齐提示 |
| 冻结期 | 第八条 | 提示（不是阻断） |
| 重复提交 | 第六条、第二十七条第六项 | `BLK_DUPLICATE_ITEM` |
| 金额与红线 | 第十二条、第二十二条 | 提示 + 分级输入 |
| 分级 L1–L4 | 第二十六条 | `level` |

### 3.2 三处容易做错的地方

**冻结期不是阻断。** 制度说的是"顺延至下一周期"，不是"拒绝受理"。
把它做成阻断，员工会以为业务办不了，实际只是下个月生效。所以它是 `warning`。
银行账户更特殊：冻结期内**可受理但不参与当期发放**（第八条但书），单独一个提示码。

**L3/L4 不给凭证。** 第二十六条的"处置方式"列写着 L3 要经理审批、L4 要双签。
既然必须人审，Agent 就不该拿到凭证 —— 拿不到就建不了单，不需要靠它自觉。

**降薪用专属阻断码。** 第二十七条把"降薪无员工书面确认"单列，
所以它不该混在 `BLK_MATERIAL` 里 —— 申诉时的举证重点完全不同。

### 3.3 分级边界（含上不含下）

| 差异金额 | 等级 |
|---|---|
| ≤ 100 元 | L1（自动核准） |
| 100 < x ≤ 1000 | L2（专员确认） |
| 1000 < x ≤ 5000，或涉及基数调整 | L3（经理审批） |
| > 5000，或涉及降薪/证件号变更 | L4（经理+合规双签） |

这组边界就是"不该让模型自己判断"的最好理由：差一个数，审批层级就变了，
而模型每次读"1,000 元至 5,000 元"都可能理解成开区间或闭区间。

---

## 4. 闸门：顺序约束怎么保证

### 4.1 四层，从软到硬

| 层 | 机制 | 拦得住什么 | 拦不住什么 |
|---|---|---|---|
| ① 提示词 | system prompt 写清依赖关系 | 正常路径走对 | 注入、用户施压 |
| ② `next_allowed` | 过滤下一轮可用工具 | 大部分跳步 | 判定本身算错 |
| ③ `validation_token` | 签名 + 内容哈希 + 时效 + 一次性 | 伪造、改内容、复用、过期 | 人点的确认 |
| ④ `confirm_token` | 只能由人点击产生 | 模型自行提交 | — |

**只有 ③④ 是安全边界。** ①② 的价值是"减少犯错概率"，不能当防线。

### 4.2 凭证的五道校验

`check_rules` 判定通过时签发，内容绑定本次明细项与材料：

```
payload = {jti, order_hash, verdict, level, issued_at, exp, iss}
token   = base64url(json) + "." + base64url(hmac_sha256(gate_secret, json))
```

`create_draft` 按顺序验：

| # | 校验 | 失败码 | 拦住什么 |
|---|---|---|---|
| 1 | 签名有效 | `AGT_001` | 伪造、篡改 |
| 2 | 未过期（默认 600s） | `AGT_002` | 拿旧校验建新单 |
| 3 | `verdict == pass` 且无 final 阻断 | `AGT_005` | 用需复核的凭证硬建 |
| 4 | `order_hash` 与提交内容一致 | `AGT_003` | **校验 A 建 B** |
| 5 | `jti` 未用过 | `AGT_004` | 一张凭证建多张单 |

第 4 条是最容易被低估的：`check_rules` 报"18500 → 19000"拿到 pass，
然后建一张"18500 → 50000"的单 —— 没有内容绑定，这招畅通无阻。

### 4.3 为什么幂等检查在验签之前

Agent 会重试。重试时它带的 token 可能已经被消费掉了（一次性凭证的正常代价）。
如果先验签，重试会得到"凭证已使用"，Agent 以为失败，换个幂等键再建一次 ——
结果是两张一样的单。

所以 `create_draft` 的第一步是查 `idempotency_key`：命中就返回上次那张单，
`created=false`。这是唯一一条"没有凭证也能返回成功"的路径，因为它**没有产生新写入**。

### 4.4 失败后的提示

工具被拒时，`next_allowed` 给的是**恢复路径**（`check_rules` / `escalate_to_human` /
`ask_clarification`），不是成功路径。

这条也是踩出来的：最初失败后返回的是 `["submit_draft", "escalate_to_human"]`，
可当时根本没有单可提交 —— 模型会被引到一个必然再失败的动作上。

同时返回文案里有一句硬话：**"不要修改参数重试，也不要寻找其他工具绕过"**。
模型的默认反应就是重试，不把这句话放在紧邻的返回体里，光靠 system prompt 拦不住。

---

## 5. 9 个工具

### 读（5）

| 工具 | 用途 | 关键约束 |
|---|---|---|
| `lookup_policy` | 制度条款检索 | 不设拒答闸门，把"有没有检索到"如实交给模型 |
| `lookup_param` | 结构化参数 | 缺参数返回 `missing`，**不给默认值** |
| `get_employee` | 员工档案 | 出口脱敏；非管理员只能查本人 |
| `get_change_order` | 已有单据 | 默认 24 小时窗（对应第六条） |
| `check_rules` | 规则判定 | 只算不写，可反复调用 |

### 写（3）

| 工具 | 用途 | 闸门 |
|---|---|---|
| `create_draft` | 建草稿 | `validation_token` + 五道校验 |
| `submit_draft` | 提交审批 | `confirm_token`（人点） |
| `escalate_to_human` | 转人工 | 无（这是正当出口） |

### 交互（1）

`ask_clarification` —— 缺信息时问，不猜。一次最多 3 个问题。

---

## 6. Agent 循环与 SSE 接入

### 6.1 循环的四条刹车

模型能自由决策是好事，但自由决策会失控：无限重试、反复猜参数、把上下文撑爆、
一直不肯收尾。四条限制在 `loop.py` 里强制执行，一条也不依赖模型自觉：

| 项 | 默认 | 触顶后 |
|---|---|---|
| 单轮工具调用次数 | 8 | 降级到固定 RAG |
| 单次对话生成轮数 | 6 | 降级 |
| token 预算 | 30000 | 降级 |
| 总超时 | 60s | 降级 |

**触顶不是报错，是降级**：退回阶段 1–4 的固定检索流程，给一个带引用的答案。
用户至少能拿到东西，而不是一个"服务器错误"。

### 6.2 工具集逐轮收窄

第一轮给全部 9 个工具；`check_rules` 判定 pass 后，下一轮只剩
`create_draft` / `escalate_to_human`。模型看不见的工具，比"被告知不要调"可靠得多。

### 6.3 SSE 事件

`POST /chat` 传 `{"mode": "agent", "question": "…"}` 时，事件序列是：

```
session → status → tool_call → tool_result → …（多轮）→ delta → done
```

| 事件 | 携带 | 前端表现 |
|---|---|---|
| `tool_call` | `{name, args}` | 状态条显示"正在核对规则…" |
| `tool_result` | `{name, ok, summary, next_allowed}` | 失败时状态条提示 |
| `delta` | `{text}` | 最终答案（打字机逐字显示） |
| `done` | `{status, tool_calls, draft_id, degraded}` | 落库 + 气泡状态徽标 |

最终答案在**所有工具执行完之后**才产生，作为一帧 `delta` 发出 ——
Agent 的工具调用阶段不是给用户看的"打字机"，用户只关心结论。

### 6.4 状态映射

Agent 的"流程状态"比 RAG 的"答案质量"多出几个值。落库时映射回五态，
`done` 事件仍返回原始状态给前端展示：

| Agent 状态 | 落库 answer_status | 含义 |
|---|---|---|
| `drafted` | `answered` | 建了草稿，等人工确认 |
| `escalated` | `answered` | 已转人工 |
| `inconsistent` | `partial` | 模型谎报写操作，已标注 |
| `degraded` | `partial` | 降级到固定流程 |

### 6.5 模型谎报写操作

工具被拒之后，模型仍然可能说"好的，已为您建单"（这是真模型的典型行为）。
循环会核对"模型说了什么"与"系统实际发生了什么"，命中时：

- 状态标为 `inconsistent`；
- 答复开头加一句 **"⚠ 需要说明：本次并没有实际完成建单"**。

因为**改文案比改状态更直接**：状态给运维看，文案给用户看。用户看到"已建单"
就不会去点确认，而那张单根本不存在。

### 6.6 切到真模型（已接入 DeepSeek）

```bash
# .env（仓库根，已被 .gitignore 排除，config 会自动加载它）
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-...
```

`LLM_BASE_URL` 与 `LLM_MODEL` 留空会自动回落 DeepSeek 官方端点与 `deepseek-chat`。
其他兼容 OpenAI `/chat/completions` 且支持 function calling 的都能接：通义、Kimi、
智谱、本地 vLLM/Ollama —— 只改 `LLM_PROVIDER` 三个字。

验证连通：

```bash
python -m my_tool agent run "上海社保基数上下限是多少"
```

真模型会自己调 `lookup_param`（带 region_code=310000）、组织带条款出处的答案；
问到建单时，它会走完"查档案 → 校验 → 建草稿"，最后停在"请你在界面上确认"。
`LLM_PROVIDER=mock` 时 Agent 模式用脚本化假 Agent（happy path 剧本），日志会提示。

---

## 7. 运维

| 事项 | 说明 |
|---|---|
| 密钥 | `gate_secret` 留空则从 `auth_secret` 派生（HMAC-KDF，不直接复用）。多实例部署必须统一 |
| 参数维护 | 后台改参数后，已签发的凭证仍然有效（绑定的是内容，不是参数）。**但**内容一样、参数变了，判定结论可能已经不对 —— 所以 `create_draft` 会重新检查 final 阻断 |
| 凭证有效期 | `GATE_TOKEN_TTL_SECONDS`，默认 600。太短会让长对话反复校验，太长会让"昨天校验的"今天还能用 |
| 轨迹 | `agent_traces` 表，每次工具调用一条。这是回答"它为什么这么判断"的唯一依据 |
| 演示数据 | `data seed` 会写 16 个参数 + 5 个档案（含一个缺法人归属的样本，用来验证阻断） |
| Agent 开关 | `AGENT_ENABLED`（能力是否可用）、`AGENT_DEFAULT_MODE`（`/chat` 默认走哪条路，默认 `rag`） |

### 已知限制

1. **Agent 决策层是脚本化的**。`LLM_PROVIDER=mock` 时按剧本调工具，不体现模型智商。
   接真模型后，答案质量取决于提示词调优，这不是代码能替你解决的。
2. **`get_employee` 的脱敏只做了字段级**，没有做"同一员工多次查询的关联推断"防护。
3. **转人工不入队**。`escalate_to_human` 只记录 + 返回话术，没有真的进第二十八条的队列 ——
   因为入队需要一张"待人工"的单，而那时候通常还没有单。
4. **参数只有 310000 一套**。别的地区会全部 `missing`，这是刻意的（宁可不答，不可答错）。
5. **薪酬带宽没有数据**。第十一条的带宽校验需要 `grade → bandwidth` 映射，本期未建表，
   所以"超带宽"目前判不出来。
6. **Agent 模式的最终答案不是真流式**。工具循环是阻塞跑完后一次性发 `delta`，
   前端用打字机弥补观感。要真流式需在"模型决定不再调工具"那轮改用 `chat_stream()`，
   但那要再生成一遍，本期没做。
