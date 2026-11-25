import asyncio
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from pydantic import Field, StringConstraints
from sqlalchemy import or_, select

from app.api.member import release_binding
from app.core.config import get_settings
from app.deps.auth import Principal, require_admin
from app.deps.member import GiftDB, invalidate
from app.models.captain_gift_archive import CaptainGiftArchive
from app.models.member_gifts import (
    Appeal,
    Binding,
    GiftAudit,
    GiftMonth,
    GiftOrder,
    GiftSystem,
    Member,
)
from app.schemas.member import UID, Body, Email
from app.services import gift_workbook as sheets
from app.services.gift_mail import send_roster
from app.services.member_gifts import (
    TIERS,
    active_collection_summary,
    active_frozen_recipient,
    admin_record_page,
    roster_snapshot,
    audit,
    collection_schedule,
    get_order,
    midnight_after,
    now_cn,
    order_address,
    orders_with_members_query,
    read_page,
    return_deadline,
    set_deadline,
)
from app.services.member_security import ROOM

Admin = Annotated[Principal, Depends(require_admin)]
Month = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}(0[1-9]|1[0-2])$")]
PageNumber = Annotated[int, Query(ge=1)]
router = APIRouter(prefix="/admin/member-gifts", dependencies=[Depends(require_admin)])


class Deadline(Body):
    date: date


class Extension(Deadline):
    uid: UID | None = None


class Correction(Body):
    tier: Literal[1, 2, 3]
    reason: str = Field(min_length=2, max_length=300)


class Resolution(Body):
    reply: str = Field(min_length=1, max_length=1000)
    action: Literal["reply", "release_uid", "recover_email"] = "reply"
    old_email: Email | None = None


@router.get("/summary")
async def dashboard_summary(db: GiftDB):
    now = now_cn()
    month = now.strftime("%Y%m")
    uploaded = await db.scalar(
        select(
            select(CaptainGiftArchive.id)
            .where(CaptainGiftArchive.gift_month == month)
            .exists()
        )
    )
    return {
        "archive": {"month": month, "uploaded": uploaded},
        "collection": await active_collection_summary(db, now),
    }


@router.get("/months")
async def months(db: GiftDB):
    batches = await db.scalars(
        select(GiftMonth)
        .where(GiftMonth.room_id == ROOM)
        .order_by(GiftMonth.month.desc())
    )
    return {
        "first_month": (await db.get(GiftSystem, 1)).first_month,
        "items": [
            {
                "month": b.month,
                "settled": b.settled,
                "deadline": b.deadline,
                "reminded": b.reminded,
            }
            for b in batches
        ],
    }


@router.put("/months/{month}")
async def deadline(month: Month, body: Deadline, db: GiftDB, admin: Admin):
    await set_deadline(db, month, body.date, admin.subject)
    return {"ok": True}


@router.get("/months/{month}")
async def records(
    month: Month,
    db: GiftDB,
    page: PageNumber = 1,
    kind: Literal["all", "missing", "ready", "reship"] = "all",
    q: Annotated[str, Query(max_length=255)] = "",
):
    last_export = await db.scalar(
        select(GiftAudit)
        .where(
            GiftAudit.action == "export",
            GiftAudit.subject == month,
            GiftAudit.details["kind"].as_string().in_(("all", "ready")),
        )
        .order_by(GiftAudit.id.desc())
        .limit(1)
    )
    changed_since_export = False
    if last_export:
        batch = await db.get(GiftMonth, (ROOM, month))
        expected = last_export.details.get("roster_signature")
        if expected and batch:
            current, _ = await roster_snapshot(db, batch)
            changed_since_export = current != expected
        else:
            # The contents of an attachment sent by an older release cannot be compared.
            changed_since_export = True
    return {
        **await admin_record_page(db, month, kind, q.strip(), page),
        "roster_recipient": get_settings().gift_roster_recipient,
        "needs_resend": changed_since_export,
    }


@router.post("/months/{month}/email")
async def email_roster(month: Month, body: Body, db: GiftDB, admin: Admin):
    batch = await db.get(GiftMonth, (ROOM, month))
    if not batch or not batch.settled:
        raise HTTPException(409, "当月资格名单尚未结算")
    signature, rows = await roster_snapshot(db, batch, for_email=True)
    settings = get_settings()
    delivery = audit(
        db,
        admin.subject,
        "export_pending",
        month,
        kind="all",
        count=len(rows),
        recipient=settings.gift_roster_recipient,
        roster_signature=signature,
    )
    # Keep the exact attachment signature while SMTP runs outside the fulfillment lock.
    await db.commit()
    try:
        attachment = await asyncio.to_thread(sheets.roster_workbook, rows)
        await asyncio.to_thread(send_roster, month, attachment, settings=settings)
    except Exception as exc:
        delivery.action = "export_failed"
        await db.commit()
        raise HTTPException(502, "名单发送失败，请稍后重试") from exc
    delivery.action = "export"
    return {"recipient": settings.gift_roster_recipient, "count": len(rows)}


