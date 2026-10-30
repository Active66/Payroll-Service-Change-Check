# 贡献指南

欢迎提 Issue 与 PR。这个项目的失败方式很隐蔽 —— **程序不报错，只是答案开始悄悄变差** —— 所以下面的约定大多是为了让"变差"能被发现。

## 环境

```bash
git clone <repo-url> && cd payroll-change-check
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m my_tool build            # 需要 PYTHONPATH=src，或先 pip install -e .
```

## 提交前必须全绿

```bash
python -m pytest -q                              # 39 passed
python -m pyflakes src scripts tests examples    # 无输出
python scripts/eval_retrieval.py                 # clause@3 ≥ 90%，否则退出码 1
```

`scripts/deploy.sh` 把这四步串好了，直接跑它也行。

## 代码约定

1. **注释写"为什么"，不写"做了什么"**。`# 这里加 1` 没有价值；「这里必须给正文预留 120 字，否则条标题会落在上一块末尾」才有。
2. **类型标注尽量写全**，`from __future__ import annotations` 不是必需的（Python ≥ 3.10）。
3. **出网只允许在 `core/llm/http.py`**。业务层不要直接 `import requests` / `urllib` —— 限流、超时、重试、成本记账都靠这一个出口。
4. **新增供应商 = 加一行字典**。改 `core/config.py` 的 `PROVIDER_DEFAULTS`，不要在业务代码里写 `if provider == ...`。
5. **新增配置项要同时更新三处**：`Settings` 字段、`config/default.yaml`、`.env.example`。
6. **不要在 `core/` 里 import `main`**。依赖方向严格单向，`main` 只是装配层。

## 改动敏感区的要求

| 改动位置 | 额外要求 |
|---|---|
| `core/chunker.py` | 必须重跑 `scripts/eval_retrieval.py`，并在 PR 里贴出改动前后的分数 |
| `core/retrieval/` | 同上；如果是融合策略变更，附上 2~3 条典型 query 的双通道名次对比 |
| `core/prompt.py` | 修改 `PROMPT_VERSION` 字符串（它会被写进 `chat_messages`，用于线上线下归因） |
| `core/guard.py` | 新增检查项必须配单测，护栏的漏报比误报严重得多 |
| `core/entities.py` | 字段变更需同步阶段 2 的建表脚本，并在 PR 描述里说明迁移方式 |

## 增删评测题

评测集在 `src/my_tool/core/eval_dataset.py`。加题原则：

- 题目要来自真实业务提问（HR 同事出的题最有价值），不要自己编"标准考试题"；
- 必须给出 `expect_clause`（期望条款号）与 `expect_kw`（原文里的关键证据字符串）；
- 加题后确认 `clause@3` 仍然 ≥ 90%，否则要先判断是题太难还是检索真的退化。

## 提交信息

参考 Conventional Commits：

```
feat(retrieval): 支持图谱通道作为第三路召回
fix(guard): 修复千分位数字未归一化导致误报
docs(design): 补充拒答阈值的标定方法
refactor(chunker): 抽出标题内联逻辑
test(retrieval): 增加跨章合并的回归用例
```

## PR 流程

1. 从 `main` 切分支：`feat/xxx`、`fix/xxx`、`docs/xxx`；
2. 按 `.github/PULL_REQUEST_TEMPLATE.md` 填写，**「对答案质量的影响」一栏不要留空**；
3. CI 全绿；
4. 至少一位 reviewer 通过。涉及切片/检索/护栏的改动，reviewer 应独立重跑一次评测。

## 不要提交

- 任何密钥（`.env`、`*.key`、`*.pem`）—— 已在 `.gitignore` 中，但提交前仍请自查；
- 真实员工薪酬数据。测试与示例一律使用 `data/policy/` 下的合成制度文本；
- `data/index/` 下的索引产物（可由制度原文完整重建）。
