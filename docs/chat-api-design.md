# 员工问答入口 · 接口设计 v0.1

> **状态：已实现（v0.2.0）。** 本文是契约来源，代码在 `src/my_tool/api/` 与 `web/`。
> 实现过程中有三处与初稿不同，见第 12 节「实现差异」——以本文正文 + 第 12 节为准。
>
> 依据：`docs/product/薪酬变更核对系统_技术架构设计_v0.1.html`
> —— 3.3 / 3.4 的会话与消息表、0.3 的"引用必须回到 L0"铁律、4.3 的评测口径。
> 复用：阶段 1 的 `Orchestrator` / `HybridRetriever` / `run_guard` / `build_prompt`。

---

## 0. 范围

| | 内容 |
|---|---|
| **本期做** | `POST /chat` SSE 流式问答；会话与消息落 SQLite；最近 6 轮历史进上下文；单页聊天界面（打字机 + 检索状态 + 引用卡片） |
| **本期不做** | 登录与权限（用固定 `user_id` 占位）、Router 意图分流、pgvector 迁移、变更单比对、滚动摘要压缩 |
| **预留** | `POST /messages/{id}/feedback`（表里已有 `feedback` 字段）、`session.summary` 字段（阶段 3 的滚动摘要） |

一句话边界：**本期只保证"员工问、系统答、答案可举证、刷新不丢"，不保证"问得乱也能答对"。**

---

## 1. 接口总览

| 方法 | 路径 | 用途 | 响应类型 |
|---|---|---|---|
| POST | `/chat` | 提问，流式返回 | `text/event-stream` |
| POST | `/sessions` | 新建会话 | JSON |
| GET | `/sessions/{session_id}/messages` | 刷新后恢复历史 | JSON |
| GET | `/healthz` | 索引 / 模型 / 数据库自检 | JSON |
| POST | `/messages/{message_id}/feedback` | 有用 / 无用（可选） | JSON |

**为什么用户只要 `/chat`，我却要多做三个**：

1. `GET /sessions/{id}/messages` —— 没有它，刷新页面就只剩一条空白，"最近 6 轮"这个能力用户根本感知不到；
2. `POST /sessions` —— 前端"新建对话"按钮需要，且让 `/chat` 可以只做"回答"一件事；
3. `GET /healthz` —— 索引是派生物（`data/index` 不进版本库），换机器后忘了 `build` 是最常见的部署事故，要给一个能一眼看出来的入口。

---

## 2. 统一错误契约

### 2.1 响应信封

所有**非流式**接口统一两种形状，前端只需要一个 `if (j.ok)` 分支：

```json
// 成功
{ "ok": true, "data": { ... } }

// 失败
{
  "ok": false,
  "error": {
    "code": "SES_001",
    "message": "会话不存在或已删除",
    "detail": { "session_id": "0b7c..." },
    "trace_id": "tr_9f2c1a8b"
  }
}
```

约定四条：

1. `code` 是**机器可判断**的稳定标识，**永不复用**、永不改语义；新增错误只能新增码。
2. `message` 是**给员工看的中文**，一句话、说清"发生了什么 + 该怎么办"，不出现堆栈、不出现英文异常类名、不出现表名和字段名。
3. `detail` 只放开发者排障需要的信息（字段名、阈值、上游状态码），前端默认不展示。
4. `trace_id` 每次请求生成，同时写进日志与 `chat_messages.trace_id`，用户报障时可凭它反查全链路。

### 2.2 错误码表

| 错误码 | HTTP | 中文信息（默认） | 触发条件 | 前端动作 |
|---|---|---|---|---|
| `REQ_001` | 400 | 请求格式不正确，请确认发送的是合法 JSON | body 无法解析 | 提示 + 不重试 |
| `REQ_002` | 400 | 缺少必填参数：{字段名} | 缺 `question` 等 | 开发期错误，提示 |
| `REQ_003` | 400 | 问题不能为空，请先输入想问的内容 | `question` 空白 | 输入框聚焦 |
| `REQ_004` | 400 | 问题过长（上限 500 字），请拆分后再问 | 超长 | 提示，保留草稿 |
| `REQ_005` | 400 | 会话标识格式不正确 | `session_id` 非法 | 清本地会话，重建 |
| `SES_001` | 404 | 会话不存在或已被清理，请新建一个对话 | 查无此会话 | 自动新建会话并重发 |
| `SES_002` | 410 | 该会话已删除，无法继续提问 | `deleted_at` 非空 | 自动新建会话 |
| `SES_004` | 409 | 该会话正在生成回答，请等待完成或先停止 | 存在 `streaming` 消息 | 禁用发送按钮 |
| `IDX_001` | 503 | 制度索引尚未就绪，请联系管理员初始化 | 索引文件缺失 | 提示，不重试 |
| `IDX_002` | 503 | 索引与当前向量模型不一致，需要重建索引 | manifest 与配置不符 | 提示，不重试 |
| `LLM_001` | 504 | 生成回答超时，请稍后重试 | 上游超时 | 显示重试按钮 |
| `LLM_002` | 502 | 模型服务配置有误（鉴权失败或参数不合法） | 上游 4xx | 提示，联系管理员 |
| `LLM_003` | 502 | 模型服务暂时不可用，请稍后重试 | 上游 5xx | 重试按钮 |
| `SRV_001` | 500 | 服务内部错误，请稍后重试（编号 {trace_id}） | 未捕获异常 | 重试按钮 |
| `SRV_002` | 429 | 当前请求过多，请稍后再试 | 并发超限 | 3 秒后允许重试 |
| `CLI_001` | — | （不返回给前端）客户端已断开，生成中止 | 浏览器关页 / abort | 仅日志与落库 |

