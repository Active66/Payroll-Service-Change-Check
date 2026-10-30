"""SQLite 连接、建表与事务。字段沿用架构文档 3.3 / 3.4，只降级类型不动语义。
"""

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

# =============================================================================
# 【模块 15 · 会话存储】目标文件：src/my_tool/store/db.py
# ★核心模块（阶段 3 迁 PG 时复用）：DDL 之外的代码一行都不用改。
#
#   三条设计约束（都与"SQLite 是单写者"这个事实有关）：
#     ① WAL + busy_timeout：读写不互相阻塞，SSE 长连接场景必需；
#     ② 用 BEGIN IMMEDIATE 而不是默认的 deferred：写锁在事务开始就拿，
#        避免"读到能写、写时才发现冲突"的经典 SQLITE_BUSY；
#     ③ 每步操作一个短事务。**绝不要把 LLM 调用包进事务**——
#        一次生成 5 秒就会锁库 5 秒，其他人全部排队超时。
# =============================================================================

SCHEMA = """
-- 会话表：对应架构文档 3.3 chat_sessions
CREATE TABLE IF NOT EXISTS chat_sessions (
    id                   TEXT PRIMARY KEY,
    user_id              TEXT,
    legal_entity_id      TEXT NOT NULL DEFAULT 'demo-entity',
    scene                TEXT NOT NULL DEFAULT 'employee',
    title                TEXT,
    summary              TEXT,
    summarized_until_seq INTEGER NOT NULL DEFAULT 0,
    model                TEXT,
    token_used           INTEGER NOT NULL DEFAULT 0,
    message_count        INTEGER NOT NULL DEFAULT 0,
    last_message_at      TEXT,
    deleted_at           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- 消息表：对应架构文档 3.4 chat_messages
CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('user','assistant','tool','system')),
    content       TEXT NOT NULL,
    citations     TEXT NOT NULL DEFAULT '[]',
    tool_calls    TEXT,
    tool_result   TEXT,
    answer_status TEXT NOT NULL DEFAULT 'answered'
                  CHECK (answer_status IN ('streaming','answered','partial','refused','error')),
    token_in      INTEGER NOT NULL DEFAULT 0,
    token_out     INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    model         TEXT,
    trace_id      TEXT,
    feedback      INTEGER,
    prompt_version TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_msg_session_seq ON chat_messages (session_id, seq);
CREATE INDEX IF NOT EXISTS idx_msg_status       ON chat_messages (answer_status);
CREATE INDEX IF NOT EXISTS idx_sess_last_msg    ON chat_sessions (last_message_at DESC);
"""

# =============================================================================
# 【模块 15b · 管理后台建表】目标文件：src/my_tool/store/db.py
#   后台的 4 张表单独放一个脚本，与上面两张会话表分开：
#     ① 会话表是"业务运行必需"，后台表是"运营管理必需"，分开后
#        只跑员工问答的部署可以完全不建这 4 张表；
#     ② 两段 DDL 各自独立演进，改后台不会碰会话表。
#
#   为什么 message_audits 是独立表而不是给 chat_messages 加一列：
#     "标记疑似违规"是「人 + 时间 + 结论」的事件，可能被同一个人改主意、
#     也可能两个人给出不同判断；加列只能存最后一次，把过程丢了。
# =============================================================================

