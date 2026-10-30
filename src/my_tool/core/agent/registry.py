"""Agent 工具注册表：schema、参数校验、调用、工具集裁剪。

    三件事必须由这一层统一做，不能让各工具自己做：

      ① **参数校验**。模型生成的 JSON 经常缺字段、类型不对、多传字段。
         在每个 handler 里写 if 判断，二十个工具就有二十种校验风格。
      ② **错误不抛给模型**。工具失败返回结构化的 `error_code`，
         模型能看到并调整；抛异常只会让整个循环崩掉，用户拿到一个 500。
      ③ **`next_allowed` 裁剪工具集**。这是"顺序约束"的第 2 层：
         不在上一步允许列表里的工具，下一轮**根本不出现**在上下文里。

    关于第 ③ 点的分寸：它降低跳步概率，但**不是安全边界** ——
    如果它算错了（比如 check_rules 判 pass 但实际该 block），模型就会合法地建单。
    真正的硬约束在 gate.py 的校验凭证上。两层都要有，不能只靠一层。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ...utils.console import log


# =============================================================================
# 【模块 30 · 工具注册表】目标文件：src/my_tool/core/agent/registry.py
# =============================================================================


@dataclass(frozen=True)
class AgentErr:
    """
    Agent 侧的错误规格。

    刻意**不继承** `api.errors.ErrSpec`：core 层不能依赖 api 层（原有分层是
    单向的 api → core）。这里只要 code + message 两个字段，api 层需要时
    自己映射成 HTTP 响应即可；工具返回的是字符串错误码，本来也不需要异常。
    """

    code: str
    message: str


# —— 闸门类（这一段是 Agent 与普通 API 最不同的地方）——
E_GATE_REQUIRED = AgentErr("AGT_001", "缺少校验凭证，请先完成规则校验")
E_GATE_EXPIRED = AgentErr("AGT_002", "校验凭证已过期，请重新校验")
E_GATE_STALE = AgentErr("AGT_003", "校验内容与提交内容不一致，请重新校验")
E_GATE_REUSED = AgentErr("AGT_004", "该校验凭证已被使用")
E_BLOCKED = AgentErr("AGT_005", "存在阻断项，不得建单")
E_TOOL_FAILED = AgentErr("AGT_006", "工具执行失败")
E_BAD_ARGS = AgentErr("AGT_007", "工具参数不合法：{reason}")
E_BUDGET_EXCEEDED = AgentErr("AGT_008", "本轮工具调用次数已达上限")
E_CONFIRM_REQUIRED = AgentErr("AGT_009", "提交需要用户确认")
E_NOT_FOUND = AgentErr("AGT_010", "查询对象不存在：{what}")


@dataclass
class ToolResult:
    """工具的统一返回。

    `next_allowed` 为空表示"不做限制"（读工具之间可以自由跳转），
    非空表示"下一步只能这几个"。
    """

    ok: bool = True
    data: Any = None
    error_code: str = ""
    error_message: str = ""
    next_allowed: list[str] = field(default_factory=list)
    # 给审计用的摘要：结果可能很大（切片正文），轨迹里只存摘要
    summary: str = ""

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            body["data"] = self.data
        else:
            body["error"] = {"code": self.error_code, "message": self.error_message}
        if self.next_allowed:
            body["next_allowed"] = self.next_allowed
        return body


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[["ToolContext", dict[str, Any]], ToolResult]
    # 有副作用的工具：不可并行、必须过闸、必须进审计的"写"分支
    writes: bool = False
    # 静态的下一步允许列表；handler 可以返回更精确的（如 check_rules 按 verdict 分叉）
    next_allowed: list[str] = field(default_factory=list)
    # 调用前需要**人工确认**：循环在真正执行它之前强制中断，
    # 把要做的动作发给用户，等确认后才执行。这一位比 writes 更强 ——
    # writes 只是"要审计"，requires_human_confirm 是"不许 Agent 自己做"。
    requires_human_confirm: bool = False

    def as_llm_schema(self) -> dict[str, Any]:
        """转成 function calling 的 tool 定义（OpenAI 风格，各家兼容端点通用）。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolContext:
    """一次工具调用的上下文。

    刻意只有几个字段：工具能拿到的东西越少，越难写出"顺手改点别的"的代码。
    数据库连接由 registry 按需开关，工具自己不持有连接（避免长事务）。
    """

    def __init__(self, *, settings: Any, user: dict[str, Any] | None = None,
                 session_id: str = "", turn_no: int = 1,
                 retriever: Any = None, workspace: dict[str, Any] | None = None):
        self.settings = settings
        self.user = user or {}
        self.session_id = session_id
        self.turn_no = turn_no
        # 检索器由外部注入：工具不该自己 build 一遍索引（内存里已经有一份了）
        self.retriever = retriever
        # 跨工具共享的临时状态（如已取到的员工档案，避免重复查库）
        self.workspace = workspace if workspace is not None else {}

    @property
    def user_id(self) -> str:
        return str(self.user.get("id") or "")

    @property
    def is_admin(self) -> bool:
        return self.user.get("role") == "admin"


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"工具重名：{spec.name}")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        """
        交给模型的工具定义。

        `allowed` 为 None 表示不裁剪（第一轮）；否则只放出列表里的工具。
        裁剪在**服务端**做而不是在提示词里说"你现在只能用 X"——
        模型看不见的工具，比"被告知不要用的工具"可靠得多。
        """
        names = list(self._tools) if allowed is None else allowed
        return [self._tools[n].as_llm_schema() for n in names if n in self._tools]

    def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        spec = self._tools.get(name)
        if spec is None:
            # 模型幻觉出一个不存在的工具：不要崩，也不要静默成功。
            # 明确告诉它"没有这个工具"，并把可用工具列出来 —— 否则它会反复试。
            return ToolResult(ok=False, error_code=E_BAD_ARGS.code,
                              error_message=f"没有名为 {name} 的工具。可用工具："
                                            f"{'、'.join(self._tools)}")

        problem = validate_args(spec.parameters, args)
        if problem:
            return ToolResult(ok=False, error_code=E_BAD_ARGS.code,
                              error_message=E_BAD_ARGS.message.format(reason=problem))

        t0 = time.perf_counter()
        try:
            res = spec.handler(ctx, args)
        except Exception as ex:                    # noqa: BLE001
            # 工具内部的 bug 不该让整个对话 500。记日志、返回结构化错误，
            # 模型会据此转人工 —— 这比抛出去让用户看到"服务器错误"好得多。
            log(f"工具 {name} 执行失败：{type(ex).__name__}: {ex}", "err")
            res = ToolResult(ok=False, error_code=E_TOOL_FAILED.code,
                             error_message=E_TOOL_FAILED.message)
        elapsed = int((time.perf_counter() - t0) * 1000)

        # `next_allowed` 的套用规则分成功/失败两条，不能混：
        #   · 成功 → 用工具声明的"下一步"（如 create_draft 之后只能 submit / escalate）
        #   · 失败 → 用**恢复路径**（重新校验 / 转人工 / 追问），绝不套用成功路径。
        #     否则建单失败后会提示"下一步只能 submit_draft"，而根本没有单可提交 ——
        #     模型会被引到一个必然再失败的动作上。这条是演示脚本跑出来的真问题。
        if not res.next_allowed:
            if res.ok:
                res.next_allowed = list(spec.next_allowed)
            else:
                res.next_allowed = _recovery_path(spec)
        if not res.summary:
            res.summary = _summarize(res)
        _trace(ctx, spec, args, res, elapsed)
        return res


