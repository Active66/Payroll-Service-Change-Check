# 薪酬变更核对系统 · 制度 RAG 内核 + 管理后台

把《员工薪酬变更核对制度》变成**能举证、不编造**的问答能力：混合检索定位条款，答案逐句带角标，数字逐个回查原文，没有依据就拒答。

内核是库 + CLI，外面套了 SSE 流式问答服务（FastAPI）、单页聊天界面，以及一套服务端渲染的管理后台（知识库 / 提示词 / 对话审计）。**核心目标不是"接上大模型"，而是让答案能被审计**。

```
问题 ──┬─ 向量检索 ─┐
       └─ BM25 ────┴─ RRF 融合 ─ top3 ─ 闸门 ─ LLM ─ 护栏 ─ 带角标的答案
                                          │
                                    无依据 → 直接拒答（0 token）
```

## 三个不妥协的设计

| 关注点 | 做法 |
|---|---|
| **引用可举证** | 切片按条款装箱、标题内联、记 `clause_range`；角标到 `chunk_id` 的映射由系统持有，模型无法伪造出处 |
| **数字不编造** | 答案里每个带单位的数字都回查原文，查不到就标记并降级 `partial` |
| **没依据就闭嘴** | 两路最高原始分都低于阈值 → 拒答，且不调用模型 |

管理后台是这三条的延伸：制度可换（但旧版本可追溯）、提示词可改（但每版都可回滚、每条答案都记下用了哪版）、答案可审（规则预筛 + 人工标记分离）。

## 快速开始

需要 Python ≥ 3.10。

```bash
pip install -r requirements.txt
pip install -e .            # 可选：装成 payroll-check 命令
```

Windows PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
$env:PYTHONPATH="src"       # 不执行 pip install -e . 时需要