ADMIN_SCHEMA = """
-- 用户。密码存 "pbkdf2_sha256$轮数$salt_b64$hash_b64"，绝不存明文或裸 sha256
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    employee_no   TEXT,
    department    TEXT,
    department_id TEXT,
    role          TEXT NOT NULL CHECK (role IN ('employee','admin')),
    password_hash TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    last_login_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 制度文档清单。source_file 存原始文件名（引用卡片要展示给人看），
-- storage_path 存物理落盘路径；checksum 决定重建时是否需要重新向量化
--
-- 唯一键是 (doc_id, version) 而不是 doc_id：制度每年都改，换版不能要求
-- "先删旧版再传新版"——那两步之间索引里没有这份制度，问答会拒答。
-- status 三态：active（在用）/ superseded（被新版替代，留档）/ deleted（不要了）
CREATE TABLE IF NOT EXISTS policy_documents (
    id             TEXT,
    doc_id         TEXT NOT NULL,
    title          TEXT NOT NULL,
    doc_type       TEXT NOT NULL DEFAULT 'policy',
    version        INTEGER NOT NULL DEFAULT 1,
    version_label  TEXT,
    region_code    TEXT NOT NULL DEFAULT '',
    effective_from TEXT,
    effective_to   TEXT,
    source_file    TEXT NOT NULL,
    storage_path   TEXT NOT NULL,
    checksum       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','superseded','deleted')),
    chunk_count    INTEGER NOT NULL DEFAULT 0,
    char_count     INTEGER NOT NULL DEFAULT 0,
    uploaded_by    TEXT,
    published_by   TEXT,
    superseded_at  TEXT,
    indexed_at     TEXT,
    deleted_at     TEXT,
    deleted_by     TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (doc_id, version)
);

-- 部门：只做两级（parent_id 为 NULL 即一级）。SSC 要看"按区域/部门"的问答分布，
-- 更深的组织树属于 HR 主数据，应该对接而不是自己造
CREATE TABLE IF NOT EXISTS departments (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    parent_id  TEXT,
    code       TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (name, parent_id)
);

-- 外部系统 API Key。JWT 是"人"的凭证，Key 是"系统"的凭证，两者不混用：
-- 否则员工能拿自己的 token 去调对外接口，责任边界立刻糊掉。
-- key_hash 存 sha256，明文只在创建时打印一次
CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    owner        TEXT NOT NULL DEFAULT '',
    key_hash     TEXT NOT NULL UNIQUE,
    prefix       TEXT NOT NULL DEFAULT '',
    rate_limit   INTEGER NOT NULL DEFAULT 60,
    status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    last_used_at TEXT,
    created_at   TEXT NOT NULL,
    revoked_at   TEXT
);

-- ============================================================================
-- 阶段 5：Agent 化（工具 + 规则引擎 + 变更单）
--
-- 四张表各自的职责边界必须分清，否则会退化成"什么都存一张表":
--   policy_params   制度里所有"确定数值"的唯一来源。数字不由模型产出，就靠它
--   employees       人事档案（入职日/职级/现薪资/参保地）。与 users 分开:
--                   users 是登录账号，employees 是业务主体，一个人可能没有账号
--   change_orders   变更单。状态机: draft → submitted → approved / rejected / blocked
--   gate_tokens     校验凭证的消费记录。凭证本身无状态（HMAC），这张表只记"哪张用过"
-- ============================================================================

-- 制度参数表。每个数值都必须带 source_clause —— 没有出处的参数等于没有参数，
-- 因为无法回答"这个数凭什么"
CREATE TABLE IF NOT EXISTS policy_params (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    param_key      TEXT NOT NULL,
    value          TEXT NOT NULL,             -- 统一存文本，unit 说明怎么解读
    unit           TEXT NOT NULL DEFAULT '',
    region_code    TEXT NOT NULL DEFAULT '',  -- 地区参数（第三十五条）：不同参保地规则不同
    source_clause  TEXT NOT NULL,             -- 条款号，如"第十八条"
    source_doc_id  TEXT NOT NULL DEFAULT '',
    effective_from TEXT,
    effective_to   TEXT,
    note           TEXT,
    updated_by     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (param_key, region_code, effective_from)
);

CREATE INDEX IF NOT EXISTS idx_param_key ON policy_params (param_key, region_code);

-- 人事档案。刻意不塞进 users：账号可以禁用/删除，而人事档案要留 5 年（第三十七条）
CREATE TABLE IF NOT EXISTS employees (
    employee_no      TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    hire_date        TEXT,                    -- 入职日：判定"生效日早于入职日"阻断
    grade            TEXT,                    -- 职级：薪酬带宽判定
    fixed_salary     REAL,
    si_base          REAL,                    -- 当前社保基数
    hf_base          REAL,
    region_code      TEXT NOT NULL DEFAULT '',
    legal_entity_id  TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active','left')),
    user_id          TEXT,                    -- 关联登录账号，可为空（批量导入的员工还没开号）
    -- 以下三列是"能查但要脱敏"的字段。存原文（业务要用于核对），
    -- 出口统一在 employee_repo.mask() 里只留后 4 位 —— 脱敏在服务端做，不靠提示词
    id_card          TEXT,
    bank_account     TEXT,
    phone            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_emp_user ON employees (user_id);

-- 变更单。items 不独立成表：明细项永远随单读写，拆表只会让"核对时的事务"
-- 变复杂，而核对需要读到一致快照。需要按明细项查询时用 json_each 展开
CREATE TABLE IF NOT EXISTS change_orders (
    id               TEXT PRIMARY KEY,
    order_no         TEXT NOT NULL UNIQUE,
    employee_no      TEXT NOT NULL,
    change_type      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'draft'
                     CHECK (status IN ('draft','submitted','approved','rejected','blocked')),
    risk_level       TEXT,                    -- L1..L4，由规则引擎写入
    items            TEXT NOT NULL DEFAULT '[]',
    materials        TEXT NOT NULL DEFAULT '[]',
    blocking         TEXT NOT NULL DEFAULT '[]',   -- 阻断码与依据条款，第三十七条要求可举证
    validation_jti   TEXT,                    -- 这张单基于哪一次校验建的（审计主线）
    idempotency_key  TEXT UNIQUE,             -- Agent 会重试，没有它就会建出两张一样的单
    -- 第二十九条：关闭后不可编辑，修正须新建单并引用原单号，形成追溯链
    supersedes_order_no TEXT,
    created_by       TEXT,
    submitted_at     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_order_emp  ON change_orders (employee_no, change_type);
CREATE INDEX IF NOT EXISTS idx_order_stat ON change_orders (status, created_at DESC);

-- 校验凭证消费记录。凭证本身是 HMAC 无状态串，这里只解决"一次性"：
-- 同一 jti 只能被 create_draft 消费一次，否则一张凭证能建出无限张单
CREATE TABLE IF NOT EXISTS gate_tokens (
    jti           TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'validation'
                  CHECK (kind IN ('validation','confirm')),
    subject_id    TEXT NOT NULL DEFAULT '',   -- 绑定的对象（draft_id / order_hash）
    consumed_at   TEXT NOT NULL,
    consumed_by   TEXT
);

-- Agent 轨迹：每次工具调用一条，含入参、出参、耗时。
-- 为什么要全量记：Agent 最常被问的问题是"它为什么这么判断"，
-- 而这个问题的答案只存在于工具调用的序列里。没有轨迹等于不可解释。
CREATE TABLE IF NOT EXISTS agent_traces (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL DEFAULT '',
    turn_no    INTEGER NOT NULL DEFAULT 1,     -- 第几次用户提问内的第几轮循环
    seq        INTEGER NOT NULL DEFAULT 0,     -- 该轮内第几次工具调用
    tool_name  TEXT NOT NULL,
    args       TEXT NOT NULL DEFAULT '{}',
    result     TEXT NOT NULL DEFAULT '{}',
    ok         INTEGER NOT NULL DEFAULT 1,
    error_code TEXT,
    latency_ms INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trace_session ON agent_traces (session_id, turn_no, seq);

-- Agent 每轮"决策"日志。与 agent_traces 的分工：
--   agent_runs   记录**模型那一步**（每轮一次）：它说了什么、选了哪些工具、花了多少 token
--   agent_traces 记录**工具那一步**（每次一次）：参数、结果、耗时
-- 两者按 (session_id, turn_no) 对齐，拼起来就是完整可回放的决策轨迹，
-- 供阶段 5 的"它为什么这么判断"做分析。
CREATE TABLE IF NOT EXISTS agent_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL DEFAULT '',
    turn_no       INTEGER NOT NULL DEFAULT 1,
    question      TEXT NOT NULL DEFAULT '',    -- 用户原始问题（每轮都冗余一份，方便单表查询）
    model         TEXT,
    tokens_in     INTEGER NOT NULL DEFAULT 0,
    tokens_out    INTEGER NOT NULL DEFAULT 0,
    finish_reason TEXT,                        -- stop / tool_calls / 降级原因
    thought       TEXT,                        -- 模型这轮的正文（可能为空，只调工具不说话）
    chose_tools   TEXT NOT NULL DEFAULT '[]',  -- 本轮选择的工具名列表
    degraded      INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_session ON agent_runs (session_id, turn_no);

-- 提示词模板。版本不可变：每次保存插入新行，(key,version) 唯一，is_active 单活。
-- 回滚 = 把旧行重新置 active，绝不 UPDATE 历史内容 —— 否则"上周那版能答"不可复现
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

-- 审计标记。reason 存规则码（R1..R5）或 'manual'，便于统计"哪条规则最常命中"
CREATE TABLE IF NOT EXISTS message_audits (
    id         TEXT PRIMARY KEY,
    message_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    flag       TEXT NOT NULL CHECK (flag IN ('suspect','confirmed','cleared')),
    reason     TEXT,
    note       TEXT,
    auditor_id TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_role    ON users (role, status);
CREATE INDEX IF NOT EXISTS idx_doc_status    ON policy_documents (status);
CREATE INDEX IF NOT EXISTS idx_prompt_active ON prompt_templates (key, is_active);
CREATE INDEX IF NOT EXISTS idx_audit_msg     ON message_audits (message_id, revoked_at);
CREATE INDEX IF NOT EXISTS idx_msg_created   ON chat_messages (created_at DESC);
"""