async def require_closed(month, db):
    schedule = await collection_schedule(db, month)
    if not schedule["settled"] or schedule["phase"] != "closed":
        raise HTTPException(409, "当月收集和全部补填期结束后才可处理单号")


def shippable_orders(month):
    return select(GiftOrder).where(
        GiftOrder.room_id == ROOM,
        GiftOrder.month == month,
        GiftOrder.state.in_(("ready", "shipped")),
        GiftOrder.address.is_not(None),
    )


@router.get("/months/{month}/tracking-template")
async def tracking_template(month: Month, db: GiftDB):
    await require_closed(month, db)
    orders = (
        await db.execute(
            shippable_orders(month)
            .with_only_columns(GiftOrder.uid, GiftOrder.nickname, GiftOrder.tracking)
            .order_by(GiftOrder.uid)
        )
    ).all()
    contents = await asyncio.to_thread(sheets.tracking_workbook, month, orders)
    return Response(
        contents,
        media_type=sheets.XLSX_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="tracking_{month}.xlsx"',
            "Cache-Control": "private, no-store",
        },
    )


async def read_tracking_upload(month: Month, file: Annotated[UploadFile, File()]):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(422, "请上传 .xlsx 单号模板")
    contents = await file.read(sheets.MAX_UPLOAD_BYTES + 1)
    if len(contents) > sheets.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "单号表格超过 5 MB，请分批上传")
    try:
        return await asyncio.to_thread(sheets.parse_tracking, contents, month)
    except sheets.WorkbookError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/months/{month}/tracking-import")
async def import_tracking(
    month: Month,
    entries: Annotated[list[sheets.TrackingRow], Depends(read_tracking_upload)],
    db: GiftDB,
    admin: Admin,
):
    await require_closed(month, db)
    orders = {row.uid: row for row in await db.scalars(shippable_orders(month))}
    changes = []
    for entry in entries:
        row = orders.get(entry.uid)
        if row is None:
            raise HTTPException(422, f"第 {entry.number} 行 UID 不属于本月可寄送名单")
        if entry.tracking == entry.original or entry.tracking == row.tracking:
            continue
        if row.tracking != entry.original:
            raise HTTPException(
                409, f"第 {entry.number} 行原单号已变动，请重新下载模板"
            )
        changes.append((row, entry.tracking))
    for row, tracking in changes:
        audit(
            db,
            admin.subject,
            "reship" if row.needs_reship and tracking else "tracking",
            f"{month}/{row.uid}",
            old=row.tracking,
            new=tracking,
        )
        if tracking:
            row.needs_reship = False
        row.tracking = tracking
        row.state = "shipped" if tracking else "ready"
    return {"changed": len(changes), "unchanged": len(entries) - len(changes)}


@router.post("/months/{month}/extend")
async def extend(month: Month, body: Extension, db: GiftDB, admin: Admin):
    now = now_cn()
    boundary = midnight_after(body.date)
    if boundary <= now:
        raise HTTPException(400, "宽限期须延长至未来")
    batch = await db.get(GiftMonth, (ROOM, month))
    if not batch or not batch.deadline or now < batch.deadline:
        raise HTTPException(409, "正式截止后才可开放延期")
    query = select(GiftOrder).where(
        GiftOrder.room_id == ROOM,
        GiftOrder.month == month,
        GiftOrder.state.in_(("ready", "grace", "overdue")),
        or_(GiftOrder.state != "ready", active_frozen_recipient()),
        or_(GiftOrder.extension_until.is_(None), GiftOrder.extension_until < boundary),
        or_(GiftOrder.state == "ready", GiftOrder.grace_until < boundary),
    )
    if body.uid:
        query = query.where(GiftOrder.uid == body.uid)
    rows = list(await db.scalars(query))
    if not rows:
        raise HTTPException(409, "没有可按此日期延期的记录")
    for row in rows:
        row.extension_until = boundary
        if row.state != "ready":
            row.grace_until = boundary
            row.state = "grace"
    audit(
        db,
        admin.subject,
        "extend",
        f"{month}/{body.uid or '*'}",
        until=boundary.isoformat(),
        count=len(rows),
        ready_count=sum(row.state == "ready" for row in rows),
    )
    return {"count": len(rows)}


