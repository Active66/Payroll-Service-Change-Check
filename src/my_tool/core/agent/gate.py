"""校验凭证（validation_token / confirm_token）的签发与核验。

    **这是"顺序约束"的硬边界所在。** registry 的 next_allowed 只是让模型
    "看不见不该用的工具"，而这里是让模型"用不了"——即使它拿到了名字、拼出了参数，
    没有凭证就是写不进库。

    凭证的结构：

        payload = {jti, order_hash, verdict, level, issued_at, exp, iss}
        token   = base64url(json(payload)) + "." + base64url(hmac_sha256(secret, payload))

    为什么必须是这个结构，而不是一个 `validated: true` 的布尔标记：

      · 布尔标记挡不住"校验的是 A、建的是 B"。Agent 完全可能在 check_rules
        里报 5000 元的调薪、拿到 pass，然后建一张 50000 元的单。order_hash
        绑定内容之后，这种改法会让签名校验失败 —— 这是本设计里最容易被低估的一条。
      · 布尔标记挡不住"昨天的校验今天还用"。exp 解决。
      · 布尔标记挡不住"一张凭证建十张单"。jti + gate_tokens 表解决。
      · 布尔标记可以被模型自己拼出来。HMAC 不行（密钥只在服务端）。

    为什么不落库存 token 本身：无状态凭证让 check_rules 可以随便重入、可以并发，
    不需要清理过期数据。库里只记"哪个 jti 被用掉了"，那是唯一需要持久化的事实。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from ...store import db
from .registry import (E_BLOCKED, E_CONFIRM_REQUIRED, E_GATE_EXPIRED,
                       E_GATE_REQUIRED, E_GATE_REUSED, E_GATE_STALE)


# =============================================================================
# 【模块 31 · 校验凭证】目标文件：src/my_tool/core/agent/gate.py
# =============================================================================

ISSUER = "rule_engine"


def _b64e(raw: bytes) -> str:
    """base64url 去 padding：token 会被塞进 JSON 和日志，`=` 只会添麻烦。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _secret(settings: Any) -> bytes:
    """
    取签名密钥。优先 GATE_SECRET；没配就从 auth_secret 派生。

    派生而不是复用，是为了"两把锁不共用一把钥匙"：auth_secret 泄露不该
    连带让校验凭证可伪造。用 HMAC 做 KDF，不从 auth_secret 直接切片。
    """
    base = getattr(settings, "gate_secret", "") or getattr(settings, "auth_secret", "")
    if not base:
        # 两者都没有（纯 CLI / 单测场景）：用一个进程内随机密钥。
        # 代价是重启后旧凭证失效——在单测里正是想要的行为。
        base = _FALLBACK_SECRET
    return hmac.new(base.encode("utf-8"), b"payroll-gate-v1", hashlib.sha256).digest()


_FALLBACK_SECRET = secrets.token_urlsafe(32)


