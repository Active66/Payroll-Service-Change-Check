"""安全原语：密码哈希、JWT 签发校验、CSRF 令牌、密钥落地。

    **这一层不依赖 FastAPI**，所以 CLI（payroll-check user add）和 Web 用的是
    同一套哈希与同一套 JWT 规则，不会出现"命令行建的账号网页登不上"。

    为什么用标准库 pbkdf2 而不是 bcrypt/passlib：
      pbkdf2_hmac 在 hashlib 里，FIPS 环境也支持，26 万轮 SHA-256 对内部后台足够；
      少一个原生扩展依赖，就少一类"Windows 上装不上"的事故。

    为什么 JWT 用 PyJWT 而不是自己拼 HS256：
      alg 混淆、签名比较要用 compare_digest、exp/nbf 的边界，这些坑没有理由自造。
"""

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt

# =============================================================================
# 【模块 19 · 安全原语】目标文件：src/my_tool/core/security.py
# ★核心模块：密码与令牌的**唯一**实现。任何地方不得绕过它自己算哈希或拼 token。
# =============================================================================

# PBKDF2 参数。轮数是"被拖库后攻击者算一次要多久"的唯一旋钮；
# 26 万轮在 2024 年的 OWASP 建议区间内，单次校验约 60ms，登录场景完全可接受。
_PBKDF2_ITER = 260_000
_ALGO_TAG = "pbkdf2_sha256"
JWT_ALG = "HS256"


# --------------------------------------------------------------------------- #
# 密码
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """
    产出 "pbkdf2_sha256$轮数$salt_b64$hash_b64"。

    把参数一起存在字符串里，是为了将来调轮数时**老密码仍能校验**——
    否则每次加固都要全量重置密码。
    """
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITER)
    return "%s$%d$%s$%s" % (
        _ALGO_TAG, _PBKDF2_ITER,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """恒定时间比较。解析失败（脏数据/算法升级中间态）一律判失败，绝不抛异常。"""
    try:
        tag, iter_s, salt_b64, hash_b64 = stored.split("$")
        if tag != _ALGO_TAG:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iter_s))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def is_strong_password(password: str) -> tuple[bool, str]:
    """
    强度校验。规则刻意保守到"能背下来"：≥8 位、含字母、含数字。

    不强制大小写和符号 —— 员工会把它写在便利贴上，那比弱密码更危险。
    """
    if len(password) < 8:
        return False, "密码至少 8 位"
    if not any(c.isalpha() for c in password):
        return False, "密码需包含字母"
    if not any(c.isdigit() for c in password):
        return False, "密码需包含数字"
    return True, ""


# --------------------------------------------------------------------------- #
# 密钥：落地到文件，而不是每次启动随机生成
# --------------------------------------------------------------------------- #
def load_or_create_secret(path: Path) -> str:
    """
    读本地密钥文件；不存在就生成一个。

    为什么不"启动时随机生成"：进程重启后密钥变了，所有人手里的 token 立刻失效，
    表现为"莫名其妙被踢下线"。多 worker 下各生成一个更糟 —— 彼此不认对方的 token。
    所以密钥必须持久化。
    """
    path = Path(path)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(48)
    path.write_text(secret, encoding="utf-8")
    try:
        os.chmod(path, 0o600)          # POSIX 生效；Windows 上退化为默认 ACL
    except OSError:
        pass
    return secret


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
def create_token(*, user_id: str, username: str, role: str, secret: str,
                 ttl_hours: int = 8) -> str:
    """
    签发访问令牌。

    载荷只放"渲染页面要用"的属性 + exp：
      · role 放进去是为了少查一次库？——不是，鉴权仍会查库（见 api/auth.py），
        这里放 role 只是为了让日志和前端能直接读到，不当作权限依据。
      · jti 让"同一用户多个设备"在日志里可区分。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=ttl_hours)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALG)


def decode_token(token: str, secret: str) -> dict[str, Any]:
    """
    校验并解出载荷。失败抛 jwt 的异常，由 api 层翻译成中文错误码。

    algorithms 显式限定 HS256：不限定的话，攻击者可以把 header 改成
    {"alg":"none"} 或换成非对称算法来绕过签名校验。
    """
    return jwt.decode(token, secret, algorithms=[JWT_ALG])


def new_csrf_token() -> str:
    """CSRF 令牌。双提交模式：一个放 cookie（可读），一个放表单隐藏字段，服务端比对。"""
    return secrets.token_urlsafe(24)


def csrf_equal(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)