@router.post("/months/{month}/{uid}/return-address")
async def return_address(month: Month, uid: UID, db: GiftDB, admin: Admin):
    result = (
        await db.execute(orders_with_members_query(month).where(GiftOrder.uid == uid))
    ).one_or_none()
    if result is None:
        raise HTTPException(404, "未找到该月资格")
    row, binding, recipient = result
    address = order_address(row, binding, recipient)
    until = return_deadline(
        row, recipient, address, await collection_schedule(db, month)
    )
    if until is None:
        raise HTTPException(409, "仅可在当月补填期内打回已填写且尚未发货的地址")
    # Keep the original owner of a frozen parcel. Only this month's address is reopened.
    row.refill_requested = True
    row.returned_address = address
    row.address = row.frozen_at = None
    row.state = "grace"
    row.grace_until = until
    audit(
        db,
        admin.subject,
        "return_address",
        f"{month}/{uid}",
        until=row.grace_until.isoformat(),
    )
    return {"ok": True}


@router.put("/months/{month}/{uid}/tier")
async def tier(month: Month, uid: UID, body: Correction, db: GiftDB, admin: Admin):
    row = await get_order(db, month, uid)
    if row.state == "shipped":
        raise HTTPException(409, "已发货，请核对寄送记录")
    audit(
        db,
        admin.subject,
        "tier",
        f"{month}/{uid}",
        old=TIERS[row.level],
        new=TIERS[body.tier],
        reason=body.reason,
    )
    row.level, row.manual_level = body.tier, True
    return {"ok": True}


@router.get("/appeals")
async def appeals(
    db: GiftDB,
    page: PageNumber = 1,
    status: Literal["open", "closed", "all"] | None = None,
    closed: bool = False,
):
    status = status or ("closed" if closed else "open")
    query = select(Appeal).order_by(Appeal.id.desc())
    if status != "all":
        query = query.where(Appeal.closed == (status == "closed"))
    rows, pagination = await read_page(db, query, page, 5)
    return {
        **pagination,
        "items": [
            {
                "id": a.id,
                "contact": a.contact,
                "uid": a.uid,
                "message": a.message,
                "reply": a.reply,
                "closed": a.closed,
                "created_at": a.created_at,
            }
            for (a,) in rows
        ],
    }


@router.post("/appeals/{appeal_id}")
async def resolve(appeal_id: int, body: Resolution, db: GiftDB, admin: Admin):
    item = await db.get(Appeal, appeal_id)
    if not item or item.closed:
        raise HTTPException(409, "未找到待处理申诉")
    if body.action == "release_uid":
        binding = await db.get(Binding, item.uid) if item.uid else None
        if not binding:
            raise HTTPException(409, "此 UID 尚未绑定")
        await release_binding(db, binding, admin.subject)
    elif body.action == "recover_email":
        member = (
            await db.scalar(select(Member).where(Member.email == body.old_email))
            if body.old_email
            else None
        )
        if not member or await db.scalar(
            select(Member.id).where(Member.email == item.contact)
        ):
            raise HTTPException(409, "原账号不存在或联系邮箱已被使用")
        member.email = item.contact
        invalidate(member)
    item.reply, item.closed = body.reply, True
    audit(db, admin.subject, "appeal", str(appeal_id), resolution=body.action)
    return {"ok": True}


# Historical JSON details can contain address snapshots. Only expose the documented
# metadata for each action, even when the database contains an older record.
EXPORT_DETAILS = ("kind", "count", "recipient")
AUDIT_DETAILS = {
    "address_preference": ("enabled",),
    "unbind": ("member_id",),
    "deadline": ("deadline",),
    "extend": ("until", "count", "ready_count"),
    "return_address": ("until",),
    "tracking": ("old", "new"),
    "reship": ("old", "new"),
    "qualification": ("new", "source"),
    "auto_upgrade": ("old", "new", "source", "needs_reship"),
    "tier": ("old", "new", "reason"),
    "appeal": ("resolution",),
    "export_pending": EXPORT_DETAILS,
    "export": EXPORT_DETAILS,
    "export_failed": EXPORT_DETAILS,
}


def visible_audit_details(entry):
    details = entry.details or {}
    return {
        key: details[key]
        for key in AUDIT_DETAILS.get(entry.action, ())
        if key in details and isinstance(details[key], (str, int, bool, type(None)))
    }


@router.get("/audit")
async def audit_log(db: GiftDB, month: Month | None = None, page: PageNumber = 1):
    query = select(GiftAudit).order_by(GiftAudit.id.desc())
    if month:
        query = query.where(
            or_(GiftAudit.subject == month, GiftAudit.subject.startswith(month + "/"))
        )
    rows, pagination = await read_page(db, query, page, 50)
    return {
        **pagination,
        "items": [
            {
                "id": e.id,
                "at": e.created_at,
                "actor": e.actor,
                "action": e.action,
                "subject": e.subject,
                "details": visible_audit_details(e),
            }
            for (e,) in rows
        ],
    }