### 2.3 流式接口的错误怎么给

这是最容易被忽略的一条，必须写死：

- **流开始之前**失败（校验、会话不存在、索引未就绪）→ 返回 **HTTP 4xx/5xx + 2.1 的 JSON**，此时 `Content-Type` 还是 `application/json`。
- **流已经开始**（已发出第一个 `event:`）→ HTTP 状态码固定 200，**错误只能作为 `event: error` 出现在流里**，然后立即结束，**不再发 `event: done`**。

原因：响应头一旦发出就改不了状态码了。前端必须同时处理两条错误路径——这也是为什么 `event: error` 的 payload 与 2.1 的 `error` 对象**字段完全一致**（`code` / `message` / `detail` / `trace_id`），复用同一个渲染函数。

---

## 3. `POST /chat` —— 主接口

### 3.1 请求

```http
POST /chat HTTP/1.1
Content-Type: application/json
Accept: text/event-stream
```

```json
{
  "session_id": "0b7c9e6a4d5f4c1e8b3a2d7f6e5c4b3a",
  "question": "冻结期内能改银行账户吗？"
}
```

| 字段 | 类型 | 必填 | 规则 |
|---|---|---|---|
| `session_id` | string \| null | 否 | 为空 → 服务端新建会话，并在首个 `session` 事件里回传。非空 → 必须是 32 位十六进制或标准 UUID |
| `question` | string | 是 | 去首尾空白后 1–500 字；含中文标点不影响 |
| `as_of` | string(date) | 否 | 制度生效日，默认今天。架构 3.5 的双时态过滤要用它，**本期先透传不生效**，为多版本预留 |

### 3.2 响应头

```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

`no-transform` 与 `X-Accel-Buffering: no` 都是为了**别让中间层缓冲**——否则代理会把流攒成一坨再吐，打字机变成"一次性出现"，且首屏更慢。若前面挂 nginx，还需 `proxy_buffering off; proxy_read_timeout 300s;`。

### 3.3 事件协议

七种事件，顺序固定（`ping` 可穿插，`guard` 只在需要时出现）：

| # | event | 时机 | data |
|---|---|---|---|
| 1 | `session` | 建立/确认会话后立即 | `{session_id, user_message_id, seq}` |
| 2 | `status` | 检索前 | `{stage:"retrieving", text:"正在检索制度…"}` |
| 3 | `citations` | **检索完成、生成开始前** | `{citations:[...]}` |
| 4 | `status` | 生成前 | `{stage:"generating", text:"正在组织回答…"}` |
| 5 | `delta` | 每次模型吐字 | `{text:"冻结期内"}` |
| 6 | `guard` | 仅当护栏有告警 | `{status:"partial", warnings:[...]}` |
| 7 | `done` | 结束 | `{message_id, status, usage, trace_id, query_used}` |

三个非显然的决定：

**① `citations` 放在生成之前发。** 引用列表来自检索结果，与生成无关。早发有两个好处：前端可以在打字的同时先把"参考资料"渲染出来（感知更快），而且用户能立刻判断"检索的东西对不对"——这比等答案更早暴露召回错误。

**② 拒答也走 `delta`，不新增事件。** 无召回时编排器直接返回固定话术（阶段 1 的 `refused` 分支，0 token），用 `delta` 吐出来 + `done.status="refused"`，前端只在气泡上加一个"未找到依据"标签。少一个事件分支，前端少一个 bug。

**③ `done` 里带 `query_used`。** 就是本次实际用于检索的 query。多轮里如果触发了"追问合并"（见 6.3），前端和排查都能看到"系统把我的问题补成了什么"，避免出现"我明明问了 A，它答的是 B"却无从解释。

### 3.4 完整报文样例

```
event: session
data: {"session_id":"0b7c9e6a4d5f4c1e8b3a2d7f6e5c4b3a","user_message_id":12,"seq":1}