# 依赖迁移补出来的列，必须在 migrate 之后才能建。
# 单独放一段而不是塞进 ADMIN_SCHEMA：建表脚本要在老库上先跑通，
# 而老库此时可能还没有这些列
POST_MIGRATE_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_users_dept ON users (department_id);
CREATE INDEX IF NOT EXISTS idx_doc_ver    ON policy_documents (doc_id, version);
CREATE INDEX IF NOT EXISTS idx_key_hash   ON api_keys (key_hash);
"""

def now_iso() -> str:
    """带时区的 ISO-8601 字符串。选字符串而不是 epoch 整数，是为了日志里直接可读，
    且字典序即时序（`ORDER BY created_at` 在 SQLite 上可以直接用）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    """32 位十六进制，替代 PG 的 uuid 列（去横线省 4 字节，仍可直接转回 UUID）。"""
    return uuid.uuid4().hex


def connect(db_path: str | Path) -> sqlite3.Connection:
    """
    每次操作新开一个连接 —— SQLite 打开开销约 0.1ms，远低于线程安全方案的心智成本。

    `isolation_level=None` 关掉 Python 的隐式事务管理，改为全部显式 BEGIN/COMMIT，
    这样"哪些语句在一个事务里"在代码里一眼可见，不依赖库的默认行为。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")      # 读写不互斥
        conn.execute("PRAGMA synchronous = NORMAL")    # 换取 5~10 倍写入速度
        conn.execute("PRAGMA busy_timeout = 5000")     # 写锁竞争时等待而非立刻报错
        conn.execute("PRAGMA foreign_keys = ON")       # SQLite 默认关闭，必须显式开
    except sqlite3.DatabaseError:
        # 只读介质或路径不可写时，让调用方在真正执行 SQL 时拿到错误，信息更准确
        pass
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """
    写事务。用 BEGIN IMMEDIATE 立刻取写锁，把并发写的冲突提前到事务开始，
    而不是让它发生在 COMMIT 时才炸（那时已经做了很多无用功）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """
    列增量迁移（幂等）。SQLite 的 ALTER TABLE ADD COLUMN 很安全，但不能重复执行，
    所以每次都先读 PRAGMA 判断列在不在 —— 而不是"试试看，报错就忽略"。

    返回实际执行的变更列表，启动日志里打出来，运维一眼知道库被改过什么。
    """
    applied: list[str] = []

    def columns(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    # prompt_version：提示词进了后台可随意改，答案一旦出问题必须能回答
    # "当时用的是哪版模板"—— 这条和"引用必须回到 L0"是同一条原则：结论可追溯。
    if "prompt_version" not in columns("chat_messages"):
        conn.execute("ALTER TABLE chat_messages ADD COLUMN prompt_version TEXT")
        applied.append("chat_messages.prompt_version")

    if "department_id" not in columns("users"):
        conn.execute("ALTER TABLE users ADD COLUMN department_id TEXT")
        applied.append("users.department_id")

    # —— policy_documents：从"doc_id 单主键"升级到"(doc_id, version) 唯一键" ——
    # SQLite 改不了主键，只能重建表。这个过程必须保住数据：
    # 先建新表 → 搬数据 → 删旧表 → 改名 → 补索引。整个流程在一个事务里，
    # 中途失败会整体回滚，不会留下"搬到一半"的库。
    doc_cols = columns("policy_documents")
    if "id" not in doc_cols or "version_label" not in doc_cols:
        old_rows = [dict(r) for r in conn.execute("SELECT * FROM policy_documents")]
        conn.execute("ALTER TABLE policy_documents RENAME TO policy_documents_old")
        # 新表结构从 ADMIN_SCHEMA 里取，避免"建表语句抄两份、改一处忘一处"
        for stmt in _split_sql(ADMIN_SCHEMA):
            if "CREATE TABLE IF NOT EXISTS policy_documents" in stmt:
                conn.execute(stmt)
                break
        for r in old_rows:
            conn.execute(
                """INSERT INTO policy_documents
                   (id, doc_id, title, doc_type, version, version_label, region_code,
                    effective_from, effective_to, source_file, storage_path, checksum,
                    status, chunk_count, char_count, uploaded_by, published_by,
                    superseded_at, indexed_at, deleted_at, deleted_by, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), r.get("doc_id"), r.get("title"), r.get("doc_type") or "policy",
                 int(r.get("version") or 1), str(r.get("version") or 1),
                 r.get("region_code") or "", r.get("effective_from"),
                 r.get("effective_to"), r.get("source_file"), r.get("storage_path"),
                 r.get("checksum") or "", r.get("status") or "active",
                 int(r.get("chunk_count") or 0), int(r.get("char_count") or 0),
                 r.get("uploaded_by"), r.get("uploaded_by"), None, r.get("indexed_at"),
                 r.get("deleted_at"), r.get("deleted_by"), r.get("created_at") or now_iso()),
            )
        conn.execute("DROP TABLE policy_documents_old")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_status ON policy_documents (status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_ver ON policy_documents (doc_id, version)")
        applied.append(f"policy_documents 重建为 (doc_id,version) 唯一键（迁移 {len(old_rows)} 行）")

    return applied


def _split_sql(script: str) -> list[str]:
    """把 executescript 用的脚本按分号切成单条语句，供需要逐条控制时使用。

    只在"脚本里没有字符串字面量含分号"的场景用（本项目的建表脚本满足），
    不做完整 SQL 解析 —— 那需要真正的分词器，不该为这一点需求引入。
    """
    return [s.strip() for s in script.split(";") if s.strip()]


def init_db(db_path: str | Path) -> None:
    """建表 + 迁移。幂等：可以每次启动都跑。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executescript(ADMIN_SCHEMA)
        # 迁移在前、后置索引在后：老库的 users 还没有 department_id，
        # 建表脚本里若带这一列的索引，executescript 会先炸掉
        apply_migrations(conn)
        conn.executescript(POST_MIGRATE_INDEXES)
    finally:
        conn.close()


def apply_migrations(conn: sqlite3.Connection) -> list[str]:
    """migrate 的执行包装：需要自己控制事务的调用方可以直接用它。"""
    with immediate(conn):
        return migrate(conn)


def cleanup_stale_streaming(db_path: str | Path, older_than_minutes: int = 10) -> int:
    """
    把"卡住的流式消息"收尾。进程崩了、容器被 kill 时，占位行会永远停在 streaming，
    导致该会话此后每次提问都返回 409。启动时跑一次即可。
    """
    cutoff = (datetime.now().astimezone() - timedelta(minutes=older_than_minutes)).isoformat(
        timespec="seconds")
    conn = connect(db_path)
    try:
        with immediate(conn):
            cur = conn.execute(
                """UPDATE chat_messages
                      SET answer_status = 'error',
                          content = content || '（服务中断，本条回答未完成）'
                    WHERE answer_status = 'streaming'
                      AND created_at < ?""",
                (cutoff,),
            )
            return cur.rowcount
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
