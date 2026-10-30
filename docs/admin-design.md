# 管理后台 · 页面与接口设计 v0.1

> **状态：待确认。** 本文只定页面、接口与数据模型，不含实现代码。确认后按第 8 节分批出代码。
>
> 依据：`docs/product/薪酬变更核对系统_技术架构设计_v0.1.html`（0.3 引用回 L0、3.3/3.4 会话表、3.5 制度不可覆盖更新、4.3 评测口径）。
> 复用：现有 `api/errors.py` 错误契约、`store/` 仓储分层、`core/chunker.py` 切片器、`core/prompt.py`、`Orchestrator`。

---

## 0. 现状与三个硬约束

动手前必须先说清现状，因为它决定了后台能做什么、不能做什么：

| 现状 | 对后台的直接影响 |
|---|---|
| `chat_sessions.user_id` 一直是固定的 `demo-user`（`Settings.user_id`），没有登录态 | 审计页"按员工筛选"没有数据基础。**必须先有 users 表与登录态，审计才有意义** |
| 索引是**单文档、整体全量重建**（`pipeline.build_index` 读 `s.doc_path` 一个文件） | 知识库要支持"多文档 + 删一个"，必须改成多文档清单 + 增量向量化 + 热切换 |
| 提示词是**模块级常量**（`prompt.py` 的 `SYSTEM_PROMPT`） | 要"改完立即生效"，必须把模板移到 DB，并给编排器注入一个 provider |
| 路由层没有任何鉴权，`/chat` 匿名可调 | 加鉴权会**改变现有前端行为**，必须用开关控制，否则 71 项测试与现有演示全挂 |

最后一条是最容易被低估的：这不是"加个登录页"，而是**所有既有接口的调用方都要变**。

---

## 1. 角色与权限矩阵

两个角色，不做 RBAC 权限点抽象（两个角色撑不起权限系统，硬做只会让代码变复杂）：

| 能力 | `employee` | `admin` |
|---|:---:|:---:|
| 问答 `/chat` | ✅ | ✅ |
| 查看会话历史 | 仅**自己**的 | 全部 |
| 知识库列表 / 详情 | ❌ | ✅ |
| 上传 / 删除制度、重建索引 | ❌ | ✅ |
| 提示词编辑 / 回滚 | ❌ | ✅ |
| 对话审计、标记违规 | ❌ | ✅ |
| 用户管理 | ❌ | ✅（本期用 CLI，不做页面） |

**接口级校验**落在两个依赖上，而不是散在每个 handler 里：

- `current_user()` —— 解析 JWT，查库取**最新** role/status；失败抛 `AUTH_001`
- `require_admin()` —— 依赖 `current_user`，role≠admin 抛 `AUTH_003`

对已有接口的**归属校验**同样必须补上，这是当前最大的越权口子：
`GET /sessions/{id}/messages` 现在只要知道 session_id 就能读任意人的对话 —— 改为 `session.user_id == current_user.id or role == 'admin'`。

---

## 2. 页面清单（7 个）

服务端渲染，Jinja2 + 一套手写 CSS（左侧固定导航 + 内容区卡片，对齐 SaaS 后台的观感）。零前端构建。

| # | 路径 | 页面 | 角色 | 核心内容 |
|---|---|---|---|---|
| P1 | `/login` | 登录 | 匿名 | 用户名 + 密码；按角色跳转（admin→`/admin`，employee→`/`）；登录失败不区分"用户不存在/密码错" |
| P2 | `/admin` | 概览 | admin | 4 个数字卡（文档数 / 切片数 / 今日问答 / 拒答率）+ 索引状态（就绪/重建中/失败 + 上次重建时间）+ 待处理疑似违规数 |
| P3 | `/admin/kb` | 知识库 | admin | 上传区（拖拽 + 点击）+ 文档表格（标题/版本/切片数/生效期/状态/上传人/时间）+ 行内「删除」「查看切片」+ 顶部「重建索引」 |
| P4 | `/admin/kb/{doc_id}` | 文档详情 | admin | 文档元信息 + **切片逐条预览**（切片号 / 条款范围 / 字数 / 正文）。这是判断"切得好不好"的唯一入口 |
| P5 | `/admin/prompts` | 提示词模板 | admin | 左：system prompt 与 user template 编辑框；右：当前生效版本、变量校验结果、历史版本列表（可回滚）。底部「预览」按钮用当前草稿跑一次真实问答 |
| P6 | `/admin/audit` | 对话审计 | admin | 筛选条（员工 / 时间范围 / 回答状态 / 仅看疑似 / 关键词）+ 分页表格（时间/员工/问题/状态/风险提示/标记） |
| P7 | `/admin/audit/{session_id}` | 会话详情 | admin | 完整多轮对话回放（含引用卡片）+ 每条回答的 `[标记疑似]` `[确认违规]` `[清除标记]` + 检索分数与 token 消耗 |