event: status
data: {"stage":"retrieving","text":"正在检索制度…"}

event: citations
data: {"citations":[{"index":1,"doc_title":"员工薪酬变更核对制度","source_file":"员工薪酬变更核对制度_v1.0.md","clause_no":"第七条–第九条","heading_path":"第三章 提交时限与冻结期","chunk_id":"POL-CMP-2026-001#c006","snippet":"银行账户变更在冻结期内可受理但不参与当期发放，自下一周期起生效。","score":0.032786}]}

event: status
data: {"stage":"generating","text":"正在组织回答…"}

event: delta
data: {"text":"银行账户变更在冻结期内"}

event: delta
data: {"text":"可以受理，但不参与当期发放"}

event: guard
data: {"status":"partial","warnings":["以下数字未能在资料原文中找到（疑似模型自造）：50,000 元"]}

event: done
data: {"message_id":13,"status":"partial","usage":{"tokens_in":1280,"tokens_out":156,"latency_ms":2410},"trace_id":"tr_9f2c1a8b","query_used":"冻结期内能改银行账户吗？"}
```

`ping` 心跳（每 15 秒一次，仅在 15 秒内没有其他事件时发）：

```
: ping
```

注释行（以 `:` 开头）被 SSE 规范定义为可忽略，正好用来防中间层超时断连，且不会污染前端解析逻辑。

### 3.5 断连与中断语义

| 场景 | 后端 | 前端 | 落库 |
|---|---|---|---|
| 用户点"停止" | 检测到断开 → 停止生成 | 保留已收到文本 | assistant 消息 `answer_status='error'`，`content` 存已生成部分 |
| 关页 / 断网 | 同上 | — | 同上 |
| 服务在生成中崩溃 | 启动时把 >10 分钟仍 `streaming` 的消息改成 `error` | 刷新看到"回答中断" | 定时清理，避免僵尸状态 |
| 用户重发同一问题 | 正常新建一轮 | — | 由 `trace_id` 区分两次请求 |

**已发出去的 token 收不回来**，所以中断一律记为 `error` 而不是"删掉这条"——审计要能复现"用户到底看到了什么"。

---

## 4. 其他接口

### 4.1 `POST /sessions`

```json
// 请求（均可省略）
{ "title": null, "scene": "employee" }

