import secrets
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import func, select, update

from app.core.rate_limit import client_ip
from app.deps.member import (
    GiftDB,
    LimitDB,
    ReadDB,
    ReadingMember,
    RedisDB,
    SessionDB,
    SignedIn,
    invalidate,
    session_key,
    start_session,
)
from app.models.member_gifts import Appeal, Binding, GiftMonth, GiftOrder, Member
from app.schemas import member as form
from app.services import member_security as sec
from app.services.member_cache import read_account, read_view
from app.services.member_gifts import (
    audit,
    can_edit_order,
    get_order,
    lock_system,
    now_cn,
)

router = APIRouter(dependencies=[Depends(sec.require_origin)])


async def check_password(member: Member, password: str):
    if not await sec.password_ok(member.password_hash, password):
        raise HTTPException(403, "当前密码错误")


def identity(member):
    return f"{member.id}:{member.session_version}"


@router.post("/user-auth/email-code")
async def email_code(
    body: form.EmailRequest,
    request: Request,
    redis: RedisDB,
    limits: LimitDB,
    db: ReadDB,
):
    await sec.reserve_email_send(limits, body.email, "ip", client_ip(request))
    purpose = body.purpose
    if purpose == "reset":
        member = await read_account(db, identifier=body.email)
        if not member:
            return {"ok": True, "retry_after": sec.RESEND_SECONDS}
        purpose = "reset:" + identity(member)
    await sec.send_code(redis, body.email, purpose, action=body.purpose)
    return {"ok": True, "retry_after": sec.RESEND_SECONDS}


@router.post("/user-auth/registration/email-verify")
async def verify_email(body: form.EmailVerification, redis: RedisDB, db: ReadDB):
    await sec.verify_code(redis, body.email, "register", body.code)
    if await read_account(db, identifier=body.email):
        raise HTTPException(409, "邮箱已有账号，请登录或找回密码")
    return {
        "flow": await sec.new_flow(
            redis, kind="register", owner="", identity=body.email
        )
    }


@router.post("/user-auth/uid-challenge")
async def uid_challenge(
    body: form.Flow, request: Request, redis: RedisDB, limits: LimitDB
):
    await sec.limit(limits, "dm-ip", client_ip(request), 45, 3600)
    await sec.issue_dm(redis, body.flow)
    return await verification_status(body, redis)


@router.get("/user-auth/listener-status")
async def listener_status():
    from app.services.bili_captain_listener import verification_listener_status

    return {"room": sec.ROOM, "listener": verification_listener_status()}


@router.post(sec.VERIFICATION_STATUS_PATH)
async def verification_status(body: Annotated[form.Flow | form.Token, Body()], redis: RedisDB):
    from app.services.bili_captain_listener import verification_listener_status

    return {
        **await sec.dm_status(redis, body if isinstance(body, str) else body.flow),
        "room": sec.ROOM,
        "listener": verification_listener_status(),
    }


async def ensure_free_uid(db, uid):
    if await db.get(Binding, uid):
        raise HTTPException(409, "UID 已绑定，请使用原账号解绑或提交申诉")


def save_binding(db, member, uid, record):
    db.add(
        Binding(uid=uid, member_id=member.id, username=record.get("username") or None)
    )
    audit(db, str(member.id), "bind", uid)


@router.post("/user-auth/register")
async def register(
    body: form.Registration,
    request: Request,
    response: Response,
    db: GiftDB,
    sessions: SessionDB,
    redis: RedisDB,
):
    if body.password != body.confirm_password:
        raise HTTPException(400, "两次输入的密码不一致")
    uid = body.uid
    record = await sec.verified_flow(redis, body.flow, "register", "", uid)
    email = record["identity"]
    if await db.scalar(select(Member.id).where(Member.email == email)):
        raise HTTPException(409, "邮箱已注册")
    await ensure_free_uid(db, uid)
    member = Member(
        email=email,
        password_hash=await sec.new_password_hash(body.password),
    )
    db.add(member)
    await db.flush()
    save_binding(db, member, uid, record)
    await db.flush()
    await sec.consume_flow(redis, body.flow, record["proof"], uid)
    await db.commit()
    await start_session(request, response, sessions, member)
    return {"ok": True}