不做用户管理页（P8）：两个角色、几个账号，`payroll-check user add/reset/list` 三条命令更省事。表已备好，将来要页面直接加路由。

**每个页面的失败态都要能看**：上传超限、PDF 无文本层、重建失败、模板变量缺失 —— 都渲染成页面内联的错误条，而不是白屏或 JSON 弹窗。

---

## 3. 接口清单

### 3.1 认证

| 方法 | 路径 | 入参 | 成功 | 失败 |
|---|---|---|---|---|
| GET | `/login` | `?next=` | 200 HTML（已登录则 302 到 `next` 或角色首页） | — |
| POST | `/login` | form: `username, password, next, _csrf` | 302 + `Set-Cookie: pcc_token`（HttpOnly/SameSite=Lax）+ csrf cookie | 401 → 重渲染登录页带中文错误 |
| POST | `/logout` | `_csrf` | 302 `/login` + 清 cookie | — |

JWT 同时支持 `Authorization: Bearer <token>`，方便 `curl` 与将来的 API 调用方。

### 3.2 页面（全部 html，写操作走 PRG：POST → 302 → GET）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/admin` | admin | P2 |
| GET | `/admin/kb` | admin | P3，支持 `?status=active|deleted` |
| POST | `/admin/kb/upload` | admin | multipart：`file`（.md/.txt/.pdf，≤10MB）+ `doc_id/title/version/effective_from/effective_to/region_code`（留空则从 front-matter 或文件名推断） |
| POST | `/admin/kb/{doc_id}/delete` | admin | 软删（`status='deleted'`）+ 触发重建；要求表单二次确认 `confirm=1` |
| POST | `/admin/kb/rebuild` | admin | 手动全量重建（异步，返回 302 后页面显示"重建中"） |
| GET | `/admin/kb/{doc_id}` | admin | P4 |
| GET | `/admin/prompts` | admin | P5 |
| POST | `/admin/prompts` | admin | 保存新版本并立即生效（新行 `is_active=1`，旧行置 0） |
| POST | `/admin/prompts/{version}/activate` | admin | 回滚：只切 `is_active`，不复制内容 |
| POST | `/admin/prompts/preview` | admin | 用当前草稿跑一次真实问答，返回 HTML 片段（不落库） |
| GET | `/admin/audit` | admin | P6，参数见下 |
| GET | `/admin/audit/{session_id}` | admin | P7 |
| POST | `/admin/audit/messages/{message_id}/flag` | admin | `flag=suspect|confirmed|cleared` + `note`；写 `message_audits` |

`GET /admin/audit` 查询参数：`user_id`、`from`、`to`（YYYY-MM-DD）、`status`（answered/partial/refused/error）、`risk=1`（只看规则预筛命中）、`q`（问题关键词）、`page`（默认 1，每页 20）。

### 3.3 对既有接口的改动

| 接口 | 改动 |
|---|---|
| `POST /chat` | 需登录（`AUTH_REQUIRED=1` 时）；`user_id` / `legal_entity_id` 取自登录态，不再用 `Settings.user_id` |
| `POST /sessions` | 同上 |
| `GET /sessions/{id}/messages` | **加归属校验**（本人或 admin） |
| `POST /messages/{id}/feedback` | 加归属校验 |
| `GET /healthz` | 增加 `auth`（启用与否）与 `index.rebuild`（idle/building/failed）字段 |