// 响应 200
{ "ok": true, "data": { "session_id": "0b7c...", "created_at": "2026-10-30T10:02:11+08:00" } }
```

`title` 为空时用第一轮问题前 20 字作为标题（架构 3.3 的"首轮自动生成"）。

### 4.2 `GET /sessions/{session_id}/messages?limit=50`

```json
{ "ok": true, "data": {
  "session_id": "0b7c...",
  "messages": [
    { "id": 12, "seq": 1, "role": "user", "content": "冻结期内能改银行账户吗？",
      "answer_status": null, "citations": [], "created_at": "2026-10-30T10:02:20+08:00" },
    { "id": 13, "seq": 2, "role": "assistant", "content": "银行账户变更在冻结期内可以受理……",
      "answer_status": "answered", "citations": [ { "index": 1, "...": "同 3.3" } ],
      "created_at": "2026-10-30T10:02:22+08:00" }
  ],
  "has_more": false
}}
```

按 `seq` 正序返回，前端直接追加即可。`answer_status='streaming'` 的消息也会返回，前端渲染为"回答中断"，避免用户刷新后看到一个永远转圈的气泡。

### 4.3 `GET /healthz`

```json
{ "ok": true, "data": {
  "index": { "ready": true, "chunk_count": 20, "embedding_model": "hash-tf-512",
             "doc_version": "1.0", "built_at": "2026-10-30T09:57:30" },
  "llm":  { "provider": "mock", "model": "mock-extractive" },
  "db":   { "ready": true, "path": "data/chat.db" }
}}
```

索引未就绪时 `ready:false` 且仍返回 200——**健康检查要能描述"坏"，而不是自己变成"坏"**。真正需要 503 的是 `/chat`。

### 4.4 `POST /messages/{message_id}/feedback`（可选）

```json
{ "feedback": -1 }   // 1 有用 / -1 无用
```

写 `chat_messages.feedback`，为阶段 3 扩评测集用。本期只用不改。

---

## 5. 数据模型（SQLite，沿用阶段 1 表结构）

### 5.1 原则：字段名、语义、约束不变，只降级类型

架构 3.3 / 3.4 是 PG 口径。SQLite 没有 `uuid` / `jsonb` / `timestamptz` / `bigserial`，因此做**机械映射**，不动任何业务字段：

| PG | SQLite | 说明 |
|---|---|---|
| `uuid` | `TEXT` | 存 32 位小写十六进制（去横线），可读性差但省 4 字节 |
| `bigserial` | `INTEGER PRIMARY KEY AUTOINCREMENT` | 追加型，天然有序 |
| `jsonb` | `TEXT` | 存 JSON 字符串，读写用 `json.dumps/loads`；**不引 JSON1 函数**，避免以后迁 PG 时 SQL 语义不一致 |
| `timestamptz` | `TEXT` | ISO-8601 带时区（`2026-10-30T10:02:20+08:00`），字典序即时序 |
| `smallint` | `INTEGER` | 同上 |

这样迁移到 PG 时只改 DDL 的列类型，**仓储层的 SQL 与实体字段一个字都不用动**。

### 5.2 DDL

```sql
-- 会话表：字段与架构文档 3.3 chat_sessions 一一对应
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                   TEXT PRIMARY KEY,
    user_id              TEXT,                       -- 本期固定 'demo-user'，阶段 3 接 users
    legal_entity_id      TEXT NOT NULL DEFAULT 'demo-entity',
    scene                TEXT NOT NULL DEFAULT 'employee',   -- employee / admin
    title                TEXT,                       -- 首轮自动取前 20 字
    summary              TEXT,                       -- 预留：滚动摘要，本期恒 NULL
    summarized_until_seq INTEGER NOT NULL DEFAULT 0, -- 预留：摘要已覆盖到第几条
    model                TEXT,
    token_used           INTEGER NOT NULL DEFAULT 0,
    message_count        INTEGER NOT NULL DEFAULT 0,
    last_message_at      TEXT,
    deleted_at           TEXT,                       -- 软删除，审计仍可查
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- 消息表：字段与架构文档 3.4 chat_messages 一一对应
CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
    content       TEXT NOT NULL,
    citations     TEXT NOT NULL DEFAULT '[]',   -- [{index,chunk_id,doc_id,doc_title,source_file,clause_no,heading_path,score,snippet}]
    tool_calls    TEXT,                         -- 预留：阶段 3 的工具调用轨迹
    tool_result   TEXT,
    answer_status TEXT NOT NULL DEFAULT 'answered',  -- streaming/answered/partial/refused/error
    token_in      INTEGER NOT NULL DEFAULT 0,
    token_out     INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    model         TEXT,
    trace_id      TEXT,
    feedback      INTEGER,                      -- 1 有用 / -1 无用 / NULL 未评
    created_at    TEXT NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_msg_session_seq ON chat_messages (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sess_last_msg    ON chat_sessions (last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_status       ON chat_messages (answer_status);
```

`ENUM` 类的取值全部写进 CHECK 约束——SQLite 的 `CHECK` 是真校验，能挡住"拼错状态名却静默入库"。

### 5.3 三个必须写进设计的工程细节

**① WAL + busy_timeout。** 连接一建立就执行：

```sql
PRAGMA journal_mode = WAL;      -- 读写不互相阻塞，SSE 长连接场景必需
PRAGMA synchronous = NORMAL;    -- 牺牲极小概率的最后一条事务，换 5~10 倍写入速度
PRAGMA busy_timeout = 5000;     -- 写锁竞争时等待而非立刻 SQLITE_BUSY
PRAGMA foreign_keys = ON;       -- SQLite 默认关闭，必须显式开
```

**② 绝不把 LLM 调用包进事务里。** 这是 SQLite 最常见的线上事故：一次生成 5 秒，事务就锁库 5 秒，其他人全部排队超时。因此一次问答的写库拆成三段独立短事务：

```
T1: 插 user 消息 + 插 assistant 占位行(status='streaming') + 更新 message_count
    —— 提交，释放锁
    （检索 + 生成，无事务）
T2: 更新 assistant 行：content / citations / answer_status / token / latency
    —— 提交
```

**③ `seq` 的分配。** `seq = COALESCE(MAX(seq),0)+1` 必须在事务内计算，靠 `UNIQUE(session_id, seq)` 兜底；冲突时重试一次。不要用自增 ID 顶替 `seq`——`seq` 是"会话内序号"，历史答案重放要靠它，而 `id` 可能因回滚跳号。

### 5.4 并发控制：用数据本身当锁

同一会话同时问两次，会写出两段交错的 `seq`，历史就乱了。做法：

- 判定条件 = 该会话存在 `answer_status='streaming'` 的消息 → 返回 `SES_004`（409）。
- 为什么不用内存字典：`uvicorn --workers 4` 时字典不共享，锁形同虚设；进程重启锁也没了。
- 附带收益：进程崩溃后残留的 `streaming` 行能被启动清理逻辑发现（改成 `error`），而内存锁做不到。

---

## 6. 历史上下文：最近 6 轮

### 6.1 取法

```sql
-- 当前 user 消息之前、最近 6 轮 = 最多 12 条
SELECT seq, role, content, answer_status
FROM chat_messages
WHERE session_id = :sid AND role IN ('user','assistant') AND seq < :current_seq
ORDER BY seq DESC
LIMIT 12;
```

取出后**反转成正序**，转成 `[{"role":"user"|"assistant","content":...}]`，交给 `Orchestrator.answer(history=...)`。

阶段 1 已经把这个参数留好了（`orchestrator.py:60`），注入位置是 `[system, *history, user]`，且**检索永远只用当前问题**——这一条不能改，否则多轮之后 query 被历史污染，召回精度会随轮次单调下降。

### 6.2 预算与截断

| 参数 | 默认 | 作用 |
|---|---|---|
| `history_max_turns` | 6 | 轮数上限 |
| `history_max_chars` | 2400 | 总字符上限（中文 1 字 ≈ 0.75 token，约 1800 token） |
| `history_msg_max_chars` | 400 | 单条超长截断并加 `……` |

从**最旧**开始丢，直到满足预算。为什么要双约束：轮数是产品语义，字符数是成本与窗口约束；只按轮数会撑爆上下文，只按字符数会让"6 轮"这个承诺失效。

`answer_status='refused'` 的历史**保留**。理由：模型需要知道"上一轮没查到依据"，否则会把上一轮的固定话术当成事实继续推理。但它在界面与 prompt 里都带状态标签。

### 6.3 指代消解：本期只做"检索侧追问合并"

真实场景里第 2 轮往往是"那超过 30 天呢？"——单独拿这句去检索，向量和 BM25 都召回不到东西，等于把多轮做成了单轮。

本期方案（便宜、可解释）：

```
触发条件（同时满足）：
  1) 删空白后长度 ≤ 12 字
  2) 含指代/省略线索："这个、那个、它、其、上面、刚才、还有、呢、那、超过、低于、如果"
     ／ 或者以"那/还有/如果/那么"开头
