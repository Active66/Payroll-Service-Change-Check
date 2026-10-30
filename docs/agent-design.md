# 阶段 5 · 从"固定流程问答"到"会自主决策的智能体" 设计 v0.1

> **状态：待确认。** 本文只定工具边界、调用约束与提示词策略，不含实现代码。确认后按第 9 节分批出代码。
>
> 依据：`docs/product/薪酬变更核对系统_技术架构设计_v0.1.html`（1.1 智能体编排层的 Tool Registry、
> 0.4 "数字不由模型产出"、1.2 第③步意图路由是分水岭）；
> `data/policy/员工薪酬变更核对制度_v1.0.md`（第五条类型清单、第二十六条分级、第二十七条阻断、
> 第三十六条材料清单）。
> 复用：`core/orchestrator.py` 的检索与引用生成、`core/guard.py` 的护栏、`api/errors.py` 的错误契约、
> `store/` 的仓储分层。

---

## 0. 先回答一个前置问题：工具还是 skill

你问"设计几个工具或者 skill"。我的建议是**两者都要，但要划一条硬线**：

| | 判据 | 承载什么 |
|---|---|---|
| **Tool** | 要执行代码、读数据、有副作用、需要审计 | 能力边界 |
| **Skill** | 只是"某类问题的处理套路"，不改任何状态 | 流程知识 |

**分界线是：这个能力有没有可能"做错事"。** 能建单、能改状态、能算金额的，必须是工具；
只是"遇到冻结期问题该按什么顺序想"的，是 skill。

为什么不能反过来——把 `create_draft` 写成 skill（用提示词教模型"先校验再建单"）：

> **提示词是请求，不是强制。** 模型可以因为上下文干扰、用户施压、注入攻击而绕过。
> 一旦绕过的是"建单"，代价是一张错单进入发放流程，员工这个月的工资就错了。
> 而工具的拒绝是代码级的：服务端不认 token 就不写库，模型再想绕过也做不到。

所以本文的主线是：**提示词负责让模型"想做对"，代码负责让模型"不能做错"。** 两者缺一不可，
但安全边界只在后者。

---

## 1. 从"问答"到"Agent"到底变了什么

现状（阶段 1–4）是固定流程：`问题 → 检索 top3 → 生成 → 护栏`。流程写死在 `Orchestrator.answer()` 里，
模型没有任何选择权。它只能决定"怎么措辞"。

Agent 化后，模型要自己决定：**查不查、查几次、查什么、查到的东西够不够、要不要建单、建单前该问用户什么。**

这个自由度是必要的——"我的银行卡想改一下"和"调薪超带宽了怎么办"需要的步骤完全不同，
固定流程只能把两条路都走一遍，或者都走不好。但自由度也带来了新的失败模式，本文第 3 节专门处理。

一个必须提前说清的边界：**本期不做"全自动处理变更单"**。Agent 的最远动作是
"建草稿 + 提请人工确认"，提交进审批流必须由人点。理由见第 4.4 节。

---

## 2. 工具清单（9 个）

按"有无副作用"分三层。**只有读工具可以并行、可以重入；写工具一律要过闸。**

### 2.1 读工具（5 个）

#### `lookup_policy` — 制度条款检索