@router.post("/user-auth/login")
async def login(
    body: form.Login,
    request: Request,
    response: Response,
    sessions: SessionDB,
    limits: LimitDB,
    db: ReadDB,
):
    identifier = body.identifier.strip().lower()
    await sec.limit(limits, "login-ip", client_ip(request), 60, 600)
    await sec.limit(limits, "login-identity", identifier, 20, 600)
    member = await read_account(db, identifier=identifier)
    if not member or not await sec.password_ok(member.password_hash, body.password):
        raise HTTPException(401, "账号或密码错误")
    if member.password_hash.startswith("$argon2"):
        replacement = await sec.new_password_hash(body.password)
        await lock_system(db)
        current = await db.get(Member, member.id)
        if current and current.password_hash == member.password_hash:
            current.password_hash = replacement
        await db.commit()
    # Recheck the login alias and account after hashing outside the database transaction.
    current = await read_account(db, identifier=identifier)
    if (
        not current
        or current.id != member.id
        or current.session_version != member.session_version
    ):
        raise HTTPException(401, "账号或密码错误")
    await start_session(request, response, sessions, current)
    return {"ok": True}


@router.post("/user-auth/logout")
async def logout(request: Request, response: Response, redis: SessionDB):
    if token := request.cookies.get(sec.SESSION_COOKIE):
        await redis.delete(session_key(token))
    response.delete_cookie(sec.SESSION_COOKIE)
    return {"ok": True}


@router.post("/user-auth/password-reset")
async def reset(body: form.PasswordReset, redis: RedisDB, db: GiftDB):
    member = await db.scalar(select(Member).where(Member.email == body.email))
    if not member:
        raise HTTPException(400, "验证码错误或已失效")
    await sec.verify_code(redis, body.email, "reset:" + identity(member), body.code)
    member.password_hash = await sec.new_password_hash(body.password)
    invalidate(member)
    audit(db, str(member.id), "password_reset", str(member.id))
    return {"ok": True}


@router.get("/me/account")
async def account_identity(member: ReadingMember):
    return {"email": member.email}


@router.get("/me")
async def me(member: ReadingMember, db: ReadDB):
    view = await read_view(db, member)
    addresses = view["binding_addresses"]
    return {
        "email": member.email,
        "address": view["address"],
        "auto_fill_address": view["auto_fill_address"],
        "bindings": [
            {**b, "address": addresses.get(b["uid"])} for b in member.bindings
        ],
        "gifts": view["gifts"],
    }


@router.put("/me/address-preference")
async def address_preference(
    body: form.AddressPreference, member: SignedIn, db: GiftDB
):
    member.auto_fill_address = body.auto_fill_address
    audit(
        db,
        str(member.id),
        "address_preference",
        str(member.id),
        enabled=body.auto_fill_address,
    )
    return {"ok": True}


@router.put("/me/address")
@router.put("/me/address/{uid}")
async def address(
    member: SignedIn,
    db: GiftDB,
    body: form.Address | None = None,
    uid: form.UID | None = None,
):
    target = member if uid is None else await db.get(Binding, uid)
    if uid and (not target or target.member_id != member.id):
        raise HTTPException(404, "未绑定此 UID")
    target.address = sec.seal(body.model_dump()) if body else None
    audit(db, str(member.id), "address", uid or str(member.id))
    return {"ok": True}