`AUTH_REQUIRED=0` 时全部退回当前行为（`demo-user`），测试与离线演示照旧 —— 这不是妥协，是让改造可回退。

### 3.4 新增错误码（沿用 `errors.py` 单一事实源，code 永不复用）

| code | HTTP | 中文文案 | 触发 |
|---|---|---|---|
| `AUTH_001` | 401 | 登录已过期，请重新登录 | 无/坏 token |
| `AUTH_002` | 401 | 用户名或密码不正确 | 登录失败（**不区分原因，防账号枚举**） |
| `AUTH_003` | 403 | 没有权限访问该功能 | 角色不足 |
| `AUTH_004` | 429 | 登录尝试过于频繁，请稍后再试 | 同账号 5 分钟内失败 5 次 |
| `AUTH_005` | 400 | 密码强度不足（至少 8 位，含字母与数字） | 改密 |
| `KB_001` | 400 | 不支持的文件类型，仅接受 .md / .txt / .pdf | 上传 |
| `KB_002` | 413 | 文件过大（上限 10 MB） | 上传 |
| `KB_003` | 409 | 该文档已存在相同版本，请提升版本号或先删除旧版 | doc_id+version 冲突 |
| `KB_004` | 422 | 文件解析失败：未提取到有效正文（扫描件需先 OCR） | PDF 无文本层 / md 无结构 |
| `KB_005` | 409 | 索引正在重建，请等待完成后再操作 | 并发重建 |
| `KB_006` | 500 | 索引重建失败，已保留原索引 | 重建异常（**旧索引仍可用**） |
| `PRM_001` | 400 | 模板缺少必需占位符：{missing} | 保存模板 |
| `PRM_002` | 404 | 模板版本不存在 | 回滚 |
| `AUD_001` | 404 | 消息不存在，无法标记 | 标记 |

页面请求的 401 不返回 JSON，而是 **302 到 `/login?next=<原路径>`**；403/404 渲染 HTML 错误页。API 请求（`/chat` 等）保持 JSON —— 靠 `Accept` 头与路径前缀区分。

---

## 4. 数据模型增量

四张新表 + 一处列增量。类型继续按 SQLite 降级，迁 PG 时只换 DDL。

```sql
-- 用户。密码用 pbkdf2_sha256（标准库 hashlib），存 "pbkdf2_sha256$260000$salt_b64$hash_b64"
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    employee_no   TEXT,                       -- 工号，审计按此筛
    department    TEXT,
    role          TEXT NOT NULL CHECK (role IN ('employee','admin')),
    password_hash TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    last_login_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 制度文档。一处关键：source_file 存原始文件名（引用卡片要展示），storage_path 存物理路径
CREATE TABLE IF NOT EXISTS policy_documents (
    doc_id         TEXT PRIMARY KEY,          -- POL-CMP-2026-001
    title          TEXT NOT NULL,
    doc_type       TEXT NOT NULL DEFAULT 'policy',
    version        INTEGER NOT NULL DEFAULT 1,
    region_code    TEXT NOT NULL DEFAULT '',
    effective_from TEXT,
    effective_to   TEXT,
    source_file    TEXT NOT NULL,
    storage_path   TEXT NOT NULL,
    checksum       TEXT NOT NULL,             -- 内容摘要，决定是否需要重新向量化
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','deleted')),
    chunk_count    INTEGER NOT NULL DEFAULT 0,
    char_count     INTEGER NOT NULL DEFAULT 0,
    uploaded_by    TEXT REFERENCES users(id),
    indexed_at     TEXT,
    deleted_at     TEXT,
    deleted_by     TEXT,
    created_at     TEXT NOT NULL
);

-- 提示词模板。版本不可变：每次保存插入新行，(key,version) 唯一，is_active 单活
CREATE TABLE IF NOT EXISTS prompt_templates (
    id            TEXT PRIMARY KEY,
    key           TEXT NOT NULL DEFAULT 'rag_answer',
    version       INTEGER NOT NULL,
    name          TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    user_template TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (key, version)
);

-- 审计标记。独立表而不是给 chat_messages 加列：标记是"人+时间+结论"的事件，
-- 可多人多次、可撤销、要留痕；加列只能存最后一次结果，把过程丢了。
CREATE TABLE IF NOT EXISTS message_audits (
    id         TEXT PRIMARY KEY,
    message_id INTEGER NOT NULL,              -- chat_messages.id（assistant 行）
    session_id TEXT NOT NULL,
    flag       TEXT NOT NULL CHECK (flag IN ('suspect','confirmed','cleared')),
    reason     TEXT,                          -- 规则码（R1..R5）或 'manual'
    note       TEXT,
    auditor_id TEXT REFERENCES users(id),
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_doc_status   ON policy_documents (status);
CREATE INDEX IF NOT EXISTS idx_prompt_key   ON prompt_templates (key, is_active);
CREATE INDEX IF NOT EXISTS idx_audit_msg    ON message_audits (message_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_msg_created  ON chat_messages (created_at DESC);
```