python -m my_tool build
python -m my_tool serve      # 起 Web 服务 → 浏览器打开 http://127.0.0.1:8000/
python -m my_tool ask "冻结期内可以改银行账户吗？"
python -m my_tool chat
python -m my_tool eval
```

Linux / macOS：

```bash
bash scripts/build.sh        # 建 venv → 装依赖 → 建索引 → 跑评测，一步到位
source .venv/bin/activate
python -m my_tool ask "2026 年上海社保缴费基数上限是多少？" --debug
```

一行看效果（不落盘、不需要 Key）：

```bash
python examples/demo.py
```

## 命令

| 命令 | 作用 |
|---|---|
| `build` | 登记制度 + 解析 → 500 字切片 → 向量化 → 写 FAISS |
| `serve [--port 8000]` | 启动 Web 服务（`/chat` SSE + 员工界面 + 管理后台） |
| `ask "问题" [--debug]` | 单次提问，`--debug` 看双通道名次与耗时 |
| `chat` | 交互模式（`/sources` 看原文、`/debug` 切调试） |
| `eval` | 内置 8 题检索评测（hit@3 / clause@3） |
| `info` | 当前配置与索引 manifest |
| `user add/reset/list` | 账号管理（后台用，不做用户管理页面） |
| `dept add/list/assign` | 部门（两级）与员工归属，问答按部门统计用 |
| `apikey add/list/revoke` | 外部系统 API Key（`/api/v1/*` 用） |
| `data seed/params/employees` | 制度参数与人事档案（Agent 规则引擎的数据来源） |
| `check --type … --emp …` | 命令行跑一次规则校验，看到阻断项与风险等级 |

装成命令后 `payroll-check` 等价于 `python -m my_tool`。

## 问答服务

```bash
python -m my_tool build        # 首次必做：索引没建好，/chat 会返回 IDX_001
python -m my_tool serve        # http://127.0.0.1:8000/
```

Windows 上还有个双击即用的入口：`scripts\run_server.bat` —— 起服务（最小化窗口）+ 自动开浏览器，关掉那个窗口即停止服务。端口已在监听时它不会再起一份，只负责开页面。

浏览器打开即是聊天界面：`正在检索制度…` 状态条 → 打字机输出 → 底部"引用来源"卡片（文件名 + 条款号 + 片段）。

| 接口 | 用途 |
|---|---|
| `POST /chat` | 提问，`text/event-stream` 逐帧返回（`session/status/citations/delta/guard/done/error`） |
| `POST /sessions` | 新建会话 |
| `GET /sessions/{id}/messages` | 刷新后恢复历史（含引用） |
| `GET /healthz` | 索引 / 模型 / 数据库自检 |
| `POST /messages/{id}/feedback` | 有用 / 无用 |

错误一律 `{ok:false, error:{code, message, trace_id}}`，`message` 是中文且不含堆栈；错误码表见
[`docs/chat-api-design.md`](docs/chat-api-design.md)。

会话落 SQLite（`data/chat.db`，字段沿用架构文档 3.3/3.4），每次请求带最近 6 轮历史。
**流式与护栏的取舍**：护栏只能事后判，所以边流边判 + `guard` 事件告警，不撤回已发出的字——理由与改法见设计文档第 7 节。

## 管理后台

浏览 `http://127.0.0.1:8000/login`。首次启动会自动创建管理员，密码来自 `ADMIN_SEED_PASSWORD`；
没设则随机生成并**只在启动日志里打印一次**。

```bash
python -m my_tool user list
python -m my_tool user add --username zhangsan --name "张三" --role employee
python -m my_tool user reset --username admin
```

| 页面 | 做什么 |
|---|---|
| `/admin` | 概览：索引状态、今日提问与拒答率、待处理违规、**按部门看问答** |
| `/admin/kb` | 上传 md/pdf → 切片 → 向量化入库；**版本台账**（换版自动替代旧版）；重建索引；切片逐条预览 |
| `/admin/prompts` | 编辑系统提示词；草稿预览；版本历史与回滚 |
| `/admin/audit` | 按员工/时间/关键词筛问答；规则预筛；标记疑似违规 |
| `/admin/audit/{sid}` | 会话回放：引用原文、prompt 版本、token、标记记录 |

四条设计取舍（细节见 [`docs/admin-design.md`](docs/admin-design.md)、用法见 [`docs/admin.md`](docs/admin.md)）：

- **删文档必须重建索引**（FAISS 索引无法局部摘除向量），但内容没变的切片复用向量缓存，不重复调用 embedding；重建在后台线程完成，`ctx.orchestrator` 原子替换，**重建期间问答不中断**，用的是上一版索引；
- **提示词存库、版本不可变**，保存即新版本，回滚只是切换 `is_active`；`{context}` / `{question}` 缺失直接拒绝保存；角标格式由系统强制追加（护栏按它核对引用）；
- **审计的自动预筛不落库**，只有人工标记进 `message_audits` —— 否则"确认违规 3 条"这个数字不可信；
- **JWT 每请求查一次库**，换来"禁用账号立即生效"；无状态的收益在这个规模上是理论收益，可撤销性才是真需求。

`AUTH_REQUIRED=0` 可退回演示模式（不做登录校验），用于离线演示与回归测试。

---

## 集成与上线前加固

细节见 [`docs/integration-design.md`](docs/integration-design.md)，用法见 [`docs/integration.md`](docs/integration.md)。

**制度换版不再有空窗**。唯一键从 `doc_id` 改为 `(doc_id, version)`：传同一个编号的新版本，
旧版自动置为 `superseded`，全程索引不断档（重建期间仍用上一版，完成后原子热切换）。
同编号同版本重复上传会被拒绝——制度不能静默覆盖，否则"当时生效的是哪一版"就说不清。
生效日在未来的版本入库但不进索引，列表页标「待生效」；发错了用「回滚到本版」，而不是删除。

**问答按部门可看**。`dept` 两级组织（一级 + 子部门），员工用 `dept assign` 挂上去，
概览页给出近 30 天各组的提问数、参与人数、拒答率。分组键是部门表 id 而不是用户表的
`department_id`——否则已删部门的悬挂引用会让「未归属」裂成两行、数字各算一半。

**语音输入**。浏览器内置 Web Speech API（Chrome/Edge），音频不出本机、服务端零改动。
识别结果只填进输入框不自动发送：错一个字检索就可能错一条条款，得让人看一眼再回车。

**对外接口 `/api/v1/ask`**。给 HR/工单系统用，走 API Key 而非 JWT（人是 JWT，系统是 Key），
非流式、无会话、按 Key 滑动窗口限流。返回里的 `status` 必须被调用方处理：
`refused` 是"制度里没依据"，该转人工，不该当成一条答案展示给员工。

---

## Agent 工具链

细节见 [`docs/agent-design.md`](docs/agent-design.md)（为什么这么设计）与
[`docs/agent.md`](docs/agent.md)（怎么用）。

从"固定流程问答"升级为"会自主决策的智能体"，给模型 9 个工具：读 5 个
（`lookup_policy` / `lookup_param` / `get_employee` / `get_change_order` / `check_rules`）、
写 3 个（`create_draft` / `submit_draft` / `escalate_to_human`）、交互 1 个（`ask_clarification`）。

三条不妥协：

- **数字只从 `policy_params` 来**。16 个参数每个都带条款出处；地区不匹配就返回 `missing`，
  不给默认值。护栏从"事后抓错"降为"兜底"。
- **规则判定不交给模型**。6 类核对项、6 个阻断码（第二十七条）、L1–L4 分级（第二十六条）
  全部在 `rules.py` 里，可单测、可复现。模型只负责解释结论。
- **顺序约束有四层，但只有两层是安全边界**。提示词与 `next_allowed` 只是降低跳步概率；
  真正的硬约束是 `validation_token`（HMAC + 内容哈希绑定 + 时效 + 一次性）和
  `confirm_token`（只能由人点击产生）。校验 A 建 B 这类绕过，靠提示词永远拦不住。

```bash
python -m my_tool data seed
python -m my_tool check --type salary_adjust --emp E1001 --field fixed_salary \
  --old 18500 --new 15000 --effective 2026-11-01 \
  --materials approval_form,adjust_basis --as-of 2026-10-10
python scripts/agent_demo.py     # 六个场景，含三次绕过尝试
```

## 换成真实模型

默认全部离线（`hash` 占位向量 + `mock` 抽取模型），免 Key 即可跑通链路。接真实模型只改环境变量：

```bash
export LLM_PROVIDER=deepseek
export LLM_API_KEY=sk-xxx
export EMBEDDING_PROVIDER=siliconflow     # bge-m3
export EMBEDDING_API_KEY=sk-xxx
python -m my_tool build                   # 换向量模型必须重建索引
```

支持任何 OpenAI 兼容端点：OpenAI / DeepSeek / 通义 / Kimi / 智谱 / Ollama / vLLM。

> ⚠️ 换向量模型后**必须重新标定拒答阈值**（`MIN_VECTOR_SCORE` / `MIN_BM25_SCORE`）。当前阈值是按 hash 向量标定的，直接沿用会导致"该答的不答"。

## 目录结构

```
.
├── src/my_tool/            # 源码（src 布局，包名 my_tool 为模板占位名）
│   ├── main.py             # CLI 入口：只做参数解析与装配
│   ├── __main__.py         # python -m my_tool
│   ├── evaluation.py       # 检索评测（hit@3 / clause@3）
│   ├── api/                # HTTP 层
│   │   ├── app.py          # 装配：索引 / 建库 / 异常分流（API 返 JSON、页面返 HTML）
│   │   ├── auth.py         # ★认证授权：JWT、角色依赖、CSRF、登录限流
│   │   ├── routes_chat.py  # 员工问答（SSE）
│   │   ├── routes_auth.py  # 登录 / 登出
│   │   ├── routes_admin.py # 后台概览
│   │   ├── routes_kb.py    # 知识库管理
│   │   ├── routes_prompts.py # 提示词模板
│   │   ├── routes_audit.py # 对话审计
│   │   ├── templating.py   # Jinja2 渲染封装
│   │   ├── sse.py / schemas.py / errors.py
│   ├── templates/          # 服务端渲染模板（base / login / dashboard / kb_* / prompts / audit_*）
│   ├── store/              # 持久化：唯一写 SQL 的地方
│   │   ├── db.py           # 建表（会话 2 张 + 后台 4 张）与事务
│   │   ├── chat_repo.py / user_repo.py / policy_repo.py / prompt_repo.py / audit_repo.py / stats_repo.py
│   ├── core/               # 核心业务逻辑
│   │   ├── config.py       # 配置分层
│   │   ├── entities.py     # Chunk / RetrievalHit / Citation
│   │   ├── parser.py       # Markdown → Block
│   │   ├── chunker.py      # Block → 500 字切片（★最敏感）
│   │   ├── pipeline.py     # 建索引 / 加载 / 漂移体检
│   │   ├── ingest.py       # 上传 → 落盘 → 解析 → 切片
│   │   ├── pdf.py          # PDF → 伪 Markdown（只吃文本层）
│   │   ├── registry.py     # 制度文件登记（补 build 与 policy_documents 的裂缝）
│   │   ├── index_manager.py # ★多文档重建 + 向量缓存 + 热切换
│   │   ├── security.py     # ★密码哈希 / JWT / 密钥落地
│   │   ├── prompt.py       # 模板组装 + 强制引用规则
│   │   ├── prompt_provider.py # ★模板缓存与失效
│   │   ├── audit_rules.py  # 审计预筛规则（R1~R4）
│   │   ├── guard.py        # 答案护栏（★幻觉率 0 的抓手）
│   │   ├── orchestrator.py # 一次问答的总调度（answer / answer_stream）
│   │   ├── eval_dataset.py # 评测用例
│   │   ├── retrieval/      # embedding / vector_store / bm25 / hybrid
│   │   └── llm/            # base / http / mock / openai_compat
│   └── utils/              # console / text / env
├── web/                    # 前端资源（零依赖、零构建，统一挂 /static）
│   ├── index.html / app.js / style.css   # 员工问答
│   └── admin.css                          # 管理后台样式
├── config/default.yaml     # 非机密默认参数（可入库）
├── data/
│   ├── policy/             # 制度原文（真相源 L0）+ uploads/（后台上传）
│   ├── index/              # 索引产物 + 向量缓存（派生物，git 忽略）
│   ├── chat.db             # 会话 / 用户 / 制度清单 / 提示词 / 审计（运行时生成，git 忽略）
│   └── .auth_secret        # JWT 密钥（运行时生成，git 忽略）
├── docs/                   # design.md / api.md / admin.md / admin-design.md / chat-api-design.md / product/ …
├── tests/                  # 162 项测试
├── examples/demo.py        # 离线可跑的最小示例
├── scripts/                # build.sh / deploy.sh / run_server.{bat,ps1,sh} / eval_retrieval.py / docgen/
└── .github/                # CI、Issue 模板、PR 模板
```

## 配置

优先级：**环境变量 > `config/default.yaml` > 代码默认值**。

- 团队共享的基线（切片 500 字、top3、检索权重）写在 `config/default.yaml`；
- 密钥与机器相关的（API Key、内网地址）走 `.env`，参考 `.env.example`。

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest -q                              # 162 passed
python -m pyflakes src scripts tests examples    # 应无输出
python scripts/eval_retrieval.py                 # clause@3 ≥ 90%，否则退出码 1
```

**改动 `core/chunker.py` 或 `core/retrieval/` 必须重跑评测**，因为这类改动的破坏方式很隐蔽：程序不报错，只是答案开始悄悄变差。详见 [`docs/design.md`](docs/design.md)。

回归测试里有一块是**鉴权专项**（`tests/test_auth.py`）：密码哈希、JWT 拒 `alg=none`、开放重定向、越权读会话一律 404 而不是 403（403 等于确认"这个 id 存在"）。
另有一块是**后台行为**（`tests/test_kb.py` / `test_prompts.py` / `test_audit.py`）：上传后能被召回、删除后不再召回、重建期间服务不中断、改提示词下一个问题即生效、标记可撤销且留痕。

## 已知限制

1. 默认 `hash` 向量只有词面相似度、**没有语义能力**，仅用于验证链路；
2. 拒答闸门挡得住明显越界，挡不住"制度相关但库里没有"的问法（阶段 4 由 Router 分流解决）；
3. 一份制度只保留一个现行版本：重新上传同一 `doc_id` 会被拒绝，请先删除或换编号（多版本并存需要版本表）；
4. 评测集 8 题由开发者撰写，正式验收需要 HR 出题扩到 20 题以上；
5. 流式**没有心跳帧**：生成器阻塞在 `next()` 时没机会 `yield`，真心跳要引入工作线程 + 队列。当前靠 delta 流量保活，上游静默超 60 秒会被代理掐断（详见设计文档第 12 节）；
6. 护栏是**事后**判定：已经流出去的未溯源数字收不回来，只能标 `partial` 并在气泡底部提示；
7. 管理后台是**单实例语义**：索引重建锁在进程内、提示词缓存靠 TTL 失效。多实例部署需要把这两件事挪到外部（队列 / 共享存储）；
8. PDF **只支持文本层**，扫描件会明确报错而不是产出空文档；PDF 表格的行列关系会丢。

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 单文档 RAG：切片 → 混合检索 → 带引用回答 → 护栏 | ✅ |
| 2 | 服务化：FastAPI + SSE、会话落 SQLite、6 轮历史、单页界面 | ✅ |
| 3 | 管理后台：登录鉴权、知识库管理、提示词模板、对话审计 | ✅（用户管理页面、OCR、多实例未做） |
| 4 | 集成与上线前加固：制度版本台账、组织架构、语音输入、对外 API | ✅（历史切片对照、多租户未做） |
| 5 | Agent 工具链：工具、规则引擎、闸门、Agent 循环、SSE 接入 | ✅（决策层为脚本化假模型，待接真模型） |
| 6 | 变更单与制度条款自动比对（差异分级、人工核对队列） | 待做 |
| 7 | 图谱抽取与多跳推理 | 待做 |

## 贡献与许可

贡献流程见 [`CONTRIBUTING.md`](CONTRIBUTING.md)，版本变更见 [`CHANGELOG.md`](CHANGELOG.md)。
本项目采用 [MIT 许可证](LICENSE)。
