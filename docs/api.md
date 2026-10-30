# 接口说明

> 覆盖 Python API、CLI、数据结构与状态码。阶段 2 的 HTTP 契约见文末预告。

## 1. 快速上手（Python）

```python
from my_tool.core.config import Settings
from my_tool.core.pipeline import build_bm25, load_index
from my_tool.core.retrieval.embedding import build_embedding_model
from my_tool.core.llm import build_chat_model
from my_tool.core.orchestrator import Orchestrator

s = Settings.from_env()                       # 读 config/default.yaml + 环境变量
store, manifest = load_index(s)                # 或 build_index(s) 首次构建

orch = Orchestrator(
    store, build_bm25(store),
    build_embedding_model(s), build_chat_model(s), s,
)

res = orch.answer("冻结期内可以改银行账户吗？")
print(res.answer)
for c in res.citations:
    print(c.index, c.chunk_id, c.clause_no)
```

## 2. 核心类与方法

### `Settings`（`core/config.py`）

配置优先级：**环境变量 > `config/default.yaml` > 类默认值**。

| 方法 | 说明 |
|---|---|
| `Settings.from_env()` | 构造配置；缺密钥时自动把 LLM/Embedding 降级到 mock/hash 并打警告 |
| `s.as_of_date()` | 检索基准日期；`as_of` 为空时返回 `2026-06-01` |

主要字段分组：`doc_path / index_dir`（数据）、`chunk_*`（切片）、`top_k / recall_k / rrf_k / w_*`（检索）、`min_*_score`（闸门）、`llm_*`、`embedding_*`。

### `Orchestrator.answer(question, *, top_k=None, history=None) -> RAGAnswer`

无召回时不进入生成环节，直接返回 `refused`，`tokens_in/out = 0`。
`history` 为阶段 2 预留：历史只插到 system 之后，**不参与检索**（避免多轮污染 query）。

### `build_index(s) -> (VectorStore, manifest)`

全量重建：解析 → 切片 → 向量化 → 写 `index.faiss` / `chunks.jsonl` / `manifest.json`。
返回的 manifest 与落盘文件一致（含 `backend` / `dim` / `chunk_count`）。

### `load_index(s) -> (VectorStore, manifest)`

加载索引并做**配置漂移体检**：索引的向量模型 ≠ 当前配置时抛 `RuntimeError`，因为两者不在同一向量空间，相似度无意义。

### `run_guard(answer, context, hit_count) -> GuardResult`

| 字段 | 含义 |
|---|---|
| `passed` | `status == "answered"` 且无告警 |
| `status` | `answered` / `partial` / `refused` |
| `cited_indexes` | 答案里出现的角标编号 |
| `invalid_citations` | 越界角标（如只有 3 条资料却引用 `[5]`） |
| `unverified_numbers` | 原文中找不到的带单位数字（疑似幻觉） |
| `warnings` | 人读的告警文案 |

## 3. 数据结构

### `Chunk`

| 字段 | 说明 |
|---|---|
| `chunk_id` | `POL-CMP-2026-001#c003`，全局可追溯 |
| `doc_id` / `doc_title` / `doc_type` | 文档身份 |
| `chunk_no` | 文档内序号，从 1 起 |
| `content` | 切片正文（含标题内联与重叠部分） |
| `heading_path` | `文档 > 章`，如 `员工薪酬变更核对制度 > 第五章 社会保险与住房公积金基数核对` |
| `clause_no` / `clause_range` / `clauses[]` | 条款定位；多条款时为 `第十七条–第十九条` |
| `char_len` / `token_count` | 长度与近似 token 数（中文按 1 字 ≈ 0.75 token 粗估） |
| `page_no` | Markdown 为 `None`；PDF/DOCX 摄入时填真实页码 |
| `region_code` / `version` / `effective_from` / `effective_to` | 生效区间与地区，用于检索期过滤 |

### `RAGAnswer`

| 字段 | 说明 |
|---|---|
| `question` / `answer` | 输入与输出 |
| `status` | `answered` / `partial` / `refused` / `error` |
| `citations[]` | 引用列表（见下） |
| `hits[]` | top-k 召回明细，含双通道分数与名次 |
| `guard` | `GuardResult`；未进模型时为 `None` |
| `debug` | 检索耗时、通道候选数、闸门结论、`t` `provider` 等 |
| `prompt_version` / `model` / `tokens_in` / `tokens_out` / `latency_ms` | 可观测性字段 |

### `Citation`

`index`（正文角标）、`chunk_id`、`doc_id`、`clause_no`、`heading_path`、`score`、`snippet`。

映射关系由系统持有：模型只写角标 `[1]`，角标指向哪条 `chunk_id` 由检索结果决定，模型无法伪造出处。

## 4. CLI

```
payroll-check <command>            # 安装后（pip install -e .）
PYTHONPATH=src python -m my_tool <command>   # 未安装时
```

| 命令 | 说明 |
|---|---|
| `build` | 解析制度 → 切片 → 向量化 → 写 FAISS |
| `ask "问题" [--debug] [--top-k N]` | 单次提问；`--debug` 打印双通道名次与耗时 |
| `chat [--debug]` | 交互模式；`/sources` 看完整原文，`/debug` 切调试，`/quit` 退出 |
| `eval` | 跑内置 8 题检索评测 |
| `info` | 打印当前配置与索引 manifest |

退出码：`0` 正常，`1` 索引缺失或加载失败。

### 评测脚本

```bash
python scripts/eval_retrieval.py [--rebuild] [--threshold 0.9]
```

`clause@3 < threshold` 时退出码为 `1`，供 CI 卡合并。

## 5. 环境变量

完整清单见 `.env.example`，常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock` / `openai` / `deepseek` / `qwen` / `moonshot` / `zhipu` / `ollama` / `vllm` |
| `LLM_API_KEY` | 空 | 未配置时自动降级为 mock |
| `LLM_BASE_URL` / `LLM_MODEL` | 厂商默认 | 换自建网关只改这两项 |
| `EMBEDDING_PROVIDER` | `hash` | `hash` 仅供跑通链路，生产必须替换 |
| `TOP_K` / `RECALL_K` | `3` / `12` | 送入 prompt 的条数 / 单路召回数 |
| `MIN_VECTOR_SCORE` / `MIN_BM25_SCORE` | `0.25` / `4.0` | 拒答闸门阈值（**换向量模型必须重标**） |

## 6. HTTP 契约（阶段 2 已实现）

完整契约（错误码表、SSE 事件、SQLite DDL、并发语义）见 [`chat-api-design.md`](chat-api-design.md)。
这里只放速查：

| 方法 | 路径 | 返回 |
|---|---|---|
| POST | `/chat` | `text/event-stream`：`session → status → citations → status → delta* → [guard] → done` |
| POST | `/sessions` | `{ok, data:{session_id, created_at}}` |
| GET | `/sessions/{id}/messages?limit=50` | 按 `seq` 正序的消息数组（含 citations） |
| GET | `/healthz` | 索引 / 模型 / 数据库状态（索引坏也返回 200） |
| POST | `/messages/{id}/feedback` | `{ok, data:{message_id, feedback}}` |

```bash
curl -N -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"冻结期内能改银行账户吗？"}'
```

错误统一 `{"ok":false,"error":{"code","message","trace_id","detail"}}`，`message` 为中文；流一旦开始，
错误只能以 `event: error` 出现（状态码固定 200），字段与 JSON 错误体一致。