列增量（幂等迁移，先 `PRAGMA table_info` 判断再加）：

```sql
ALTER TABLE chat_messages ADD COLUMN prompt_version TEXT;   -- 追溯当时用的是哪版模板
```

**为什么要这一列**：提示词进了后台可随意改，一旦答案出问题，审计必须能回答"当时用的是哪版"。这和架构文档 0.3「引用回 L0」是同一条原则 —— 结论必须可追溯。

---

## 5. 四个不显然的决策

### 5.1 索引重建：全量重建 + 热切换 + 向量缓存

现有 `build_index` 只认一个文件，且 `VectorStore` 的 FAISS 索引是**整体**的（无法从索引里单独删掉某个文档的向量）。所以：

- **删除文档必须重建**，没有"局部删除"这种可能；
- 但**不必重新调用 embedding**。落盘时把向量也存下来（`vectors.npy` + `chunks.jsonl` 里每片带 `doc_id` 与 `doc_checksum`），重建时：
  1. 读 `policy_documents where status='active'`；
  2. checksum 未变的文档 → **复用已有向量**；
  3. 只有新增/变更的文档才调 embedding；
  4. 合并所有向量 → 建新 FAISS → 落盘 → 原子替换。

这条缓存不是优化洁癖：改一份文档就让 50 份重新 embed，既费钱又让"改个字"等两分钟。

**热切换**：重建在后台线程跑，完成后执行 `ctx.orchestrator = new_orch`。Python 的引用赋值是原子的，正在处理中的请求持有旧对象继续跑完，新请求拿到新对象 —— 重建期间服务不中断。重建中 `/chat` 用的是**旧索引**，这点必须如实显示在页面上（"重建中，当前仍按上一版回答"），否则管理员会以为新文件立刻生效了。

失败时保留旧索引（`KB_006`），并记下错误原因到 `ctx.rebuild_state.error`，页面可看、可重试。

### 5.2 提示词：DB 为准、缓存 5 秒、版本不可变

- **单一事实源在 DB**。代码里保留默认模板，但仅用于首次 seed 与"恢复默认"。`prompt.py` 的常量降级为 `DEFAULT_SYSTEM_PROMPT`。
- **生效机制**：`PromptProvider.get_active()` 进程内缓存 5 秒。保存时本地 `reload()` 立即失效，所以单 worker 下是"立刻生效"；多 worker 下最多 5 秒延迟。要写得诚实，不要宣称"绝对实时"。
- **版本不可变**：保存 = 插入新行并切 `is_active`，绝不 UPDATE 历史行。回滚 = 把旧行重新 `is_active=1`。这样"上周那版能答、这版答不出"可复现。
- **保存前强制校验**：`user_template` 必须含 `{context}` 和 `{question}`，`system_prompt` 长度 50–8000。缺占位符就拒绝保存（`PRM_001`）—— 否则下一次提问直接抛 `KeyError`，事故现场在员工页而不是后台。
- **预览**：用草稿跑一次真实检索+生成，显示答案与引用，不落库。改了 prompt 不看效果就保存，是这类后台最典型的翻车方式。

