"""错误码与统一错误契约。**所有对外中文错误信息只在这里定义**，改文案只改这一个文件。
"""

from dataclasses import dataclass
from typing import Any


# =============================================================================
# 【模块 17 · 错误契约】目标文件：src/my_tool/api/errors.py
# ★核心模块（阶段 3 复用）：code 是机器契约、message 是给人看的中文。
#
#   两条规矩：
#     ① code 一旦发布永不复用、永不改语义 —— 前端与监控都按它做判断；
#     ② message 里不出现堆栈、类名、表名、字段名，也不出现英文异常原文。
#        技术细节全部塞进 detail，只在日志/调试时看。
# =============================================================================


@dataclass(frozen=True)
class ErrSpec:
    """一条错误规格：错误码 + HTTP 状态码 + 默认中文文案（可带 {占位符}）。"""

    code: str
    status: int
    message: str


# —— 请求校验（400 段）——
MISSING_JSON = ErrSpec("REQ_001", 400, "请求格式不正确，请确认发送的是合法 JSON")
MISSING_FIELD = ErrSpec("REQ_002", 400, "缺少必填参数：{field}")
EMPTY_QUESTION = ErrSpec("REQ_003", 400, "问题不能为空，请先输入想问的内容")
QUESTION_TOO_LONG = ErrSpec("REQ_004", 400, "问题过长（上限 {limit} 字），请拆分后再问")
BAD_SESSION_ID = ErrSpec("REQ_005", 400, "会话标识格式不正确")
NO_SUCH_ROUTE = ErrSpec("REQ_006", 404, "接口不存在或请求方法不允许")

# —— 会话（404 / 410 / 409）——
SESSION_NOT_FOUND = ErrSpec("SES_001", 404, "会话不存在或已被清理，请新建一个对话")
SESSION_DELETED = ErrSpec("SES_002", 410, "该会话已删除，无法继续提问")
SESSION_BUSY = ErrSpec("SES_004", 409, "该会话正在生成回答，请等待完成或先停止")

# —— 消息（404）——
MESSAGE_NOT_FOUND = ErrSpec("MSG_001", 404, "消息不存在，无法记录反馈")

# —— 索引（503）——
INDEX_NOT_READY = ErrSpec("IDX_001", 503, "制度索引尚未就绪，请联系管理员初始化")
INDEX_DRIFT = ErrSpec("IDX_002", 503, "索引与当前向量模型不一致，需要重建索引")

# —— 上游生成（504 / 502）——
LLM_TIMEOUT = ErrSpec("LLM_001", 504, "生成回答超时，请稍后重试")
LLM_BAD_REQUEST = ErrSpec("LLM_002", 502, "模型服务配置有误（鉴权失败或参数不合法）")
LLM_UNAVAILABLE = ErrSpec("LLM_003", 502, "模型服务暂时不可用，请稍后重试")

# —— 服务自身（500 / 429）——
INTERNAL = ErrSpec("SRV_001", 500, "服务内部错误，请稍后重试（编号 {trace_id}）")
TOO_BUSY = ErrSpec("SRV_002", 429, "当前请求过多，请稍后再试")

# —— 认证与授权（401 / 403 / 429）——
# 注意 AUTH_002 刻意不区分"用户不存在"与"密码错误"：分开写等于送给攻击者一个
# 账号枚举接口。忘了密码的人看到"密码错误"和"用户不存在"都要去找管理员。
AUTH_REQUIRED_ERR = ErrSpec("AUTH_001", 401, "登录已过期，请重新登录")
AUTH_BAD_CREDENTIALS = ErrSpec("AUTH_002", 401, "用户名或密码不正确")
AUTH_FORBIDDEN = ErrSpec("AUTH_003", 403, "没有权限访问该功能")
AUTH_TOO_MANY_TRIES = ErrSpec("AUTH_004", 429, "登录尝试过于频繁，请 {minutes} 分钟后再试")
AUTH_WEAK_PASSWORD = ErrSpec("AUTH_005", 400, "密码强度不足：{reason}")
AUTH_CSRF = ErrSpec("AUTH_006", 400, "页面已过期，请刷新后重新提交")

# —— 知识库（400 / 409 / 413 / 422 / 500）——
KB_BAD_TYPE = ErrSpec("KB_001", 400, "不支持的文件类型，仅接受 .md / .txt / .pdf")
KB_TOO_LARGE = ErrSpec("KB_002", 413, "文件过大（上限 {limit} MB）")
KB_DUPLICATE = ErrSpec("KB_003", 409, "该文档已存在相同版本，请提升版本号或先删除旧版")
KB_PARSE_FAILED = ErrSpec("KB_004", 422, "文件解析失败：{reason}")
KB_REBUILDING = ErrSpec("KB_005", 409, "索引正在重建，请等待完成后再操作")
KB_REBUILD_FAILED = ErrSpec("KB_006", 500, "索引重建失败，已保留原索引")
KB_NOT_FOUND = ErrSpec("KB_007", 404, "制度文档不存在")

# —— 提示词模板（400 / 404）——
PRM_BAD_TEMPLATE = ErrSpec("PRM_001", 400, "模板缺少必需占位符：{missing}")
PRM_NOT_FOUND = ErrSpec("PRM_002", 404, "模板版本不存在")

# —— 审计（404）——
AUD_NOT_FOUND = ErrSpec("AUD_001", 404, "消息不存在，无法标记")


class ApiError(Exception):
    """
    业务异常。抛出后由 app.py 注册的处理器统一翻译成 JSON 错误体，
    所以 route 里不需要写任何 try/except 去构造响应。
    """

    def __init__(self, spec: ErrSpec, detail: dict[str, Any] | None = None, **fmt: Any):
        self.spec = spec
        self.detail = detail or {}
        # 占位符缺失时退化为原文案，绝不因为"文案模板写错"把 500 抛给用户
        try:
            self.message = spec.message.format(**fmt) if fmt else spec.message
        except (KeyError, IndexError):
            self.message = spec.message
        super().__init__(self.message)

    @property
    def code(self) -> str:
        return self.spec.code

    @property
    def status(self) -> int:
        return self.spec.status


def error_body(code: str, message: str, trace_id: str = "",
               detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """统一错误体。SSE 的 `event: error` 也用这个结构，前端可以复用同一个渲染函数。"""
    body: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message,
                                                  "trace_id": trace_id}}
    if detail:
        body["error"]["detail"] = detail
    return body


def ok_body(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def translate_llm_error(ex: BaseException) -> ApiError:
    """
    把上游异常翻译成对外错误码。

    上游原始信息（含 URL、状态码、返回片段）只进 detail 与日志，
    绝不进 message —— 员工看到"401 Unauthorized"只会一头雾水，还泄露了内部结构。
    """
    text = str(ex)
    detail = {"upstream": text[:500]}
    low = text.lower()
    if "timed out" in low or "超时" in text or "timeout" in low:
        return ApiError(LLM_TIMEOUT, detail)
    if " 401" in text or " 403" in text or " 400" in text:
        return ApiError(LLM_BAD_REQUEST, detail)
    return ApiError(LLM_UNAVAILABLE, detail)