```json
{
  "name": "lookup_policy",
  "description": "按自然语言检索制度条款原文，返回带条款号的片段。用于回答'制度怎么规定的'、'某情形是否允许'类问题。当问题涉及具体数值（金额、比例、基数、时限日期）时，先调本工具定位条款，再用 lookup_param 取数值，不要从条款文本里抄数字。不要用它查员工信息或已有单据。",
  "parameters": {
    "type": "object",
    "properties": {
      "query":   {"type": "string", "description": "检索语句，用制度里的术语（如'冻结期'而非'不能改的时间'）"},
      "top_k":   {"type": "integer", "minimum": 1, "maximum": 8, "default": 3},
      "as_of":   {"type": "string", "description": "按该日期生效的制度版本检索，YYYY-MM-DD。默认今天"}
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

**返回**：`{hits: [{chunk_id, clause_no, doc_title, version, snippet, score}], refused: bool}`
`refused=true` 表示没有相关条款——此时**必须**拒答或转人工，不得用常识补答。

> 复用现有 `HybridRetriever` + `_build_citations`。这是"把固定 RAG 变成可调用能力"，不是重写。

#### `lookup_param` — 结构化参数查询

```json
{
  "name": "lookup_param",
  "description": "读取制度参数表里的确定数值：社保/公积金基数上下限、缴存比例区间、提交时限、冻结期起止日、调薪幅度红线、补发上限、差异分级金额阈值。凡是要在回答里出现数字，必须来自本工具，禁止从条款文本摘抄或自行推算。参数缺失时返回 found=false，此时不得猜值，应转人工。",
  "parameters": {
    "type": "object",
    "properties": {
      "keys": {
        "type": "array",
        "items": {"type": "string", "enum": [
          "si_base_cap", "si_base_floor", "hf_ratio_min", "hf_ratio_max",
          "submit_deadline_day", "freeze_start_day", "freeze_end_day",
          "raise_cap_pct", "cut_cap_pct", "backpay_months_limit",
          "diff_l1_max", "diff_l2_max", "diff_l3_max",
          "bank_change_quota_30d", "verify_pay_min", "verify_pay_max"
        ]},
        "description": "要取的参数名"
      },
      "as_of": {"type": "string", "description": "按该日期的参数版本取，YYYY-MM-DD"}
    },
    "required": ["keys"],
    "additionalProperties": false
  }
}
```

**返回**：`{items: [{key, value, unit, source_clause, effective_from, effective_to}], missing: [key...]}`

> 这是"数字不由模型产出"的落地。现在 `guard.py` 是**事后**核对数字有没有出现在原文里；
> 有了这个工具，数字**事前**就来自查询结果，护栏从"抓错"变成"兜底"。
> 代价是要建一张参数表（新增 `policy_params` 表 + 后台编辑页），这是本期的主要数据工作。

#### `get_employee` — 员工档案

```json
{
  "name": "get_employee",
  "description": "读取员工档案：入职日、职级、现固定薪资、参保地、法人主体、近 30 日银行账户变更次数。建变更单前必须调用，用于判定生效日是否早于入职日、调薪是否超带宽、银行卡变更是否超次数。只能查当前登录用户有权查看的员工。",
  "parameters": {
    "type": "object",
    "properties": {
      "employee_no": {"type": "string", "description": "工号。不传则查当前登录用户本人"},
      "fields": {"type": "array", "items": {"type": "string"}, "description": "只取需要的字段，减少上下文占用"}
    },
    "additionalProperties": false
  }
}
```

**返回**：`{employee: {...}, masked: ["id_card"], missing: [...]}`
敏感字段按角色脱敏（证件号只返回后 4 位），**脱敏在服务端做，不是靠提示词让模型别说**。

#### `get_change_order` — 查已有变更单

```json
{
  "name": "get_change_order",
  "description": "按单号查变更单，或按员工+变更类型查近期单据。用于判断'同一明细项 24 小时内重复提交'（制度第六条、第二十七条阻断项）和'补发是否超过 3 个月'。",
  "parameters": {
    "type": "object",
    "properties": {
      "order_no":     {"type": "string"},
      "employee_no":  {"type": "string"},
      "change_type":  {"type": "string", "enum": ["bank_account","si_base","hf_base","salary_adjust","allowance","address","tax_special"]},
      "within_hours": {"type": "integer", "default": 24, "maximum": 720}
    },
    "additionalProperties": false
  }
}
```

#### `check_rules` — 规则判定（只算不写）

**这是整个设计的枢纽。**

```json
{
  "name": "check_rules",
  "description": "对一份变更草稿逐条跑制度核对规则，返回阻断项、风险等级、所需材料，以及通过时签发的一次性校验凭证。这是建单前的强制步骤：不调用本工具、或本工具判定为阻断，create_draft 一律会被服务端拒绝。本工具不写任何业务数据，可以反复调用；每次调用都会记审计。",
  "parameters": {
    "type": "object",
    "properties": {
      "change_type": {"type": "string", "enum": ["bank_account","si_base","hf_base","salary_adjust","allowance","address","tax_special"]},
      "employee_no": {"type": "string"},
      "items": {
        "type": "array",
        "description": "明细项：本制度里'明细项'是核对与审计的最小判定单位（第三条）",
        "items": {
          "type": "object",
          "properties": {
            "field":    {"type": "string", "description": "字段名，如 bank_account / fixed_salary / si_base"},
            "old_value":{"type": "string"},
            "new_value":{"type": "string"},
            "effective_date": {"type": "string", "description": "生效日 YYYY-MM-DD"}
          },
          "required": ["field", "new_value"],
          "additionalProperties": false
        },
        "minItems": 1, "maxItems": 20
      },
      "materials": {
        "type": "array", "items": {"type": "string"},
        "description": "已上传的材料类型码，见制度第三十六条"
      },
      "is_urgent": {"type": "boolean", "default": false, "description": "是否走第三十三条紧急通道"}
    },
    "required": ["change_type", "employee_no", "items"],
    "additionalProperties": false
  }
}
```

**返回**：

```json
{
  "verdict": "pass | block | need_review",
  "level": "L1 | L2 | L3 | L4 | null",
  "blocking": [
    {"code": "BLK_BANK_NAME_MISMATCH", "clause": "第二十七条", "message": "账户姓名与员工姓名不一致", "final": true}
  ],
  "warnings": [
    {"code": "WARN_FREEZE_PERIOD", "clause": "第八条", "message": "当前处于冻结期，本单将顺延至下一周期"}
  ],
  "required_materials": [{"code": "bank_change_form", "name": "员工签字的账户变更申请", "present": false}],
  "computed": {"boundary": [36921, 7384], "diff_amount": 0, "annual_raise_pct": 0.08},
  "validation_token": "vt_eyJ...",
  "token_expires_in": 600,
  "next_allowed": ["create_draft", "escalate_to_human"]
}
```

四条约定：

1. **`blocking` 里 `final: true` 的项，服务端永久拒绝建单**——对应制度第二十七条"不得人工放行"。
   非 final 的阻断（缺材料）可以在补齐后重跑。
2. **`validation_token` 只在 `verdict == "pass"` 时签发**，且与本次 `items` 的内容哈希绑定（见 3.3）。
3. **`next_allowed` 是给模型看的"当前可做什么"**。这是第 3.2 节的核心机制。
4. 每次调用记审计（谁、什么时间、判定结果、命中的条款），即使没建单也留痕——
   制度第三十八条要求"核对结论与留存材料的一致性"可抽查。

### 2.2 写工具（3 个）

#### `create_draft` — 建变更单草稿

```json
{
  "name": "create_draft",
  "description": "创建变更单草稿。必须携带 check_rules 签发的 validation_token，且草稿内容必须与签发时的内容完全一致；服务端会重新校验，任一不符即拒绝。缺 token、token 过期、内容被修改、存在 final 阻断项，都会返回 E_GATE_REQUIRED 或 E_BLOCKED。被拒绝时不要换参数重试，应向用户说明原因。",
  "parameters": {
    "type": "object",
    "properties": {
      "validation_token": {"type": "string"},
      "change_type": {"type": "string"},
      "employee_no": {"type": "string"},
      "items": {"type": "array", "items": {"type": "object"}},
      "materials": {"type": "array", "items": {"type": "string"}},
      "idempotency_key": {
        "type": "string",
        "description": "同一意图的唯一键。Agent 会因超时重试，没有幂等键就会建出两张一样的单"
      },
      "note": {"type": "string", "maxLength": 500}
    },
    "required": ["validation_token", "change_type", "employee_no", "items", "idempotency_key"],
    "additionalProperties": false
  }
}
```

#### `submit_draft` — 提交进审批流

```json
{
  "name": "submit_draft",
  "description": "把草稿提交进审批流。这是不可撤销的对外动作，必须携带人工确认凭证 confirm_token。confirm_token 只能由用户在界面上点击产生，你无法自己获取；没有它请先向用户说明将要提交的内容并请求确认。",
  "parameters": {
    "type": "object",
    "properties": {
      "draft_id": {"type": "string"},
      "confirm_token": {"type": "string"},
      "idempotency_key": {"type": "string"}
    },
    "required": ["draft_id", "confirm_token", "idempotency_key"],
    "additionalProperties": false
  }
}
```

#### `escalate_to_human` — 转人工

```json
{
  "name": "escalate_to_human",
  "description": "把当前事项转人工核对队列（制度第二十八条）。当出现以下任一情况必须调用：制度里查不到依据；参数缺失（lookup_param 返回 missing）；存在 final 阻断但用户坚持；风险等级需要人工审批；你不确定自己理解对了。这是正确的收尾方式，不是失败。",
  "parameters": {
    "type": "object",
    "properties": {
      "reason_code": {"type": "string", "enum": [
        "no_policy_basis", "param_missing", "blocked_final",
        "needs_approval", "ambiguous", "out_of_scope"
      ]},
      "summary": {"type": "string", "maxLength": 500, "description": "一句话说明卡在哪，人工接手要看的第一句话"},
      "context": {"type": "object", "description": "已查到的条款号、判定结果，避免人工从零开始"}
    },
    "required": ["reason_code", "summary"],
    "additionalProperties": false
  }
}
```

> 这个工具不能省。**没有正当出口的 Agent 会硬编**——它要么开始编制度，要么在拒绝后无限重试。
> 给它一个"体面退出"的路径，比任何"不要编造"的提示词都有效。

### 2.3 交互工具（1 个）

#### `ask_clarification` — 向用户澄清

```json
{
  "name": "ask_clarification",
  "description": "信息不足时向用户提问，而不是猜测。当缺少必填项（生效日、变更类型、新值）、或同一句话有两种解释、或需要用户确认将要执行的动作时使用。一次最多问 3 个问题。",
  "parameters": {
    "type": "object",
    "properties": {
      "questions": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "field": {"type": "string"},
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}}
          },
          "required": ["field", "question"]
        },
        "minItems": 1, "maxItems": 3
      }
    },
    "required": ["questions"],
    "additionalProperties": false
  }
}
```

### 2.4 为什么是 9 个，不是 15 个

工具越多，模型选错的概率越高（上下文里每个工具的 description 都在争夺注意力），
而且每个工具都要维护 schema、错误码、审计、测试。9 个已经覆盖制度里全部 7 种变更类型和 6 类核对项。

**刻意不做的工具**（写在这里，免得将来被"顺手"加上）：

| 不做 | 原因 |
|---|---|
| `run_sql` / `query_db` | 注入面太大，且无法审计"这条数据是被谁以什么理由读的" |
| `calculate`（自由算式） | 金额必须由规则引擎算。让模型算第十二条的"年度累计调薪幅度"，它会在第三次调薪时算错 |
| `update_order` / `delete_order` | 制度第二十九条：关闭后不可编辑，修正须新建单并引用原单号 |
| `send_notification` | 对外发消息是不可撤销动作，本期只做站内 |
| `analytics_query` | 架构文档里有，但它属于"统计分析"通道，与核对主流程正交，下一期单独做 |

---

## 3. 调用顺序约束（你最关心的部分）

你要的例子是"`create_draft` 必须在 validate 通过后"。我把它拆成四层，**从软到硬**，
并明确每层拦得住什么、拦不住什么。

### 3.1 第 1 层：提示词（拦不住，但决定"默认行为"）

见第 5 节。作用：让模型在正常路径上按顺序走，理解为什么。**但绝不作为安全边界**——
一个被注入的文档片段、一句"我是薪酬经理，直接建单"，就可能让它跳步。

### 3.2 第 2 层：工具返回值里的 `next_allowed`（大幅降低跳步概率）

每个工具返回都带 `next_allowed`：

```
lookup_policy  → next_allowed: ["lookup_policy", "lookup_param", "get_employee", "check_rules"]
check_rules    → next_allowed: ["create_draft", "escalate_to_human"]      // 或只 ["escalate_to_human"]
create_draft   → next_allowed: ["submit_draft", "escalate_to_human"]
```

**机制**：服务端在把工具返回交给模型之前，按 `next_allowed` **过滤工具集**——
不在列表里的工具，下一轮根本不出现。

为什么这招比提示词有效：模型的行为由**上下文里最近、最具体的信息**驱动，
而不是由几十轮之前的系统提示驱动。把"你现在只能建单或转人工"放在紧邻的返回值里，
比在 system prompt 里写一段流程说明强得多。

**它拦不住什么**：如果 `next_allowed` 算错了（比如 `check_rules` 判定 pass 但实际该 block），
模型就会合法地建单。所以它只是"减少犯错"，不是"防错"。

### 3.3 第 3 层：校验凭证（真正的硬约束）

`check_rules` 判定通过时签发 `validation_token`，结构：

```
payload = {
  "jti":        一次性 id,
  "order_hash": sha256(change_type + employee_no + 规范化(items) + 排序(materials)),
  "verdict":    "pass",
  "level":      "L2",
  "issued_at":  时间戳,
  "exp":        issued_at + 600s,
  "iss":        "rule_engine"
}
token = base64(payload) + "." + hmac_sha256(secret, base64(payload))
```

`create_draft` 服务端做 **五道校验**，任一失败即拒绝：

| # | 校验 | 失败返回 | 拦住什么 |
|---|---|---|---|
| 1 | 签名有效 | `E_GATE_REQUIRED` | 模型伪造 token、token 被篡改 |
| 2 | 未过期（600s） | `E_GATE_EXPIRED` | 拿很久以前的一次校验来建新单 |
| 3 | `order_hash` 与本次提交内容一致 | `E_GATE_STALE` | **校验的是 A，建的是 B**（这是最隐蔽的绕过：把金额从 5000 改成 50000） |
| 4 | `verdict == pass` 且无 final 阻断 | `E_BLOCKED` | 用 need_review 的凭证硬建单 |
| 5 | `jti` 未被使用过 | `E_GATE_REUSED` | 一张凭证建多张单 |

**为什么 token 不能只是一个布尔标记 `validated: true`**：
因为 Agent 会重试、会改内容、会在多轮对话里混淆上下文。布尔标记对这四件事全都无能为力，
而"签名 + 内容绑定 + 时效 + 一次性"能同时防住。

`jti` 的消费记录落 `gate_tokens` 表，同时 `create_draft` 的审计里记下 `jti`——
这样能回答"这张单是基于哪一次校验建的、当时校验的内容是什么"，这是制度第三十八条的审计要求。

### 3.4 第 4 层：人工确认（对外动作的最后一道）

`submit_draft` 需要 `confirm_token`，它与 `validation_token` **机制相同但来源不同**：
只能由人在界面上点"确认提交"时，由前端调用 `/api/agent/confirm` 生成，绑定 `draft_id` + 内容哈希。

模型没有任何工具能生成它。**这是整个设计里唯一一处"模型绝对做不到"的约束**——
因为它是"人"的凭证，不是"系统"的凭证（和阶段 4 里 JWT vs API Key 的划分同源）。

### 3.5 完整顺序图

```
用户: "把我银行卡改成 6222 xxxx"
  │
  ├─ ask_clarification        (缺生效日 / 缺材料 → 先问，不猜)
  ├─ get_employee             (入职日、30 日内改过几次)
  ├─ lookup_policy            (第二十二条、第二十三条)
  ├─ lookup_param             (verify_pay_min/max, bank_change_quota_30d)
  ├─ get_change_order         (within_hours=24，查重复提交)
  │
  ├─ check_rules  ────────────► block  → escalate_to_human  (到此为止)
  │                    │
  │                    └──────► pass   → validation_token
  │
  ├─ create_draft (带 token)  → draft_id
  │
  └─ submit_draft (带 confirm_token)  ← 需要人点确认，模型只能"请求"
