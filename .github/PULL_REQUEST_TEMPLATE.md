## 这个 PR 做了什么

<!-- 一句话说清目的。如果是修 bug，附 issue 编号。 -->

## 改动范围

- [ ] `core/chunker.py` 切片策略（★改这里必须重跑评测）
- [ ] `core/retrieval/` 检索与融合
- [ ] `core/guard.py` 答案护栏
- [ ] `core/llm/` 模型适配
- [ ] 配置 / 脚本 / 文档 / 测试

## 自查清单

- [ ] `python -m pytest -q` 全绿
- [ ] `python -m pyflakes src scripts tests examples` 无输出
- [ ] 改了切片或检索 → 已重跑 `python scripts/eval_retrieval.py`，clause@3 ≥ 90%
- [ ] 新增可配置项 → 已同时更新 `config/default.yaml` 与 `.env.example`
- [ ] 新增出网调用 → 已收敛到 `core/llm/http.py`，没有在业务层直接引 urllib/requests
- [ ] 没有提交任何密钥、真实员工数据或 `.env`

## 对答案质量的影响

<!--
只要动了检索/切片/prompt/护栏，就必须回答这句：
改动前后的评测分数分别是多少？为什么认为这次改动是正向的？
-->

## 备注