### 5.3 PDF：只吃文本层，扫描件明确拒绝

`pypdf` 已可用。但必须说清边界：

- 文本层 PDF → 抽取文本 → **转成伪 Markdown**（识别 `第X章/第X条/一、1.` 作为标题行，其余按段落）→ 喂给**现有 chunker**。复用切片器是关键，否则又多一套切片逻辑。
- 每页平均提取字符数 < 20 → 判定为扫描件 → `KB_004`，文案直说"需 OCR，本期不支持"。**不要静默产出空索引**，那是最坏的失败方式（上传成功、问答永远答不出）。
- PDF 表格还原不可靠（pypdf 只给文本流）。页面在解析结果里显示"识别到 N 个条款 / M 个切片"，让管理员自己判断质量，而不是假装解析得很好。

### 5.4 审计：人工标记与自动预筛分离

"疑似违规"由两类信号构成，**必须分开存**：

- **人工标记** → 写 `message_audits`（谁、何时、什么理由、可撤销）；
- **自动预筛** → **实时计算、不落库**。列表页对当前 20 条算一次，只作为"风险提示"徽标。

规则（不需要 LLM，可解释、零成本）：

| 码 | 规则 | 判据 |
|---|---|---|
| R1 | 未溯源数字 | 回答含金额/百分比，且该数字不在任何 citation 片段中 |
| R2 | 拒答却给结论 | `answer_status='refused'` 但正文 > 40 字且含"元 / %" |
| R3 | 推测语气 | 含"通常 / 一般来说 / 大概是 / 应该是 / 建议您" |
| R4 | 无引用作答 | `answered` 但 `citations` 为空 |
| R5 | 低分强答 | 最高检索分 < 拒答阈值 × 1.2（勉强过闸） |

为什么不落库：自动标记一旦混进人工标记表，"确认违规 3 条"这个数字就不可信了。分开之后，人工表永远是"人真的看过"的干净集合。

### 5.5 JWT 不迷信无状态：每请求查一次库

标准 JWT 是无状态的，代价是**禁用账号、改角色要等 token 过期**（8 小时）。这个后台里管理员会因为"某人被停职"而禁用账号，等 8 小时不可接受。

所以校验时**查一次 users 表**取最新 `role` / `status` —— SQLite 单行主键查询约 0.05ms，对一个内部后台完全不构成负担。换来的是"禁用立即生效、改角色立即生效"。

无状态的收益在这个规模上是理论收益，可撤销性才是真需求。

---

## 6. 对既有代码的改动

| 文件 | 改动 | 风险 |
|---|---|---|
| `store/db.py` | SCHEMA 加 4 表；新增 `migrate(conn)` 做幂等列增量 | 低 |
| `store/` 新增 | `user_repo.py` / `policy_repo.py` / `prompt_repo.py` / `audit_repo.py` | 低（守"SQL 只在 store"铁律） |
| `core/prompt.py` | `build_prompt(..., template=None)` 可注入；常量改名 `DEFAULT_*` | 低（`None` 走原路径，测试不动） |
| `core/orchestrator.py` | 构造器加可选 `prompt_provider`；`_prepare` 取模板 | 中（改的是最热路径，需回归） |
| `core/index_manager.py` 新增 | 多文档重建 + 热切换 + 状态机 | 中 |
| `core/ingest.py`、`core/pdf.py` 新增 | 上传 → 解析 → 切片 → 向量 | 中 |
| `api/auth.py` 新增 | 密码哈希、JWT、依赖注入、CSRF | 中（安全相关，需专门测试） |
| `api/routes_admin.py` 新增 | 7 个页面路由 | 低 |
| `api/app.py` | 注册 admin 路由、Jinja2、HTML 异常处理、`/static` 挂载、启动 seed | 中 |
| `api/routes_chat.py` | `user_id` 取自登录态 + 归属校验 | 中（改的是既有接口） |
| `api/errors.py` | 新增 13 个错误码 | 低 |
| `core/config.py` | 加 `auth_required / auth_secret / jwt_ttl / upload_dir / upload_max_mb` | 低 |
| `main.py` | 加 `user` 子命令（add / reset / list） | 低 |
| `web/index.html`·`app.js` | 资源引用改 `/static/...`；401 时跳 `/login` | **破坏性**，见下 |