动作：
  query_used = 上一轮 user 问题 + " " + 当前问题      ← 只用于检索
  生成时的 user 消息仍然是当前问题原文（避免把两句话强行拼给模型）
```

不做 LLM 改写：多一次调用、多 300ms、且会引入新幻觉，而"多轮召回不准"这件事在 6 轮窗口内用规则能覆盖 80%。三个值全部可配（`followup_merge_enabled` / `followup_max_chars` / `followup_hints`）。

`query_used` 会随 `done` 事件返回，前端不展示，但 `--debug` 与日志里能看到，排查"为什么召回变了"时是唯一线索。

---

## 7. 流式 × 护栏：必须明说的矛盾

阶段 1 的护栏（`run_guard`）是**全文后置检查**：数字是否可溯源、角标是否越界、是否拒答。而流式一旦吐出 token 就收不回来。这两件事天然冲突，只有三种解法：

| 方案 | 首字延迟 | 护栏能否拦截 | 复杂度 | 结论 |
|---|---|---|---|---|
| **A. 边流边判，事后告警** | ~0.6s | ❌ 只能标记 | 低 | **推荐** |
| B. 缓冲到整句/整段再发 | +1~3s | 半（可在段落边界拦数字） | 中 | 备选，如果合规要求"绝不显示未溯源数字" |
| C. 全文生成完再切块喷出（伪流式） | +3~5s | ✅ 完全可拦 | 低 | 不推荐，失去流式意义 |

**推荐 A 的具体行为**：

1. 硬闸门（无召回拒答）在流开始前判定 —— 这类问题**根本不会有 `delta`**，0 token；
2. 生成过程中正常 `delta`；
3. 结束时跑 `run_guard`，若有告警 → 发 `event: guard`，`done.status` 记为 `partial`；
4. 前端在气泡底部渲染一条黄色提示条：**"⚠ 以下数字未能在制度原文中找到：50,000 元"**，并把状态标为"待核"。

这条链路的价值不在"拦住了"，而在**"说清楚了"**：审计能看到哪一次回答降级了、降级原因是什么，`chat_messages.answer_status` 与 `citations` 就是证据。要真拦得住，就把方案切成 B，只是一个开关的事（`stream_guard_mode: after | buffer`）——**所以请你在这条上拍板**（见第 11 节）。

---

## 8. 后端代码结构

### 8.1 新增 / 修改清单

标注沿用阶段 1 的约定：**★ = 直接复用，不改** · **＋ = 新增** · **△ = 修改（尽量小）**

```
＋ src/my_tool/api/__init__.py
＋ src/my_tool/api/app.py            # create_app()：装配、静态目录、异常处理器、启动清理
＋ src/my_tool/api/errors.py         # ApiError + 错误码常量表（唯一事实源）
＋ src/my_tool/api/schemas.py        # pydantic 请求/响应模型
＋ src/my_tool/api/sse.py            # SSE 编码器（event/data/心跳/序列化）
＋ src/my_tool/api/routes_chat.py    # POST /chat、POST /sessions、GET 历史、/healthz
＋ src/my_tool/store/__init__.py
＋ src/my_tool/store/db.py           # 连接、PRAGMA、建表、启动清理僵尸 streaming
＋ src/my_tool/store/chat_repo.py    # 会话/消息 CRUD、最近 6 轮查询（唯一写 SQL 的地方）
＋ web/index.html  web/app.js  web/style.css
＋ tests/test_repo.py  tests/test_api.py  tests/test_sse.py
＋ scripts/run_server.sh  scripts/run_server.ps1
＋ data/chat.db                     # 运行时生成，进 .gitignore

