# 文档索引

| 文件 | 内容 | 读者 |
|---|---|---|
| [`design.md`](design.md) | 设计说明：分层、模块职责、四个关键决策、已知限制、阶段规划 | 要改代码的人 |
| [`api.md`](api.md) | 接口说明：Python API、CLI、数据结构、环境变量、状态码 | 接这份代码的人 |
| [`chat-api-design.md`](chat-api-design.md) | 员工问答入口的 HTTP 契约：SSE 事件、错误码表、SQLite DDL、历史窗口、实现差异 | 前后端联调、运维排障 |
| [`admin.md`](admin.md) | 管理后台**使用说明**：启动与登录、七个页面、四个常见任务、运维要点、已知限制 | 管理员、实施、运维 |
| [`admin-design.md`](admin-design.md) | 管理后台**设计**：页面与接口清单、权限矩阵、数据模型、五个关键决策、分批交付 | 要改后台的人 |
| [`integration.md`](integration.md) | 集成与加固**使用说明**：制度换版与回滚、部门统计口径、语音输入、`/api/v1` 对接、升级注意 | 管理员、集成开发、运维 |
| [`integration-design.md`](integration-design.md) | 集成与加固**设计**：为什么用版本台账、为什么 API Key 不复用 JWT、为什么语音不上服务端、明确不做什么 | 要改这块的人 |
| [`agent.md`](agent.md) | Agent **使用说明**：参数表与条款对照、规则引擎判什么、闸门四层机制、9 个工具、运维要点 | 实施、运维、集成开发 |
| [`agent-design.md`](agent-design.md) | Agent **设计**：工具 vs skill 的划分依据、顺序约束四层、系统提示词策略、明确不做什么 | 要改 Agent 的人 |
| [`agent-traces.md`](agent-traces.md) | Agent **轨迹**：两张日志表的分工、3 个典型流程的预期轨迹、决策日志怎么查 | 阶段 5 分析、运维排障 |
| `eval/agent_cases.json` + `scripts/eval_agent.py` | Agent **评测**：30 条用例（四类）+ 自动评测 runner，比对实际工具序列 vs 期望，输出 pass/fail 报告 | QA、回归 |

## 产品与方案（`product/`）

| 文件 | 内容 |
|---|---|
| [`product/薪酬服务变更核对系统-项目方案.md`](product/薪酬服务变更核对系统-项目方案.md) | 项目背景、业务范围、价值测算 |
| [`product/薪酬变更核对系统_需求清单_v0.1.html`](product/薪酬变更核对系统_需求清单_v0.1.html) | 需求清单与优先级 |
| [`product/薪酬变更核对系统_技术架构设计_v0.1.html`](product/薪酬变更核对系统_技术架构设计_v0.1.html) | 技术架构（本仓库实现的依据） |
| [`product/薪酬变更单现状流程图.html`](product/薪酬变更单现状流程图.html) | 业务现状流程图 |
| [`product/报价单设计方案.md`](product/报价单设计方案.md) | 商务侧方案 |

## 素材（`assets/`）

流程图图片与 `ai_flow.*` 由 `scripts/docgen/gen_ai_flow.py` 生成，改图请改脚本后重跑，不要直接改产物。

## 历史参考（`reference/`）

`reference/stage1_rag_prototype.py` 是阶段 1 的**单文件原始版本**，仅供追溯与对照。
它的代码已按模块拆分进 `src/my_tool/`，行为一致（拆分后重建索引得到同样的 20 个切片、评测同为 8/8）。
新代码请改 `src/`，不要再改这份留档。