```

---

## 4. 系统提示词怎么写才拦得住跳步

### 4.1 五条原则

1. **写"必须"和"禁止"，不写"请尽量"。** 模糊的措辞等于没有约束。
2. **每条禁止后面跟"正确做法"。** 只说"不要猜"会让模型卡住；说"不要猜，改用 ask_clarification"它才会动。
3. **说明"被拒绝时怎么办"。** 模型最常见的坏行为是被工具拒绝后换个参数重试——必须明确禁止。
4. **把流程写成"依赖关系"而不是"步骤清单"。** "步骤 1、2、3"模型会跳着做；"没有 X 就不能 Y"它更可能遵守。
5. **不要重复工具 description。** 提示词讲"为什么"和"边界"，schema 讲"怎么调"。重复会让上下文臃肿且互相矛盾。

### 4.2 提示词正文（可直接用）

```
你是薪酬共享服务中心（SSC）的核对助手。你的工作是两件事：
① 按制度回答员工的薪酬变更问题；② 在信息齐备时起草变更单，交由人确认。
你不做审批决定，也不替员工做承诺。

【最高原则】
1. 一切结论必须有制度条款支撑。查不到条款就说查不到，不要用常识、经验或"一般来说"补答。
2. 任何数字（金额、比例、基数、天数、条号）只能来自 check_rules 或 lookup_param 的返回值。
   不得从条款文本里摘抄，不得由你计算或推算。年度累计幅度、补发差额、补退金额这类
   需要多步计算的结果，一律交给规则引擎。