def order_hash(change_type: str, employee_no: str, items: list[dict[str, Any]],
               materials: list[str] | None = None) -> str:
    """
    内容指纹。**规范化**是关键：同一份内容换了字段顺序，必须得到同一个哈希。

    规范化规则：
      · items 按 (field, new_value) 排序 —— 明细项的顺序不影响业务含义；
      · 只取参与判定的字段：field / old_value / new_value / effective_date。
        多传的字段（比如前端顺手带的 label）不该改变指纹，否则一次无意义的
        前端升级就会让所有凭证失效；
      · materials 去重排序 —— 顺序不影响"材料齐不齐"；
      · 数字统一成字符串并去空格，避免 5000 / "5000" / "5 000" 算出三个哈希。
    """
    norm_items = sorted(
        (
            {
                "field": str(i.get("field") or "").strip(),
                "old_value": _norm(i.get("old_value")),
                "new_value": _norm(i.get("new_value")),
                "effective_date": str(i.get("effective_date") or "").strip(),
            }
            for i in items
        ),
        key=lambda i: (i["field"], i["new_value"], i["effective_date"]),
    )
    payload = {
        "change_type": str(change_type or "").strip(),
        "employee_no": str(employee_no or "").strip(),
        "items": norm_items,
        "materials": sorted({str(m).strip() for m in (materials or [])}),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _norm(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip().replace(",", "").replace("，", "").replace(" ", "")
    # "5000.00" 与 "5000" 应当同指纹
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else str(f)
    except ValueError:
        return s


@dataclass
class TokenInfo:
    """核验通过后的凭证内容。调用方拿它写审计（jti 是主线）。"""

    jti: str
    order_hash: str
    verdict: str
    level: str | None
    exp: int


def issue_validation(settings: Any, *, content_hash: str, level: str | None,
                     forced: bool = False) -> tuple[str, int]:
    """
    签发校验凭证。返回 (token, 有效期秒数)。

    **只在判定通过时调用。** 这个函数不做判定——判定是 rules.py 的事。
    两者分开是为了防止"顺手在签发时放宽条件"这类改动：
    想放宽必须改规则，改规则会被规则的单测拦住。
    """
    ttl = int(getattr(settings, "gate_token_ttl_seconds", 600) or 600)
    now = int(time.time())
    payload = {
        "jti": secrets.token_hex(12),
        "order_hash": content_hash,
        "verdict": "pass",
        "level": level,
        "issued_at": now,
        "exp": now + ttl,
        "iss": ISSUER,
        # forced 只用于"人工强制"，本期不开放；留字段是为了将来不破坏 token 结构
        "forced": bool(forced),
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_secret(settings), body, hashlib.sha256).digest()
    return f"{_b64e(body)}.{_b64e(sig)}", ttl


def verify_validation(settings: Any, token: str, *, content_hash: str,
                      blocking_final: bool = False) -> tuple[TokenInfo | None, Any]:
    """
    核验凭证。返回 (info, error)。

    五道校验的顺序有讲究：**先验签名，再验时效，最后验内容**。
    反过来的话，一个伪造的 token 会先被拿去和内容比对，
    等于给攻击者一个"内容是否匹配"的探测接口。
    """
    if not token:
        return None, E_GATE_REQUIRED

    parts = token.split(".")
    if len(parts) != 2:
        return None, E_GATE_REQUIRED
    body_b64, sig_b64 = parts
    try:
        body = _b64d(body_b64)
        sig = _b64d(sig_b64)
    except Exception:                            # noqa: BLE001
        return None, E_GATE_REQUIRED

    # ① 签名
    expect = hmac.new(_secret(settings), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        return None, E_GATE_REQUIRED

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, E_GATE_REQUIRED

    # ② 时效
    if int(payload.get("exp") or 0) < int(time.time()):
        return None, E_GATE_EXPIRED

    # ③ 判定本身
    if payload.get("verdict") != "pass":
        return None, E_BLOCKED
    if blocking_final:
        # 双保险：即使凭证是 pass，只要当前存在 final 阻断，就不放行。
        # 场景是"签发之后制度参数被改了"——凭证还是好的，但结论已经不对了
        return None, E_BLOCKED

    # ④ 内容绑定
    if not hmac.compare_digest(str(payload.get("order_hash") or ""), content_hash):
        return None, E_GATE_STALE

    info = TokenInfo(
        jti=str(payload.get("jti") or ""),
        order_hash=str(payload.get("order_hash") or ""),
        verdict=str(payload.get("verdict") or ""),
        level=payload.get("level"),
        exp=int(payload.get("exp") or 0),
    )
    return info, None


def consume(settings: Any, info: TokenInfo, *, subject_id: str = "",
            consumed_by: str = "") -> tuple[bool, Any]:
    """
    标记凭证已使用（第 5 道校验：一次性）。返回 (成功, error)。

    用主键冲突而不是"先查再插"：两个并发请求同时查都会得到"没用过"，
    然后都插入成功——但主键冲突只能有一个成功。这是唯一可靠的做法。
    """
    conn = db.connect(settings.chat_db_path)
    try:
        conn.execute(
            """INSERT INTO gate_tokens (jti, kind, subject_id, consumed_at, consumed_by)
               VALUES (?, 'validation', ?, ?, ?)""",
            (info.jti, subject_id, _now(), consumed_by))
        return True, None
    except Exception as ex:                        # noqa: BLE001
        if "UNIQUE" in str(ex) or "PRIMARY" in str(ex).upper():
            return False, E_GATE_REUSED
        raise
    finally:
        conn.close()


def issue_confirm(settings: Any, *, draft_id: str, content_hash: str) -> tuple[str, int]:
    """
    签发人工确认凭证。**只由 /api/agent/confirm 调用**，而那个接口需要登录态。

    模型没有任何工具能走到这里 —— 这是设计里唯一一处"模型绝对做不到"的约束，
    因为它是"人"的凭证，不是"系统"的凭证（与阶段 4 的 JWT vs API Key 同源）。
    """
    ttl = int(getattr(settings, "gate_token_ttl_seconds", 600) or 600)
    now = int(time.time())
    payload = {
        "jti": secrets.token_hex(12),
        "kind": "confirm",
        "draft_id": draft_id,
        "order_hash": content_hash,
        "by": "human",
        "issued_at": now,
        "exp": now + ttl,
        "iss": "console",
    }
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_secret(settings), body, hashlib.sha256).digest()
    return f"{_b64e(body)}.{_b64e(sig)}", ttl


def verify_confirm(settings: Any, token: str, *, draft_id: str,
                   content_hash: str) -> tuple[dict[str, Any] | None, Any]:
    info, err = _decode(settings, token)
    if err is not None:
        return None, err
    if info.get("kind") != "confirm" or info.get("by") != "human":
        # 拿校验凭证冒充确认凭证：这正是需要拦住的那类"绕过"
        return None, E_CONFIRM_REQUIRED
    if int(info.get("exp") or 0) < int(time.time()):
        return None, E_GATE_EXPIRED
    if str(info.get("draft_id") or "") != draft_id:
        return None, E_GATE_STALE
    if not hmac.compare_digest(str(info.get("order_hash") or ""), content_hash):
        return None, E_GATE_STALE
    return info, None


def _decode(settings: Any, token: str) -> tuple[dict[str, Any], Any]:
    if not token or token.count(".") != 1:
        return {}, E_GATE_REQUIRED
    body_b64, sig_b64 = token.split(".")
    try:
        body, sig = _b64d(body_b64), _b64d(sig_b64)
    except Exception:                              # noqa: BLE001
        return {}, E_GATE_REQUIRED
    expect = hmac.new(_secret(settings), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expect):
        return {}, E_GATE_REQUIRED
    try:
        return json.loads(body.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, E_GATE_REQUIRED


def _now() -> str:
    from ...store.db import now_iso
    return now_iso()
