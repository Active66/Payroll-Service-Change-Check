"""全局配置。优先级：环境变量 > config/default.yaml > 代码内置默认值。

三个"供应商可配置"的开关：LLM_PROVIDER / EMBEDDING_PROVIDER / *_BASE_URL。
同一 OpenAI 兼容协议下换厂商，只改 base_url + model 两行。
"""

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


# =============================================================================
# 【模块 01 · 配置】目标文件：src/my_tool/core/config.py
# ★核心模块（阶段 2 复用）：改成 pydantic-settings 的 BaseSettings 即可，
#   字段名与分层（llm / retrieval / ingestion）保持不变，调用方无需改动。
# =============================================================================
#
# 设计要点：所有可变量走环境变量或配置文件，不在代码里写死。
# 三个"供应商可配置"的开关分别是：
#   LLM_PROVIDER          —— 生成模型（mock / openai / deepseek / qwen / ollama ...）
#   EMBEDDING_PROVIDER    —— 向量模型（hash / openai / ...）
#   LLM_BASE_URL / EMBEDDING_BASE_URL —— 同一协议下换厂商只改这两行
# --------------------------------------------------------------------------- #

# 项目根目录：本文件位于 src/my_tool/core/config.py，向上第四级即仓库根
#   parents[0]=core  [1]=my_tool  [2]=src  [3]=repo root
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DOC = PROJECT_ROOT / "data" / "policy" / "员工薪酬变更核对制度_v1.0.md"
DEFAULT_INDEX_DIR = PROJECT_ROOT / "data" / "index"

# 非机密默认值所在文件。可提交进仓库；密钥一律走环境变量（.env 不进版本库）。
CONFIG_FILE = PROJECT_ROOT / "config" / "default.yaml"

# YAML 只认这四个分区 —— 显式白名单，避免 YAML 里写错键名却静默生效，
# 让人排查半天"我明明改了配置怎么没反应"。
_YAML_SECTIONS = ("ingestion", "retrieval", "llm", "embedding", "chat")