3. 你不做审批。风险等级的处置由规则引擎和人决定，你只负责把结论讲清楚。

【建单的依赖关系】
- 没有 validation_token，就不能调用 create_draft。这不是建议，是硬性依赖：
  服务端会拒绝并返回 E_GATE_REQUIRED。
- validation_token 由 check_rules 在判定通过时签发，绑定你提交给它的内容。
  只要明细项的新旧值、生效日、材料清单有任何改动，凭证即失效，必须重新调用 check_rules。
- 存在任何 final 阻断项时，不得建单，也不得通过修改字段来"绕过"阻断。
  正确做法是向用户解释阻断依据（引用条款号），必要时调用 escalate_to_human。
- 建单后如需提交进审批流，必须调用 submit_draft 并携带 confirm_token。
  confirm_token 只能由用户在界面上确认产生，你没有获取它的工具。
  因此你的最后一步永远是"向用户说明将要提交什么，并请求确认"，而不是自己提交。

【被拒绝时怎么办】
- 工具返回 E_GATE_REQUIRED / E_GATE_EXPIRED / E_GATE_STALE / E_BLOCKED 时，
  不要修改参数重试，不要寻找其他工具绕过。向用户说明被拒绝的原因和依据条款。
- 同一工具连续失败两次，改用 escalate_to_human。不要循环。