**唯一破坏性改动**：现在 `app.mount("/", StaticFiles(...))` 把根路径整个让给了静态目录，无法在 `/` 上做鉴权。改为：
- `GET /` 走显式路由（校验登录 → 返回 `index.html`）；
- 静态资源挂 `/static`，`index.html` 里的 `./app.js` `./style.css` 改成 `/static/app.js`。

不做这一步，员工页就没法要求登录。

---

## 7. 依赖增量

| 包 | 用途 | 备注 |
|---|---|---|
| `jinja2` | 服务端渲染 | 必需 |
| `python-multipart` | 文件上传表单 | 必需（FastAPI 强依赖） |
| `PyJWT` | 签发/校验 JWT | 必需。自己实现 HS256 只有 40 行，但 alg 混淆、`compare_digest`、`exp` 校验这类坑没有理由是自造的 |

已在环境内、无需新增：`pypdf 6.21.1`。密码哈希用标准库 `hashlib.pbkdf2_hmac`，不引 `passlib`。

---

## 8. 分批交付（每批可独立运行 + 带测试）

| 批次 | 内容 | 验收 |
|---|---|---|
| **1** 鉴权地基 | users 表、密码哈希、JWT、登录/登出、`current_user`/`require_admin`、CSRF、HTML 错误页、CLI `user` 命令、既有接口归属校验 | 未登录访问 `/admin` 跳登录；employee 访问后台 403；越权读他人会话被拒 |
| **2** 知识库 | 多文档 ingest、PDF 转换、IndexManager 热切换、P3/P4 页面、上传/删除/重建 | 上传一份 md 后能问答命中；删除后不再召回；重建期间 `/chat` 不中断 |
| **3** 提示词 | prompt_templates 表、Provider 缓存、P5 页面、版本历史/回滚、编排器接入 | 改一条规则后下次提问立即体现；缺占位符拒绝保存；回滚生效 |
| **4** 审计 + 概览 | P2 概览、P6/P7、筛选分页、标记、5 条预筛规则 | 按员工/时间筛出正确记录；标记可撤销；预筛徽标与规则一致 |
| **5** 打磨 | SaaS 样式统一、`docs/admin.md`、全量回归（预计 71 → 110+ 测试） | pyflakes 无输出、pytest 全绿、eval 8/8 不退 |

每批结束都会真起服务跑一遍，不只是单测。

---

## 9. 待确认

| # | 议题 | 选项 | 建议 |
|---|---|---|---|
| 1 | 现有 `/chat` 是否强制登录 | **开关控制，默认开** / 立刻强制 / 暂不强制 | **开关**。`AUTH_REQUIRED=0` 退回 demo-user，71 项测试与离线演示不受影响，改坏了能一键回退 |
| 2 | 用户管理 | **本期 CLI**（add/reset/list）/ 做页面 / 只 seed 固定账号 | **CLI**。两个角色撑不起一套用户管理界面，但账号必须能加、能改密 |
| 3 | PDF 范围 | **仅文本层** / 加 OCR（paddleocr/tesseract）| **仅文本层**。OCR 是另一个工期，且识别质量直接决定切片质量，不该混在这期 |
| 4 | 提示词版本 | **全留 + 可回滚** / 只存当前 | **全留**。prompt 是可以把答对变成答错的东西，没有回滚等于没有保险 |

另两个默认项，有异议一起说：**审计预筛用规则不用 LLM**（可解释、零成本）；**密码用 pbkdf2-sha256 26 万轮**（标准库，不引 passlib）。

确认后我从批次 1 开始出代码。