★ src/my_tool/core/chunker.py       # 不动
★ src/my_tool/core/retrieval/*      # 不动
★ src/my_tool/core/guard.py         # 不动（只在编排器里换调用时机）
★ src/my_tool/core/prompt.py        # 不动
△ src/my_tool/core/orchestrator.py  # 抽出 _prepare()/_finalize()，新增 answer_stream()；answer() 行为不变
△ src/my_tool/core/entities.py      # Citation 增加 doc_title / source_file（"文件名 + 片段"的硬需求）
△ src/my_tool/core/llm/base.py      # ChatModel 增加 chat_stream() 抽象方法
△ src/my_tool/core/llm/openai_compat.py  # 实现 stream=true 的逐行解析
△ src/my_tool/core/llm/mock.py      # 按句 yield（离线也能看到打字机效果）
△ src/my_tool/core/config.py        # 新增 chat.* / server.* 配置段
△ config/default.yaml               # 同上
△ pyproject.toml                    # + fastapi、uvicorn、httpx（测试用）
△ requirements.txt
△ .gitignore                        # + data/*.db、data/*.db-wal
```

### 8.2 分层与三条铁律

架构文档 00 节定的三条，本期照做：

```
api/     只管 HTTP：解析、校验、SSE 编码、错误翻译 —— 不含 SQL，不含 prompt
store/   只管持久化：全部 SQL 在这里 —— 不含业务判断
core/    只管业务：检索、生成、护栏 —— 不含 HTTP 概念
```

### 8.3 同步 / 异步的取舍（这是性能上的关键决定）

`Orchestrator`、`sqlite3`、`urllib` 全是同步的。两种写法：

- ❌ `async def chat()` 里直接调同步代码 → 阻塞事件循环，**一个用户的生成会卡住所有人的请求**；
- ✅ 端点写成普通 `def`，由 Starlette 自动丢进线程池；`StreamingResponse` 传**同步生成器**，Starlette 用 `iterate_in_threadpool` 逐个取。

本期选后者：零改造、零 `await` 传染。代价是每连接占一个线程（默认上限 40），对内部系统的并发量足够。生成模型换成异步 SDK（阶段 3）时，只需把 `answer_stream` 换成 async 版本，端点改 `async def`，`api/` 之外一行不动。

### 8.4 `answer_stream()` 的事件形制

```python
def answer_stream(self, question, *, top_k=None, history=None) -> Iterator[StreamEvent]:
    """yield 事件，最后由调用方从 last 事件或返回值取 RAGAnswer。

    StreamEvent = {"event": "citations|status|delta|guard|refused", "data": {...}}
    """
```

关键约束：**`answer()` 与 `answer_stream()` 必须共用同一套检索、prompt、护栏、引用组装代码**，只有"生成"这一段不同（一次拿全文 vs 逐字 yield）。否则两条路径会慢慢漂移，出现"CLI 答得对、网页答得错"这种最难查的问题。

---

## 9. 前端设计（单页，零依赖、零构建）

### 9.1 为什么不用 `EventSource`

`EventSource` 只支持 GET，且不能带 body。本接口是 `POST + JSON`，所以必须 `fetch()` + `ReadableStream` 手工解析：

```js
const resp = await fetch("/chat", {method:"POST", headers:{"Content-Type":"application/json"},
                                   body: JSON.stringify({session_id, question}), signal});
const reader = resp.body.getReader();
const decoder = new TextDecoder();
let buf = "";
// 按 \n\n 切事件块；剩下的半截留在 buf 里，等下一个 chunk
```

**必须处理的三个坑**（不写进去一定会踩）：

1. **chunk 边界会切断一个事件**，所以要留缓冲、只处理以 `\n\n` 结尾的完整块；
2. **一个事件可能有多个 `data:` 行**，按 SSE 规范要拼接；
3. **`:` 开头是注释行**（我们的心跳），必须跳过，否则 JSON 解析直接抛错。

### 9.2 打字机：二级缓冲

模型（尤其 mock 与高 TPS 的真实模型）会一次吐一大段。直接把 `delta` 文本 append 到 DOM 等于"闪一下全出来"，没有打字机。

做法：

```
网络层 ──delta──▶ pending 字符串缓冲
                      │
        requestAnimationFrame 循环，每帧取 1~2 个字 ──▶ 已显示文本 + 光标 ▍
                      │
              done → 每帧取 8 个字（加速收尾，最多 300ms 追上）
```

参数：正常 40 字/秒（阅读舒适），`done` 后 10 倍速。光标用 CSS 动画闪烁，`done` 后移除。

### 9.3 状态条

| 阶段 | 显示 | 细节 |
|---|---|---|
| 连接中 | `正在连接…` | 请求已发出、未收到首帧 |
| `retrieving` | **`正在检索制度…`** | 三个点的跳动动画 + 骨架气泡 |
| `generating` | `正在组织回答…` | 首字到达即消失，换成光标 |
| `done` | — | 显示耗时与状态标签（已核对 / 待核 / 未找到依据） |
| `error` | 红色提示条 | 显示 `message` + 重试按钮，附 `trace_id` 小字 |

**首字之前必须至少有东西在动**。若检索 200ms、模型首字 1.5s，那 1.5 秒的空白就是"卡住了"的观感——骨架气泡 + 状态文案是解决这个的唯一手段。

### 9.4 引用渲染

气泡底部一张"参考资料"卡片列表（默认展开，最多 3 条）：

```
[1] 《员工薪酬变更核对制度》第七条–第九条        员工薪酬变更核对制度_v1.0.md
    银行账户变更在冻结期内可受理但不参与当期发放，自下一周期起生效。
```

- 序号徽章与正文里的 `[1]` 双向联动：点正文角标高亮卡片，点卡片滚动回正文对应位置；
- `snippet` 是 120 字截断，够判断"是不是这条"；要看全文走阶段 3 的条款详情页；
- 无引用时不留空卡片，改为一行灰字"本次回答未引用制度条款"——**没有引用的答案本身就是可疑信号，界面不能装作正常**。

### 9.5 交互细节（都是中文场景的坑）

| 问题 | 处理 |
|---|---|
| 中文输入法回车会误发送 | `keydown` 时判 `e.isComposing \|\| e.keyCode === 229`，正在拼字就不发 |
| Enter 发送 / Shift+Enter 换行 | 常规，但要在 `textarea` 上做，不能用 `input` |
| 用户向上翻历史被自动滚动拽回底部 | 判断 `scrollHeight - scrollTop - clientHeight < 80` 才自动跟随，否则显示"↓ 回到最新"按钮 |
| 刷新丢会话 | `session_id` 存 `localStorage`，进页面先 `GET /sessions/{id}/messages`；404 则静默新建 |
| 生成中又发一条 | 发送按钮变"停止"，`AbortController` 中断上一条，服务端随之停止生成（省 token） |
| 消息里的 HTML 被当标签执行 | 全部走 `textContent`；只对 `[n]` 角标做白名单化处理（拆成文本节点 + `<sup>`），**不用 innerHTML 拼用户内容** |

### 9.6 渲染格式：纯文本，不做 Markdown

答案里会有 `- 列表` 和 `**加粗**`。本期**不做 Markdown 渲染**，理由三条：

1. 打字机逐字渲染半截 Markdown 会导致列表符号、加粗闪烁跳动；
2. 引 `marked.js` 违反"零依赖"，自己写正则解析 Markdown 又是 XSS 面的来源；
3. 用 `white-space: pre-wrap` + 角标高亮已经能读，收益/风险比不划算。

阶段 3 若确实需要表格、编号列表，再引 `marked` + `DOMPurify`，那时用"生成完再渲染、流式期间纯文本"的双段模式解决跳动。

---

## 10. 测试与验收

### 10.1 测试清单（沿用阶段 1 的 pytest 风格，全部离线、不依赖预建索引）

| 文件 | 覆盖点 |
|---|---|
| `tests/test_repo.py` | 建表幂等；`seq` 连续；最近 6 轮查询正确（含 `seq` 边界、排除当前条）；软删除；`unique(session_id,seq)` 冲突 |
| `tests/test_sse.py` | 编码格式（`event:`/`data:`/空行/心跳）；中文不转义；多行 data；payload 与 `done` 字段齐全 |
| `tests/test_api.py` | 200 流式全流程；`REQ_003` 空问题；`REQ_004` 超长；`SES_001` 404；`SES_004` 409；`IDX_001` 503（临时指向空目录）；流中断连后落库为 `error`；`/healthz` 三块状态 |
| `tests/test_api.py::test_refused_no_delta` | 越界问题：**没有任何 `delta`**、`status=refused`、token 为 0 |

用 `httpx.ASGITransport` 直接打 app，不起真实端口；SSE 用 `client.stream()` 逐行断言。

### 10.2 验收标准（可逐条演示）

1. `curl -N` 能看到逐帧事件，且 `session → status → citations → status → delta… → done` 顺序正确；
2. 首字延迟：mock 下 < 300ms，真实模型下 < 1.5s（不含冷启动）；
3. 界面能看见"正在检索制度…"，并在首字到达时切换成打字机；
4. 刷新页面，历史与引用完整恢复；
5. 越界问题（"报销流程怎么走"）显示"未找到依据"，且**没有 `citations` 事件**、`done.status=refused`、`citations_count=0`；
   （拒答话术本身仍走 `delta` 下发，所以会有 delta、但只有一句固定文案——见 3.3 ②）
6. 同一会话并发两次提问，第二个返回 409 且文案是中文；
7. 生成中关掉浏览器，服务端在 3 秒内停止生成，`chat_messages` 该行 `answer_status='error'`；
8. `pytest` 全绿（现有 39 项 + 新增约 20 项），`pyflakes` 无输出。

---

## 11. 已定方案（原「待确认」）

| # | 议题 | 结论 | 落地位置 |
|---|---|---|---|
| 1 | 流式与护栏的矛盾 | **A：边流边判 + 事后告警**（`partial` + 前端黄色提示条） | `orchestrator.answer_stream()` 的 `guard` 事件 |
| 2 | 前端文件形态 | **三文件**：`index.html` + `app.js` + `style.css` | `web/` |
| 3 | 身份与多用户 | **固定占位** `user_id='demo-user'` | `config.py` 的 `user_id` 字段 |
| 4 | Markdown 渲染 | **不渲染**，纯文本 + 角标高亮（打完再上角标） | `app.js` 的 `paintWithCites()` |
| 5 | 指代消解 | **只做检索侧合并**，不改写生成用的问题 | `orchestrator.build_retrieval_query()` |

---

## 12. 实现差异（与初稿的三处不同）

**① 心跳没做，理由写清楚。**
初稿说"每 15 秒发一帧 `: ping`"。真做下来发现：生成器在 `next()` 上阻塞时根本没有机会 `yield`，
要实现真心跳必须把上游迭代挪进工作线程 + 队列，而那样"客户端断开 → 关闭正在另一个线程执行的生成器"
会抛 `ValueError: generator already executing`，需要额外的取消协议。
权衡后本版**不做心跳**：靠 `citations`/`delta` 的持续流量保活；若上游静默超过 60 秒，由 nginx 报 504。
触发条件是"模型思考超过 1 分钟"，在 900 token 上限下极少发生。`sse.comment()` 保留，测试已覆盖，
将来接工作线程方案时直接用。

**② 拒答走 `delta`（初稿已定），并补充了实现细节。**
拒答时发 `status{stage:"refused"}` + 一句固定话术的 `delta`，然后 `done{status:"refused"}`。
所以"没有 delta"这个验收标准改成了"没有 `citations`"（见 10.2 第 5 条）。

**③ 引用片段压成一行。**
切片正文里带换行与章节标题，原样给前端会让卡片高度忽高忽低。
`api/schemas.py::_one_line()` 把连续空白折成单个空格并截 140 字——只影响排版，不删内容。

**另外补充的两个小东西**（初稿未提，实现时需要）：

- `EventSource` 不可用于本接口（只支持 GET、不能带 body），前端改用 `fetch + ReadableStream` 手工解析，
  代码里对"chunk 切断事件 / 多行 data / 注释行"三个坑都做了处理；
- 前端"打完再上角标"：流式期间纯文本，收到 `done` 且打字追平后，才把 `[1]` 替换成可点 `<sup>`。
  否则半截的 `[1` 会被拆成文本节点，标点一到位就要重排，视觉抖动明显。

