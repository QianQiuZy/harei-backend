import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from email.message import EmailMessage
from typing import Literal

from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.rate_limit import BEIJING_WINDOW
from app.services.gift_mail import send_gift_message

ROOM = 1820703922
CODE_TTL = 600
DM_CODE_LENGTH = 8
DM_CODE_PATTERN = re.compile(rf"[0-9]{{{DM_CODE_LENGTH}}}")
VERIFICATION_STATUS_PATH = "/user-auth/verification-status"
RESEND_SECONDS = 60
DAILY_EMAIL_LIMIT = 10
FLOW_TTL = 1800
SESSION_COOKIE = "harei_member"
PREFIX = "harei:members:"
legacy_hasher = PasswordHasher()
PASSWORD_ROUNDS = 600_000
member_hash_limiter = CapacityLimiter(2)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    result = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ROUNDS)
    return f"pbkdf2_sha256${PASSWORD_ROUNDS}${salt.hex()}${result.hex()}"


def verify_password(encoded: str, password: str) -> bool:
    if encoded.startswith("$argon2"):
        try:
            return legacy_hasher.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False
    try:
        kind, rounds, salt, expected = encoded.split("$")
        if kind != "pbkdf2_sha256" or int(rounds) != PASSWORD_ROUNDS:
            return False
        salt, expected = bytes.fromhex(salt), bytes.fromhex(expected)
        if len(salt) != 16 or len(expected) != 32:
            return False
        received = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, PASSWORD_ROUNDS
        )
        return hmac.compare_digest(received, expected)
    except (TypeError, ValueError):
        return False


EmailCodeAction = Literal["register", "reset", "change_old", "change_new", "deletion"]
EMAIL_CODE_LABELS = {
    "register": "账号注册",
    "reset": "重置密码",
    "change_old": "换绑邮箱（验证原邮箱）",
    "change_new": "换绑邮箱（验证新邮箱）",
    "deletion": "注销账号",
}


def digest(*parts: str) -> str:
    message = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    return hmac.new(
        get_settings().app_secret_key.encode(), message, hashlib.sha256
    ).hexdigest()


def cipher() -> Fernet:
    key = get_settings().gift_address_key
    return Fernet(
        key.encode()
        if key
        else base64.urlsafe_b64encode(bytes.fromhex(digest("address-v1")))
    )


def seal(value: dict) -> str:
    return cipher().encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()


def reveal(value: str | None) -> dict | None:
    return json.loads(cipher().decrypt(value.encode())) if value else None


async def new_password_hash(password: str) -> str:
    return await to_thread.run_sync(
        hash_password, password, limiter=member_hash_limiter
    )


async def password_ok(encoded: str | None, password: str) -> bool:
    if not encoded:
        return False
    return await to_thread.run_sync(
        verify_password, encoded, password, limiter=member_hash_limiter
    )


def require_origin(request: Request):
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    allowed = get_settings().cors_allow_origins_list
    if (
        request.method == "POST"
        and request.url.path == VERIFICATION_STATUS_PATH
        and origin in allowed
        and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/plain"
    ):
        return
    if request.headers.get("x-harei-request") != "1" or (
        origin and origin not in allowed
    ):
        raise HTTPException(403, "请求来源无效")


async def limit(
    redis: Redis, category: str, identifier: str, maximum: int, seconds: int
):
    key = PREFIX + "limit:" + digest(category, identifier)
    count, remaining = await redis.eval(
        """
        local n = redis.call('INCR', KEYS[1])
        if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
        return {n, redis.call('TTL', KEYS[1])}
    """,
        1,
        key,
        seconds,
    )
    if count > maximum:
        raise HTTPException(
            429,
            "请求过于频繁，请稍后重试",
            headers={"Retry-After": str(max(1, remaining))},
        )


def smtp_send(recipient: str, subject: str, body: str, priority: bool):
    msg = EmailMessage()
    msg["To"], msg["Subject"] = recipient, subject
    msg.set_content(body)
    send_gift_message(get_settings(), msg, [recipient], priority=priority)


async def deliver(email: str, subject: str, body: str, *, bulk=False):
    await asyncio.to_thread(smtp_send, email, subject, body, not bulk)


def email_key(email: str, purpose: str) -> str:
    return PREFIX + "email:" + digest(email, purpose)


EMAIL_LIMIT_SCRIPT = (
    BEIJING_WINDOW
    + """
local day, used = unpack(redis.call('HMGET', KEYS[1], 'day', 'count'))
if tonumber(day) == day_start and tonumber(used) >= tonumber(ARGV[1]) then
    return {2, midnight - now}
end
local wait = redis.call('PTTL', KEYS[2])
if wait >= 0 then return {1, math.max(1, math.ceil(wait / 1000))} end
if tonumber(day) ~= day_start then redis.call('HSET', KEYS[1], 'day', day_start, 'count', 0) end
redis.call('HINCRBY', KEYS[1], 'count', 1)
redis.call('EXPIREAT', KEYS[1], midnight)
redis.call('SET', KEYS[2], '1', 'EX', ARGV[2])
return {0, 0}
"""
)