@router.put("/me/gifts/{month}/{uid}/address")
async def edit_gift_address(
    month: str,
    uid: form.UID,
    body: form.GiftAddress | form.UseDefaultAddress,
    member: SignedIn,
    db: GiftDB,
):
    row = await get_order(db, month, uid)
    binding = await db.scalar(
        select(Binding).where(Binding.uid == uid, Binding.member_id == member.id)
    )
    owner = row.owner_id or (binding.member_id if binding else None)
    if owner != member.id:
        raise HTTPException(404, "未找到此资格")
    batch = await db.get(GiftMonth, (sec.ROOM, month))
    if not can_edit_order(row, batch, now_cn()):
        raise HTTPException(409, "这份舰礼的地址修改已截止")
    if row.refill_requested:
        if not isinstance(body, form.GiftAddress) or not body.confirm_refill:
            raise HTTPException(409, "地址已打回，请重新打开并保存修改")
        row.address = sec.seal(body.model_dump(exclude={"confirm_refill"}))
        row.returned_address = None
        audit(db, str(member.id), "refill_draft", f"{month}/{uid}")
        return {"ok": True}
    if isinstance(body, form.UseDefaultAddress):
        if not member.address:
            raise HTTPException(409, "请先保存默认收件地址")
        row.address = member.address
    else:
        row.address = sec.seal(body.model_dump(exclude={"confirm_refill"}))
        if member.auto_fill_address:
            count = await db.scalar(
                select(func.count())
                .select_from(Binding)
                .where(Binding.member_id == member.id)
            )
            target = binding if binding and (count > 1 or binding.address) else member
            target.address = row.address
    audit(db, str(member.id), "address", f"{month}/{uid}")
    return {"ok": True}


@router.post("/me/binding-flow")
async def binding_flow(member: ReadingMember, redis: RedisDB):
    return {
        "flow": await sec.new_flow(
            redis, kind="bind", owner=identity(member), identity=str(member.id)
        )
    }


@router.post("/me/bind")
async def bind(body: form.ConfirmUID, member: SignedIn, redis: RedisDB, db: GiftDB):
    uid = body.uid
    record = await sec.verified_flow(redis, body.flow, "bind", identity(member), uid)
    await ensure_free_uid(db, uid)
    save_binding(db, member, uid, record)
    await db.flush()
    await sec.consume_flow(redis, body.flow, record["proof"], uid)
    return {"ok": True}


async def release_binding(db, binding, actor):
    await db.execute(
        update(GiftOrder)
        .where(GiftOrder.uid == binding.uid, GiftOrder.owner_id.is_(None))
        .values(waived_by=None, address=None, returned_address=None)
    )
    audit(db, actor, "unbind", binding.uid, member_id=binding.member_id)
    await db.delete(binding)


@router.post("/me/unbind/{uid}")
async def unbind(
    uid: form.UID, body: form.ConfirmPassword, member: SignedIn, db: GiftDB
):
    await check_password(member, body.password)
    binding = await db.get(Binding, uid)
    if not binding or binding.member_id != member.id:
        raise HTTPException(404, "未绑定此 UID")
    await release_binding(db, binding, str(member.id))
    return {"ok": True}


@router.post("/me/password")
async def password(
    body: form.ChangePassword, member: SignedIn, db: GiftDB, response: Response
):
    await check_password(member, body.password)
    member.password_hash = await sec.new_password_hash(body.new_password)
    invalidate(member)
    response.delete_cookie(sec.SESSION_COOKIE)
    audit(db, str(member.id), "password_change", str(member.id))
    return {"ok": True}


@router.post("/me/email-code")
async def send_old_email_code(
    body: form.Body,
    member: ReadingMember,
    request: Request,
    redis: RedisDB,
    limits: LimitDB,
):
    scope = sec.account_proof_scope(member, request)
    await sec.reserve_email_send(limits, member.email, "account", str(member.id))
    await sec.send_code(redis, member.email, "change-old:" + scope, action="change_old")
    return {"retry_after": sec.RESEND_SECONDS}


@router.post("/me/email/verify-old")
async def verify_old_email(
    body: form.MailCode, member: ReadingMember, request: Request, redis: RedisDB
):
    scope = sec.account_proof_scope(member, request)
    await sec.verify_code(redis, member.email, "change-old:" + scope, body.code)
    return {"flow": await sec.start_email_change(redis, scope)}