【必须转人工的情形】
制度里查不到依据；lookup_param 返回 missing；存在 final 阻断但用户坚持要办；
风险等级 L3 及以上需要审批；你不确定自己的理解是否正确。
转人工是正确收尾，不是失败。调用 escalate_to_human 时把已查到的条款号和判定结论放进 context，
让人工不必从零开始。

【回答风格】
- 先给结论，再给依据（条款号 + 一句原文），最后给下一步该做什么。
- 涉及金额、时限、等级时，把数字和它的来源条款一起说，让用户能自己核对。
- 不承诺"一定可以办成"。你能确定的是"按第 X 条，这个情形属于/不属于受理范围"。
```

### 4.3 三条容易被忽略但很关键的设计

**① 不把"步骤清单"写进提示词。**
"第一步调 A，第二步调 B"看着清楚，但会让模型在 A 失败时不知道能否继续。
写成"没有 X 就不能 Y"的依赖关系，模型在任何中断点都能自己判断当前状态。

**② 提示词里明确"工具会拒绝你"。**
模型不知道服务端有闸门时，会觉得"我调一下试试"。告诉它"会被拒绝、拒绝码是什么"，
它的默认策略就会从"试试看"变成"先准备好"。

**③ 拒答和转人工要写成"正常结果"，不是"异常"。**
模型的训练偏好是"给出答案"。如果拒答被描述成失败，它就会尽力避免拒答——也就是开始编。
把它写成"这是正确收尾"，拒答率才会真实。

### 4.4 为什么 `submit_draft` 必须人确认

技术上可以让 Agent 一路做到底，但这份系统的错误代价是不对称的：
答错一句话，用户再问一次；**提交错一张单，员工这个月的工资就错了**，且要走冲销流程（第三十二条）。

所以动作按"可撤销性"分级：
- 建草稿 → 可撤销（删掉即可）→ 允许 Agent 自动做
- 提交进审批流 → 不可撤销 → 必须人确认
- 审批通过 → 影响发放 → 人做，Agent 不参与

---

## 5. Skill 清单（3 个）

Skill 是"注入到上下文里的流程知识"，不改状态。用 Markdown 写在 `src/my_tool/skills/`，
按 Router 判定的意图**按需注入**，不是全量塞进 system prompt（否则每次对话都在烧钱）。

| Skill | 何时注入 | 内容要点 |
|---|---|---|
| `change_order_intake` | 用户表达"我要改 X" | 7 种变更类型各自需要哪些字段、哪些材料（第三十六条）、常见追问顺序。**不含**判定规则——那些在 `check_rules` 里 |
| `risk_explain` | 用户问"为什么被阻断/为什么是 L3" | L1–L4 的判定口径怎么向员工解释、哪些话不能说（如"这个我帮你放行"）、如何引导申诉（第二十七条要求记录阻断码供举证） |
| `freeze_period_guide` | 问题涉及冻结期/时限 | 第七条、第八条、第十条的关系；银行账户在冻结期内的特殊处理（可受理但不参与当期发放）；加急通道（第三十三条）的适用条件 |

**为什么规则不写成 skill**：规则的判定结果必须可复现、可审计、能在单测里断言。
写成自然语言交给模型判断，同一个问题两次回答可能不一样——这在核对场景里是不可接受的。
规则的唯一载体是 `check_rules` 背后的 Python 规则引擎。

---

## 6. Agent 循环的工程边界

自由度的代价是可能失控。四条硬限制：

| 项 | 值 | 理由 |
|---|---|---|
| 单轮最大工具调用次数 | 8 | 超过说明在打转。触顶强制 `escalate_to_human` |
| 单次对话最大 LLM 轮数 | 6 | 每轮都是一次完整生成，6 轮足够完成"问 → 查 → 判 → 建" |
| 单次对话 token 预算 | 可配，默认 30k | 超预算中断并告知用户 |
| 总超时 | 60s | 超过返回"处理中"，转异步（本期只提示，不做异步） |

超限的处理方式统一是**"降级到固定流程"**：用已有的 `Orchestrator.answer()` 走一遍纯 RAG，
保证用户至少拿到一个带引用的答案，而不是一个报错。

另外：**模型选择**。工具调用对模型能力要求比纯生成高得多。默认配置应指向支持
function calling 的模型，`mock` 供应商需要实现一个"脚本化的假 agent"用于离线测试
（按预设顺序返回工具调用，验证闸门逻辑而不验证模型智商）。

---

## 7. 与现有代码的关系

| 现有 | 变化 | 风险 |
|---|---|---|
| `core/orchestrator.py` | 保留，作为 Agent 的"降级通道"和 `lookup_policy` 的实现基础 | 低 |
| `core/guard.py` | 保留，从"主要防线"降为"兜底"（数字事前来自 `lookup_param`） | 低 |
| `core/llm/base.py` | `ChatModel` 增加 `chat_with_tools(messages, tools) -> ToolCallResult` | **中**：所有供应商适配器要加，无工具能力的要有降级 |
| 新增 `core/agent/` | `loop.py`（循环与预算）、`registry.py`（工具注册与按 `next_allowed` 裁剪）、`gate.py`（token 签发校验）、`rules.py`（规则引擎）、`tools/*.py` | 高（新子系统） |
| 新增 `store/param_repo.py`、`store/order_repo.py` | `policy_params`、`change_orders`、`change_items`、`gate_tokens` 四张表 | 中 |
| `api/routes_chat.py` | 增加 `agent` 模式开关；SSE 增加 `tool_call` / `tool_result` 事件 | 中（改的是最热路径） |
| `api/routes_confirm.py` 新增 | `/api/agent/confirm` 生成 confirm_token | 中（安全相关） |
| 管理后台 | 参数表编辑页、变更单列表、Agent 轨迹回放（每次工具调用一条） | 低 |

**兼容性**：`AGENT_ENABLED=0` 时完全走现有固定流程，179 项测试与现有演示不受影响。
这与阶段 4 的 `AUTH_REQUIRED` 开关是同一个策略。

---

## 8. 明确不做

1. **不做全自动审批**。Agent 最远到"建草稿"，提交要人点，审批不碰。
2. **不给模型 SQL 或自由算式**。理由见 2.4。
3. **不做跨法人/跨租户的工具**。本期工具全部在单法人上下文内（架构文档的法人隔离是另一件事）。
4. **不做 Agent 之间的协作**（多智能体）。一个核对助手，拆成多个 Agent 只会让调试地狱化。
5. **不做"记忆"**。跨会话记忆在薪酬场景是负担不是能力——"上次你说可以"不能作为依据。

---

## 9. 分批交付

| 批次 | 内容 | 验收 |
|---|---|---|
| **1** 工具地基 | `core/agent/registry.py`、`gate.py`、`rules.py` 骨架；`lookup_policy` / `lookup_param` / `get_employee` 三个读工具；`policy_params` 表 + 种子数据；`AGENT_ENABLED` 开关 | 三个工具能单独调通；参数表数值与制度第十八条一致；关开关后 179 项测试全绿 |
| **2** 规则引擎 | `check_rules` + 6 类核对项 + 6 个阻断码 + L1–L4 分级；`validation_token` 签发 | 12 个用例覆盖：6 个阻断项各一、分级边界（100/1000/5000）、冻结期、生效日非法 |
| **3** 写工具与闸门 | `create_draft` / `submit_draft` / `escalate_to_human` / `ask_clarification`；五道校验；`change_orders` 表；`/api/agent/confirm` | 无 token 建单被拒；改内容后 token 失效；同 token 二次使用被拒；无 confirm_token 提交被拒 |
| **4** Agent 循环 | `loop.py`（预算、轮数、降级）、工具集裁剪、SSE 的 `tool_call`/`tool_result` 事件、前端轨迹展示 | 端到端跑通"改银行卡"全流程；超预算时降级到 RAG 而不是报错 |
| **5** 打磨 | 3 个 skill、提示词进后台版本管理、Agent 轨迹回放页、`docs/agent.md` | 提示词可在后台改；轨迹页能看清每一步为什么这么走 |

预计测试从 179 项增加到 240+。

---

## 10. 待你确认

| # | 议题 | 选项 | 我的建议 |
|---|---|---|---|
| 1 | 工具 or skill 为主 | 工具为主 + skill 为辅 / 全用 skill / 全用工具 | **工具为主**。安全边界只能落在代码上 |
| 2 | 工具数量 | 9 个 / 精简到 5 个 / 扩到 15 个 | **9 个**。5 个会逼模型用万能工具，15 个会选错 |
| 3 | 规则引擎形态 | Python 硬编码 / 配置化规则表（可后台改） | **Python 硬编码**。规则要能单测、能复现；配置化会让"改配置引发的错判"无法回归 |
| 4 | 参数表维护 | 后台编辑 + 版本 / 只读种子数据 | **后台编辑带版本**。基数每年 7 月变（第十九条），没有编辑入口就只能改代码上线 |
| 5 | `submit_draft` 本期是否做 | **做到"请求人工确认"为止** / 连提交一起做 | **做到请人确认为止**。理由见 4.4 |
| 6 | token 有效期 | 10 分钟 / 30 分钟 / 1 小时 | **10 分钟**。够一次完整对话，又不会让"昨天校验的"今天还能用 |
| 7 | 是否保留固定流程 | **保留，作为降级通道** / 完全替换 | **保留**。超预算、模型不支持 tools、异常时都要能退回 |

另外两个默认项，有异议一起说：**Agent 轨迹全量落库**（每次工具调用一条，含入参出参与耗时，
这是唯一能复现"它为什么这么判断"的方式）；**`mock` 供应商实现脚本化假 agent**（离线可测闸门，
不测模型智商）。

确认后我从批次 1 开始。