async def reserve_email_send(
    limits: Redis, email: str, scope: Literal["ip", "account"], subject: str
):
    cooldown = PREFIX + "limit:" + digest("email-resend", email)
    daily = f"rate_limit:mail:{scope}:{digest(scope, subject)}"
    gate, wait = await limits.eval(
        EMAIL_LIMIT_SCRIPT, 2, daily, cooldown, DAILY_EMAIL_LIMIT, RESEND_SECONDS
    )
    if gate:
        message = (
            "今日验证码发送次数已达上限，北京时间零点后可重试"
            if gate == 2
            else "请等待后再发送邮箱验证码"
        )
        raise HTTPException(429, message, headers={"Retry-After": str(wait)})


async def send_code(redis: Redis, email: str, purpose: str, *, action: EmailCodeAction):
    key = email_key(email, purpose)
    label = EMAIL_CODE_LABELS[action]
    code = f"{secrets.randbelow(1_000_000):06d}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(
            key, mapping={"proof": digest("email", purpose, email, code), "tries": 0}
        )
        pipe.expire(key, CODE_TTL)
        await pipe.execute()
    try:
        await deliver(
            email,
            f"harei.cn {label}验证码",
            f"{code} 是harei.cn{label}的验证码\n有效期{CODE_TTL // 60}分钟",
        )
    except Exception as exc:
        raise HTTPException(
            503,
            "验证码投递失败，请稍后重发",
            headers={"Retry-After": str(RESEND_SECONDS)},
        ) from exc


async def verify_code(redis: Redis, email: str, purpose: str, code: str):
    ok = await redis.eval(
        """
        local expected = redis.call('HGET', KEYS[1], 'proof')
        if not expected then return 0 end
        if expected == ARGV[1] then return redis.call('DEL', KEYS[1]) end
        if redis.call('HINCRBY', KEYS[1], 'tries', 1) >= 5 then redis.call('DEL', KEYS[1]) end
        return 0
    """,
        1,
        email_key(email, purpose),
        digest("email", purpose, email, code),
    )
    if not ok:
        raise HTTPException(400, "验证码错误或已失效")


def account_proof_scope(member, request: Request):
    return digest(
        str(member.id), str(member.session_version), request.cookies[SESSION_COOKIE]
    )


async def start_email_change(redis: Redis, scope: str):
    flow = secrets.token_urlsafe(32)
    key = PREFIX + "change:" + scope
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(key, digest(flow), ex=CODE_TTL)
        pipe.delete(key + ":new")
        await pipe.execute()
    return flow


async def check_email_change(
    redis: Redis, scope: str, flow: str, email: str, *, choosing=False
):
    key = PREFIX + "change:" + scope
    grant = await redis.get(key if choosing else key + ":new")
    if grant != digest(flow, email) and not (choosing and grant == digest(flow)):
        raise HTTPException(410, "请重新验证原邮箱")


async def authorize_new_email(redis: Redis, scope: str, flow: str, email: str):
    # Only the session that verified the original mailbox may choose a recipient.
    # Concurrent requests for two different recipients cannot both claim the grant.
    ok = await redis.eval(
        """
        local current = redis.call('GET', KEYS[1])
        if current ~= ARGV[1] and current ~= ARGV[2] then return 0 end
        redis.call('SET', KEYS[1], ARGV[2], 'KEEPTTL')
        redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
        return 1
    """,
        2,
        PREFIX + "change:" + scope,
        PREFIX + "change:" + scope + ":new",
        digest(flow),
        digest(flow, email),
        CODE_TTL,
    )
    if not ok:
        raise HTTPException(410, "请重新验证原邮箱")


def flow_key(token: str) -> str:
    return PREFIX + "flow:" + digest("flow", token)


async def new_flow(redis: Redis, **fields: str) -> str:
    token = secrets.token_urlsafe(32)
    key = flow_key(token)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, mapping=fields)
        pipe.expire(key, FLOW_TTL)
        await pipe.execute()
    return token


def random_dm_code() -> str:
    return str(secrets.randbelow(10 ** DM_CODE_LENGTH)).zfill(DM_CODE_LENGTH)


def dm_key(code: str) -> str:
    return PREFIX + "dm-code:" + digest("danmaku", code)


def resend_key(kind: str, identity: str) -> str:
    return PREFIX + "dm-resend:" + digest(kind, identity)