def _recovery_path(spec: ToolSpec) -> list[str]:
    """
    失败后的允许路径。

    刻意**不含刚失败的那个工具**：它刚才就失败了，再给它一次只会助长
    "换个参数重试"。而模型提示词里要求"失败两次就转人工"，
    所以真正需要保留的是重新校验和转人工。
    """
    path = ["escalate_to_human", "ask_clarification"]
    if spec.name in ("create_draft", "submit_draft"):
        path.insert(0, "check_rules")      # 凭证问题一律回到校验
    return path


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str:
    """
    极简 JSON Schema 校验：required / type / enum / minimum / maximum / maxLength。

    刻意不引 jsonschema 库：工具参数只有这几类约束，引一个库要处理
    draft 版本、$ref、format 这些本系统永远用不到的东西，还多一个依赖。

    返回空串表示通过，否则返回一句人话（会被放进错误消息给模型看，
    所以要说清"哪个字段、什么问题"，不能说"schema invalid"）。
    """
    if not isinstance(args, dict):
        return "参数必须是一个 JSON 对象"

    props: dict[str, Any] = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if args.get(key) in (None, "", []):
            return f"缺少必填参数 {key}"

    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                return f"不认识的参数 {key}"
            continue
        expect = spec.get("type")
        if expect and not _type_ok(expect, value):
            return f"参数 {key} 应为 {expect}"
        if "enum" in spec and value not in spec["enum"]:
            return f"参数 {key} 只能是 {'/'.join(map(str, spec['enum']))}"
        if expect in ("integer", "number"):
            low, high = spec.get("minimum"), spec.get("maximum")
            if low is not None and value < low:
                return f"参数 {key} 不能小于 {low}"
            if high is not None and value > high:
                return f"参数 {key} 不能大于 {high}"
        if expect == "string":
            cap = spec.get("maxLength")
            if cap and len(str(value)) > cap:
                return f"参数 {key} 超过 {cap} 字"
        if expect == "array":
            lo = spec.get("minItems")
            hi = spec.get("maxItems")
            if lo is not None and len(value) < lo:
                return f"参数 {key} 至少 {lo} 项"
            if hi is not None and len(value) > hi:
                return f"参数 {key} 最多 {hi} 项"
    return ""