@router.post("/me/email/new-code")
async def send_new_email_code(
    body: form.NewEmail,
    member: ReadingMember,
    request: Request,
    redis: RedisDB,
    limits: LimitDB,
    db: ReadDB,
):
    scope = sec.account_proof_scope(member, request)
    await sec.check_email_change(redis, scope, body.flow, body.email, choosing=True)
    if await read_account(db, identifier=body.email):
        raise HTTPException(409, "新邮箱不可用")
    await sec.authorize_new_email(redis, scope, body.flow, body.email)
    await sec.reserve_email_send(limits, body.email, "account", str(member.id))
    await sec.send_code(
        redis, body.email, "change-new:" + scope + ":" + body.flow, action="change_new"
    )
    return {"retry_after": sec.RESEND_SECONDS}


@router.post("/me/email")
async def change_email(
    body: form.ChangeEmail,
    member: SignedIn,
    request: Request,
    redis: RedisDB,
    db: GiftDB,
    response: Response,
):
    scope = sec.account_proof_scope(member, request)
    await sec.check_email_change(redis, scope, body.flow, body.email)
    if await db.scalar(select(Member.id).where(Member.email == body.email)):
        raise HTTPException(409, "新邮箱不可用")
    await sec.verify_code(
        redis, body.email, "change-new:" + scope + ":" + body.flow, body.code
    )
    member.email = body.email
    invalidate(member)
    response.delete_cookie(sec.SESSION_COOKIE)
    audit(db, str(member.id), "email_change", str(member.id))
    return {"ok": True}


@router.post("/me/deletion-code")
async def send_deletion_code(
    member: ReadingMember, request: Request, redis: RedisDB, limits: LimitDB
):
    scope = sec.account_proof_scope(member, request)
    await sec.reserve_email_send(limits, member.email, "account", str(member.id))
    await sec.send_code(redis, member.email, "deletion:" + scope, action="deletion")
    return {"retry_after": sec.RESEND_SECONDS}


@router.post("/me/deletion")
async def deletion(
    body: form.Deletion,
    member: SignedIn,
    request: Request,
    response: Response,
    redis: RedisDB,
    db: GiftDB,
):
    await sec.verify_code(
        redis,
        member.email,
        "deletion:" + sec.account_proof_scope(member, request),
        body.code,
    )
    for b in list(
        await db.scalars(select(Binding).where(Binding.member_id == member.id))
    ):
        await release_binding(db, b, str(member.id))
    member.email = member.password_hash = member.address = None
    invalidate(member)
    response.delete_cookie(sec.SESSION_COOKIE)
    audit(db, str(member.id), "delete_account", str(member.id))
    return {"ok": True}


@router.post("/me/gifts/{month}/{uid}")
async def decide(
    month: str, uid: form.UID, body: form.Decision, member: SignedIn, db: GiftDB
):
    row = await get_order(db, month, uid)
    binding = await db.get(Binding, uid)
    if (row.owner_id or (binding.member_id if binding else None)) != member.id:
        raise HTTPException(404, "未找到此资格")
    batch = await db.get(GiftMonth, (sec.ROOM, month))
    if not can_edit_order(row, batch, now_cn()):
        raise HTTPException(409, "当前不能修改此份舰礼")
    row.waived_by = member.id if body.waive else None
    audit(db, str(member.id), "waive" if body.waive else "restore", f"{month}/{uid}")
    return {"ok": True}


@router.post("/user-auth/appeals")
async def appeal(body: form.AppealInput, request: Request, limits: LimitDB, db: GiftDB):
    await sec.limit(limits, "appeal-ip", client_ip(request), 5, 3600)
    ticket = secrets.token_urlsafe(32)
    db.add(
        Appeal(
            ticket=sec.digest("appeal", ticket),
            contact=body.contact,
            uid=body.uid,
            message=body.message,
            created_at=now_cn(),
        )
    )
    return {"ticket": ticket}


class AppealTicket(form.Body):
    ticket: form.Token


@router.post("/user-auth/appeals/status")
async def appeal_status(body: AppealTicket, db: ReadDB):
    item = await db.scalar(
        select(Appeal).where(Appeal.ticket == sec.digest("appeal", body.ticket))
    )
    if not item:
        raise HTTPException(404, "未找到该申诉")
    return {"closed": item.closed, "reply": item.reply}