async def issue_dm(redis: Redis, token: str) -> str:
    key = flow_key(token)
    kind, identity = await redis.hmget(key, "kind", "identity")
    if not kind or not identity:
        raise HTTPException(410, "验证流程已过期")
    cooldown = resend_key(kind, identity)
    for _ in range(16):
        code = random_dm_code()
        result = await redis.eval(
            """
            if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
            local wait = redis.call('PTTL', KEYS[3])
            if wait > 0 then return math.ceil(wait / 1000) end
            if redis.call('HGET', KEYS[1], 'code') == ARGV[1] then return -1 end
            if not redis.call('SET', KEYS[2], KEYS[1], 'NX', 'EX', ARGV[2]) then return -1 end
            redis.call('SET', KEYS[3], '1', 'EX', ARGV[3])
            redis.call('HSET', KEYS[1], 'code', ARGV[1], 'proof', KEYS[2], 'verified', '0')
            redis.call('HDEL', KEYS[1], 'uid', 'username')
            redis.call('EXPIRE', KEYS[1], ARGV[4])
            return 0
            """,
            3,
            key,
            dm_key(code),
            cooldown,
            code,
            CODE_TTL,
            RESEND_SECONDS,
            FLOW_TTL,
        )
        if result == 0:
            return code
        if result == -2:
            raise HTTPException(410, "验证流程已过期")
        if result > 0:
            raise HTTPException(
                429, "请等待后再生成弹幕验证码", headers={"Retry-After": str(result)}
            )
        # A different flow reserved this candidate. Its reservation and ours are untouched.
    raise HTTPException(503, "暂时无法生成验证码，请重试")


async def dm_status(redis: Redis, token: str) -> dict:
    key = flow_key(token)
    # Read the match and its TTLs together, including during a concurrent rotation.
    result = await redis.eval(
        """
        if redis.call('EXISTS', KEYS[1]) == 0 then return {} end
        local v = redis.call('HMGET', KEYS[1], 'code', 'verified', 'uid', 'username', 'proof', 'kind', 'identity')
        local ttl = v[1] and redis.call('GET', v[5]) == KEYS[1] and redis.call('PTTL', v[5]) or 0
        return {v[1] or '', v[2] or '', v[3] or '', v[4] or '', math.max(ttl, 0), v[6], v[7]}
        """,
        1,
        key,
    )
    if not result:
        raise HTTPException(410, "验证流程已过期")
    code, matched, uid, username, ttl_ms, kind, identity = result
    verified = bool(code and matched == "1")
    if not verified and not DM_CODE_PATTERN.fullmatch(code):
        ttl_ms = 0
    remaining = max(0, await redis.pttl(resend_key(kind, identity)))
    return {
        "issued": bool(code),
        "code": code if ttl_ms or verified else None,
        "verified": verified,
        "uid": uid if verified else None,
        "username": username if verified else None,
        "expires_in": (ttl_ms + 999) // 1000,
        "retry_after": (remaining + 999) // 1000,
    }


async def accept_dm(redis: Redis, room: int, uid: str, username: str, message: str):
    if (
        room != ROOM
        or not re.fullmatch(r"[1-9][0-9]{0,19}", uid)
        or not DM_CODE_PATTERN.fullmatch(message)
    ):
        return False
    # Keep the public code reserved until its original expiry, including after a match or rotation.
    return bool(
        await redis.eval(
            """
            local flow = redis.call('GET', KEYS[1])
            if not flow or redis.call('HGET', flow, 'proof') ~= KEYS[1]
                or redis.call('HGET', flow, 'verified') ~= '0' then return 0 end
            redis.call('HSET', flow, 'uid', ARGV[1], 'username', ARGV[2], 'verified', '1')
            return 1
            """,
            1,
            dm_key(message),
            uid,
            username,
        )
    )


async def verified_flow(
    redis: Redis, token: str, kind: str, owner: str, uid: str
) -> dict:
    record = await redis.hgetall(flow_key(token))
    if not record:
        raise HTTPException(409, "本次 UID 验证已失效")
    if (
        not record.get("code")
        or record.get("verified") != "1"
        or record.get("uid") != uid
        or record.get("kind") != kind
        or record.get("owner") != owner
    ):
        raise HTTPException(409, "验证结果已更新，请重新核对 UID")
    return record


async def consume_flow(redis: Redis, token: str, proof: str, uid: str):
    consumed = await redis.eval(
        """
        if redis.call('HGET', KEYS[1], 'proof') ~= ARGV[1]
           or redis.call('HGET', KEYS[1], 'uid') ~= ARGV[2] then return 0 end
        return redis.call('DEL', KEYS[1])
        """,
        1,
        flow_key(token),
        proof,
        uid,
    )
    if not consumed:
        raise HTTPException(409, "验证结果已更新，请重新核对 UID")