def _type_ok(expect: str, value: Any) -> bool:
    if expect == "string":
        return isinstance(value, str)
    if expect == "integer":
        # bool 是 int 的子类，必须单独排除，否则 True 会被当成 1
        return isinstance(value, int) and not isinstance(value, bool)
    if expect == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expect == "boolean":
        return isinstance(value, bool)
    if expect == "array":
        return isinstance(value, list)
    if expect == "object":
        return isinstance(value, dict)
    return True


def _summarize(res: ToolResult) -> str:
    """轨迹里存的摘要。正文可能几千字，全存会让轨迹表变成第二个文档库。"""
    if not res.ok:
        return f"{res.error_code} {res.error_message}"[:200]
    if isinstance(res.data, dict):
        keys = ",".join(list(res.data)[:6])
        return f"ok keys=[{keys}]"
    if isinstance(res.data, list):
        return f"ok {len(res.data)} 条"
    return f"ok {str(res.data)[:120]}"


def _trace(ctx: ToolContext, spec: ToolSpec, args: dict[str, Any],
           res: ToolResult, elapsed_ms: int) -> None:
    """
    写轨迹。失败**不影响**工具结果 —— 审计是旁路，不是主链路。
    但如果轨迹写不进去，就不能回答"它为什么这么判断"，所以失败要大声记日志。
    """
    from ...store import db, trace_repo

    conn = None
    try:
        conn = db.connect(ctx.settings.chat_db_path)
        trace_repo.record(
            conn, session_id=ctx.session_id, turn_no=ctx.turn_no,
            tool_name=spec.name, args=args,
            result={"summary": res.summary, "ok": res.ok,
                    "next_allowed": res.next_allowed},
            ok=res.ok, error_code=res.error_code or None, latency_ms=elapsed_ms)
    except Exception as ex:                        # noqa: BLE001
        log(f"写 Agent 轨迹失败（{spec.name}）：{ex}", "warn")
    finally:
        if conn is not None:
            conn.close()


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