def _load_dotenv(path: Path | None = None) -> None:
    """
    把仓库根的 `.env` 读进 os.environ。**只补不覆盖**。

    为什么是"只补"：优先级铁律是"环境变量 > yaml > 默认值"。真正从 shell
    `export` 出来的变量应该压过 `.env` 文件里的值，否则一条本机临时配置
    会被一份可能过期的 .env 悄悄覆盖。

    为什么要自己写而不引 python-dotenv：只需要"注释 / KEY=VALUE / 引号"三件事，
    10 行内能写完，引库要多处理 ${VAR} 展开、multiline 这些永远用不到的特性。
    """
    env_file = path or (PROJECT_ROOT / ".env")
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _load_yaml_defaults() -> dict[str, Any]:
    """
    读取 config/default.yaml，拍平成 {Settings 字段名: 值}。

    · 文件不存在 → 返回空字典（只靠内置默认值也能跑）；
    · 未安装 PyYAML → 打一行警告后跳过（不让可选依赖把主流程搞挂）；
    · 键名不在 Settings 字段里 → 忽略，防止拼写错误被当成有效配置。
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        import yaml
    except ImportError:  # pragma: no cover
        print("[warn] 未安装 PyYAML，已忽略 config/default.yaml，改用内置默认值。")
        return {}

    raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    allowed = set(Settings.__dataclass_fields__)  # type: ignore[name-defined]
    out: dict[str, Any] = {}
    for section, items in raw.items():
        if section not in _YAML_SECTIONS or not isinstance(items, dict):
            continue
        for key, value in items.items():
            if key in allowed and value is not None:
                out[key] = value
    return out


def _as_path(value: Any, fallback: Path) -> Path:
    """路径归一化：YAML 里写相对路径时，一律相对仓库根解析，不依赖当前工作目录。"""
    if not value:
        return fallback
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

# 各厂商的默认端点与默认模型。值为 (base_url, model)。
# 除 mock 外全部走 OpenAI 兼容协议，所以"新增一个供应商"= 增加一行字典。
PROVIDER_DEFAULTS: dict[str, tuple[str, str]] = {
    "openai":   ("https://api.openai.com/v1", "gpt-4o-mini"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "qwen":     ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "moonshot": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "zhipu":    ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "ollama":   ("http://localhost:11434/v1", "qwen2.5:7b"),
    "vllm":     ("http://localhost:8000/v1", "Qwen2.5-7B-Instruct"),
}

EMBEDDING_PROVIDER_DEFAULTS: dict[str, tuple[str, str, int]] = {
    # (base_url, model, dim)
    "openai": ("https://api.openai.com/v1", "text-embedding-3-small", 1536),
    "qwen":   ("https://dashscope.aliyuncs.com/compatible-mode/v1", "text-embedding-v3", 1024),
    "siliconflow": ("https://api.siliconflow.cn/v1", "BAAI/bge-m3", 1024),
    "ollama": ("http://localhost:11434/v1", "bge-m3", 1024),
}


@dataclass
class Settings:
    """全局配置。所有字段都能被同名（大写）环境变量覆盖。"""

    # —— 数据与索引路径 ——
    doc_path: Path = DEFAULT_DOC
    index_dir: Path = DEFAULT_INDEX_DIR

    # —— 切片参数（架构文档要求 500 字，overlap 用于句间衔接，避免答案被切断）——
    chunk_size: int = 500
    chunk_overlap: int = 80
    min_chunk_size: int = 60      # 小于该长度的碎片并入上一块，避免产生无意义切片
    chunk_mode: str = "size"      # size=按 500 字装箱（默认）；clause=一条一款

    # —— 检索参数 ——
    top_k: int = 3                # 用户要求：top3
    recall_k: int = 12            # 每一路先召回 recall_k 条，再融合取 top_k
    rrf_k: int = 60               # RRF 平滑常数（原论文取 60，几乎不用调）
    w_vector: float = 1.0         # 向量通道权重
    w_bm25: float = 1.0           # 全文通道权重

    # —— 拒答闸门：两路最高分都低于各自阈值 → 判定"库里没有依据"，直接拒答 ——
    #   阈值与向量模型强相关（本机 hash 向量有效问题 ≈0.27~0.38，越界 ≈0.11；
    #   bge-m3 这类真实模型的相关分会更高），换模型必须重新标定。
    min_vector_score: float = 0.25
    min_bm25_score: float = 4.0

    # —— LLM ——
    llm_provider: str = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 900
    llm_timeout: float = 60.0
    llm_max_retries: int = 3

    # —— Embedding ——
    embedding_provider: str = "hash"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 512
    embedding_batch: int = 16

    # —— 检索过滤：模拟"制度生效区间"过滤，对应 policy_documents.effective_from/to ——
    as_of: str = ""               # 留空 = 用 2026-06-01，便于复现；真实系统取业务日期

    # —— 会话侧（阶段 2 员工问答入口）——
    chat_db_path: Path = PROJECT_ROOT / "data" / "chat.db"
    user_id: str = "demo-user"            # 本期固定占位；表里已留用户列，接鉴权时只换取值
    legal_entity_id: str = "demo-entity"  # 多法人隔离的兜底列，SQLite 阶段不做 RLS
    # 历史窗口：轮数是产品语义（用户要"最近 6 轮"），字符数是成本与窗口约束。
    # 两者同时生效，按最旧优先丢弃，避免"只按轮数"撑爆上下文。
    history_max_turns: int = 6
    history_max_chars: int = 2400
    history_msg_max_chars: int = 400
    # 追问合并：短问题 + 指代词时，把上一轮问题拼进检索 query（只影响检索，不影响生成）
    followup_merge_enabled: bool = True
    followup_max_chars: int = 12
    # 流式与护栏的取舍：after = 边流边判、事后告警（默认）；buffer = 缓冲到句末再发（预留）
    stream_guard_mode: str = "after"

    # —— 鉴权（阶段 3 管理后台）——
    # auth_required=False 时全部退回 demo-user：测试与离线演示不受影响，
    # 改造出问题也能一键回退。这是可回退性的开关，不是妥协。
    auth_required: bool = True
    auth_secret: str = ""                      # 留空 = 从 auth_secret_file 读取或生成
    auth_secret_file: Path = PROJECT_ROOT / "data" / ".auth_secret"
    jwt_ttl_hours: int = 8                     # 一个工作日；不做 refresh token
    cookie_secure: bool = False                # 生产走 HTTPS 时置 1
    # 登录限流：同账号连续失败 N 次后锁定 M 分钟（在内存里计数，重启清零）
    login_max_fails: int = 5
    login_lock_minutes: int = 5

    # —— 知识库上传 ——
    upload_dir: Path = PROJECT_ROOT / "data" / "policy" / "uploads"
    upload_max_mb: int = 10

    # —— 首次启动的种子管理员 ——
    admin_seed_username: str = "admin"
    admin_seed_password: str = ""              # 留空则随机生成并打印到启动日志

    # —— 提示词模板缓存 ——
    # 单 worker 下保存即失效（立即生效）；多 worker 下其他进程靠 TTL 兜底，
    # 所以对外只能承诺"最多 N 秒内生效"。
    prompt_cache_seconds: float = 5.0

    # —— Agent（阶段 5）——
    # agent_enabled 控制"能力是否可用"，agent_default_mode 控制"/chat 默认走哪条路"。
    # 分开是因为：功能要做完、要能演示，但默认行为不能因为升级而突变 ——
    # 现有前端、现有 179 项测试、现有演示都建立在固定流程上。
    # 想全局切到 Agent：AGENT_DEFAULT_MODE=agent，一行配置，不用改代码。
    agent_enabled: bool = True
    agent_default_mode: str = "rag"            # rag / agent
    # 循环预算。四条限制都在 loop.py 里强制执行，不依赖模型自觉。
    agent_max_tool_calls: int = 8              # 超过说明在打转，触顶强制转人工
    agent_max_turns: int = 5                   # 每轮一次完整生成；产品口径"最多 5 轮"
    agent_token_budget: int = 30000
    agent_timeout_seconds: float = 60.0
    # 校验凭证有效期。10 分钟够一次完整对话，又不让"昨天校验的"今天还能用。
    gate_token_ttl_seconds: int = 600
    gate_secret: str = ""                      # 留空则从 auth_secret 派生，不强制多配一个密钥

    @classmethod
    def from_env(cls) -> "Settings":
        """
        构造配置。取值优先级：环境变量 > config/default.yaml > 本类默认值。

        用"层叠"而不是"二选一"，是为了让 config/default.yaml 承担团队共享的基线
        （切片 500 字、top3 这类人人一致的参数），环境变量只覆盖本机/本环境的差异
        （密钥、临时换模型）。这样改基线不用动部署脚本，改环境不用动版本库。
        """
        _load_dotenv()                      # 先补上 .env（只补不覆盖），再取环境变量
        y = _load_yaml_defaults()          # YAML 层（可能为空）

        def e(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        def ei(name: str, default: int) -> int:
            try:
                return int(e(name) or default)
            except ValueError:
                return default

        def ef(name: str, default: float) -> float:
            try:
                return float(e(name) or default)
            except ValueError:
                return default

        def eb(name: str, default: bool) -> bool:
            """布尔开关解析。接受 1/0、true/false、yes/no、on/off，写错一律回落到默认值。"""
            raw = e(name).lower()
            if not raw:
                return default
            return raw in ("1", "true", "yes", "on", "y")

        provider = (e("LLM_PROVIDER") or str(y.get("llm_provider", "mock"))).lower() or "mock"
        base_url, model = PROVIDER_DEFAULTS.get(provider, ("", ""))
        # 优先级：环境变量 > YAML > 厂商默认端点
        base_url = e("LLM_BASE_URL") or str(y.get("llm_base_url") or "") or base_url
        model = e("LLM_MODEL") or str(y.get("llm_model") or "") or model
        # 没有 key 却选了真实供应商 → 自动退回 mock，避免原型一上来就报错
        api_key = e("LLM_API_KEY", e("OPENAI_API_KEY", "")) or str(y.get("llm_api_key") or "")
        if provider != "mock" and not api_key and "localhost" not in base_url and "127.0.0.1" not in base_url:
            print(f"[warn] LLM_PROVIDER={provider} 但未配置 LLM_API_KEY，本次降级为 mock 模型。")
            provider = "mock"

        emb_provider = (e("EMBEDDING_PROVIDER") or str(y.get("embedding_provider", "hash"))).lower() or "hash"
        emb_base, emb_model, emb_dim = EMBEDDING_PROVIDER_DEFAULTS.get(emb_provider, ("", "", 512))
        emb_base = e("EMBEDDING_BASE_URL") or str(y.get("embedding_base_url") or "") or emb_base
        emb_model = e("EMBEDDING_MODEL") or str(y.get("embedding_model") or "") or emb_model
        emb_dim = ei("EMBEDDING_DIM", int(y.get("embedding_dim", emb_dim if emb_provider != "hash" else 512)))
        emb_key = e("EMBEDDING_API_KEY", api_key)
        if emb_provider != "hash" and not emb_key:
            print(f"[warn] EMBEDDING_PROVIDER={emb_provider} 但未配置密钥，向量通道降级为 hash。")
            emb_provider = "hash"

        return cls(
            doc_path=_as_path(e("POLICY_DOC_PATH") or y.get("doc_path"), DEFAULT_DOC),
            index_dir=_as_path(e("INDEX_DIR") or y.get("index_dir"), DEFAULT_INDEX_DIR),
            chunk_size=ei("CHUNK_SIZE", int(y.get("chunk_size", 500))),
            chunk_overlap=ei("CHUNK_OVERLAP", int(y.get("chunk_overlap", 80))),
            min_chunk_size=ei("MIN_CHUNK_SIZE", int(y.get("min_chunk_size", 60))),
            chunk_mode=e("CHUNK_MODE") or str(y.get("chunk_mode", "size")) or "size",
            top_k=ei("TOP_K", int(y.get("top_k", 3))),
            recall_k=ei("RECALL_K", int(y.get("recall_k", 12))),
            rrf_k=ei("RRF_K", int(y.get("rrf_k", 60))),
            w_vector=ef("W_VECTOR", float(y.get("w_vector", 1.0))),
            w_bm25=ef("W_BM25", float(y.get("w_bm25", 1.0))),
            min_vector_score=ef("MIN_VECTOR_SCORE", float(y.get("min_vector_score", 0.25))),
            min_bm25_score=ef("MIN_BM25_SCORE", float(y.get("min_bm25_score", 4.0))),
            llm_provider=provider,
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            llm_temperature=ef("LLM_TEMPERATURE", float(y.get("llm_temperature", 0.2))),
            llm_max_tokens=ei("LLM_MAX_TOKENS", int(y.get("llm_max_tokens", 900))),
            llm_timeout=ef("LLM_TIMEOUT", float(y.get("llm_timeout", 60.0))),
            llm_max_retries=ei("LLM_MAX_RETRIES", int(y.get("llm_max_retries", 3))),
            embedding_provider=emb_provider,
            embedding_base_url=emb_base,
            embedding_api_key=emb_key,
            embedding_model=emb_model,
            embedding_dim=emb_dim,
            embedding_batch=ei("EMBEDDING_BATCH", int(y.get("embedding_batch", 16))),
            as_of=e("AS_OF_DATE") or str(y.get("as_of", "")),
            # —— 会话侧 ——
            chat_db_path=_as_path(e("CHAT_DB_PATH") or y.get("chat_db_path"),
                                 PROJECT_ROOT / "data" / "chat.db"),
            user_id=e("CHAT_USER_ID") or str(y.get("user_id", "demo-user")),
            legal_entity_id=e("LEGAL_ENTITY_ID") or str(y.get("legal_entity_id", "demo-entity")),
            history_max_turns=ei("HISTORY_MAX_TURNS", int(y.get("history_max_turns", 6))),
            history_max_chars=ei("HISTORY_MAX_CHARS", int(y.get("history_max_chars", 2400))),
            history_msg_max_chars=ei("HISTORY_MSG_MAX_CHARS", int(y.get("history_msg_max_chars", 400))),
            followup_merge_enabled=eb("FOLLOWUP_MERGE_ENABLED",
                                      bool(y.get("followup_merge_enabled", True))),
            followup_max_chars=ei("FOLLOWUP_MAX_CHARS", int(y.get("followup_max_chars", 12))),
            stream_guard_mode=e("STREAM_GUARD_MODE") or str(y.get("stream_guard_mode", "after")),
            # —— 鉴权与管理后台 ——
            auth_required=eb("AUTH_REQUIRED", bool(y.get("auth_required", True))),
            auth_secret=e("AUTH_SECRET") or str(y.get("auth_secret") or ""),
            auth_secret_file=_as_path(e("AUTH_SECRET_FILE") or y.get("auth_secret_file"),
                                      PROJECT_ROOT / "data" / ".auth_secret"),
            jwt_ttl_hours=ei("JWT_TTL_HOURS", int(y.get("jwt_ttl_hours", 8))),
            cookie_secure=eb("COOKIE_SECURE", bool(y.get("cookie_secure", False))),
            login_max_fails=ei("LOGIN_MAX_FAILS", int(y.get("login_max_fails", 5))),
            login_lock_minutes=ei("LOGIN_LOCK_MINUTES", int(y.get("login_lock_minutes", 5))),
            upload_dir=_as_path(e("UPLOAD_DIR") or y.get("upload_dir"),
                                PROJECT_ROOT / "data" / "policy" / "uploads"),
            upload_max_mb=ei("UPLOAD_MAX_MB", int(y.get("upload_max_mb", 10))),
            admin_seed_username=e("ADMIN_SEED_USERNAME")
                                 or str(y.get("admin_seed_username", "admin")),
            admin_seed_password=e("ADMIN_SEED_PASSWORD")
                                 or str(y.get("admin_seed_password", "")),
            prompt_cache_seconds=ef("PROMPT_CACHE_SECONDS",
                                    float(y.get("prompt_cache_seconds", 5.0))),
            # —— Agent ——
            agent_enabled=eb("AGENT_ENABLED", bool(y.get("agent_enabled", True))),
            agent_default_mode=(e("AGENT_DEFAULT_MODE")
                                or str(y.get("agent_default_mode", "rag"))),
            agent_max_tool_calls=ei("AGENT_MAX_TOOL_CALLS",
                                    int(y.get("agent_max_tool_calls", 8))),
            agent_max_turns=ei("AGENT_MAX_TURNS", int(y.get("agent_max_turns", 5))),
            agent_token_budget=ei("AGENT_TOKEN_BUDGET",
                                  int(y.get("agent_token_budget", 30000))),
            agent_timeout_seconds=ef("AGENT_TIMEOUT_SECONDS",
                                     float(y.get("agent_timeout_seconds", 60.0))),
            gate_token_ttl_seconds=ei("GATE_TOKEN_TTL_SECONDS",
                                      int(y.get("gate_token_ttl_seconds", 600))),
            gate_secret=e("GATE_SECRET") or str(y.get("gate_secret") or ""),
        )

    def as_of_date(self) -> date:
        """检索基准日期：真实系统取"业务日期"，原型里固定一个可复现的日期。"""
        if self.as_of:
            try:
                return date.fromisoformat(self.as_of)
            except ValueError:
                pass
        return date(2026, 6, 1)
