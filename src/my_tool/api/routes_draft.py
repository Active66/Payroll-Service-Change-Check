"""建单确认接口：`POST /api/agent/confirm-draft`。

    **这是"create_draft 前强制中断"的另一半。**

    循环在模型想调 create_draft 时把它拦住，把草稿预览发给员工（状态
    awaiting_confirmation）。员工在界面上看到草稿、点"确认建单"，
    前端就带着草稿预览里回传的 validation_token 调这个接口，
    由它完成真正的落库。

    为什么落库放在这里、而不是让模型第二轮继续执行 create_draft：

      ① **人确认是"动作"不是"话术"**。这个接口是登录态 + CSRF 保护的写操作，
        调用它本身就是"员工确认了"的证明，不需要再设计一套确认 token 让模型转交。
      ② **不依赖模型"记得"重试**。让模型第二轮"想起要再调一次 create_draft"，
        它会忘、会漏参数、会被其他上下文带偏。落库这个不可逆动作，
        不该押在模型的记忆力上。
      ③ **单一写入口**。建单只有这一个入口能落库（循环永远被拦），
        审计里"这张单是谁、在什么时间点确认的"一眼可查。

    安全性：这里**复用了 create_draft 工具本身的五道闸门**（验签 / 时效 /
    判定 / 内容哈希 / 一次性），不是另写一套更松的校验 —— 走 registry.call
    就等价于走了一遍 create_draft 的落库逻辑。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..core.agent.registry import ToolContext
from ..core.agent.toolset import build_registry
from ..utils.console import log
from . import auth
from .errors import MISSING_JSON, ApiError, ok_body

router = APIRouter()

# 单进程缓存一份 registry（工具定义无状态，复用省去每次重建）
_REGISTRY = build_registry()


@router.post("/api/agent/confirm-draft")
async def confirm_draft(request: Request,
                        user: dict = Depends(auth.current_user)) -> JSONResponse:
    """
    员工确认建单后，真正落库。

    请求体（前端从 awaiting_confirmation 的 draft_preview 原样取回）：

      {
        "validation_token": "vt_...",     // check_rules 签发的凭证，预览里回传的
        "change_type": "salary_adjust",
        "employee_no": "E1001",
        "items": [{"field": "fixed_salary", "old_value": "18500", "new_value": "19000", "effective_date": "2026-12-01"}],
        "materials": ["approval_form", "adjust_basis"],
        "idempotency_key": "..."          // 前端生成，防连点建两张单
      }

    落库逻辑完全复用 create_draft 工具（五道校验 + 幂等 + 凭证消费），
    不在这里重写 —— 重写必然会在某天和工具的校验规则分叉。
    """
    ctx = request.app.state.ctx
    raw = await request.body()
    if not raw:
        raise ApiError(MISSING_JSON, detail={"reason": "empty body"})
    import json
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise ApiError(MISSING_JSON, detail={"reason": str(ex)[:200]}) from ex
    if not isinstance(body, dict):
        raise ApiError(MISSING_JSON, detail={"reason": "body 必须是对象"})

    # 组装 create_draft 的入参。idempotency_key 前端没传就由服务端生成，
    # 但这样连点两次会建两张单 —— 所以前端必须传（见 app.js 的确认逻辑）
    idem = str(body.get("idempotency_key") or "").strip()
    if not idem:
        import uuid
        idem = "confirm-" + uuid.uuid4().hex[:16]

    args = {
        "validation_token": str(body.get("validation_token") or ""),
        "change_type": str(body.get("change_type") or ""),
        "employee_no": str(body.get("employee_no") or ""),
        "items": body.get("items") or [],
        "materials": body.get("materials") or [],
        "idempotency_key": idem,
        "note": str(body.get("note") or ""),
    }

    tool_ctx = ToolContext(
        settings=ctx.settings,
        user={"id": user["id"], "role": user.get("role", "employee"),
              "display_name": user.get("display_name", "")},
        session_id=str(body.get("session_id") or ""))

    # 走 create_draft 工具：闸门（五道校验 + 幂等 + 凭证消费）全在这里面
    res = _REGISTRY.call("create_draft", args, tool_ctx)

    if not res.ok:
        log(f"确认建单被拒（{user['id']}）：{res.error_code} {res.error_message}", "warn")
        return JSONResponse(
            status_code=409,
            content={"ok": False, "error": {"code": res.error_code,
                                            "message": res.error_message,
                                            "detail": {"retry": False}}},
        )

    data = res.data if isinstance(res.data, dict) else {}
    return JSONResponse(ok_body({
        "draft_id": data.get("draft_id"),
        "order_no": data.get("order_no"),
        "status": data.get("status"),
        "risk_level": data.get("risk_level"),
        "message": "已建单，请等待审批。",
    }))
