import hmac
import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import DateTime, case, cast, delete, func, or_, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer, load_only

from app.core.config import get_settings
from app.services.gift_workbook import roster_columns
from app.models.member_gifts import (
    Binding,
    GiftAudit,
    GiftMonth,
    GiftNotice,
    GiftOrder,
    GiftSystem,
    Member,
    PurchaseLevel,
)
from app.services.member_security import ROOM, reveal

TIERS = {1: "总督", 2: "提督", 3: "舰长"}
RANK = {name: rank for rank, name in TIERS.items()}
DEFAULT_GRACE = timedelta(hours=72)


def now_cn() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def midnight_after(day: date) -> datetime:
    return datetime.combine(day + timedelta(days=1), time.min)


def audit(db, actor: str, action: str, subject: str, **details):
    entry = GiftAudit(
        created_at=now_cn(),
        actor=actor,
        action=action,
        subject=subject,
        details=details,
    )
    db.add(entry)
    return entry


async def lock_system(db: AsyncSession) -> GiftSystem:
    row = await db.scalar(
        select(GiftSystem).where(GiftSystem.id == 1).with_for_update()
    )
    if row is None:
        raise HTTPException(503, "舰礼数据库尚未初始化")
    return row


def order_binding():
    return (Binding.uid == GiftOrder.uid) & (
        GiftOrder.owner_id.is_(None) | (Binding.member_id == GiftOrder.owner_id)
    )


def orders_with_members_query(month: str):
    return (
        select(GiftOrder, Binding, Member)
        .outerjoin(Binding, order_binding())
        .outerjoin(
            Member, Member.id == func.coalesce(GiftOrder.owner_id, Binding.member_id)
        )
        .where(GiftOrder.room_id == ROOM, GiftOrder.month == month)
        .order_by(GiftOrder.uid)
    )


async def orders_with_members(db, month: str):
    return (await db.execute(orders_with_members_query(month))).all()


def order_address(row, binding, member):
    if row.state == "overdue" or (row.state == "grace" and row.refill_requested):
        return None
    if row.frozen_at or row.refill_requested or row.address:
        return row.address
    if binding and member and member.auto_fill_address:
        return binding.address or member.address
    return None


def has_order_address():
    # Follow the same address precedence as order_address without reading the addresses.
    return case(
        (
            (GiftOrder.state == "overdue")
            | ((GiftOrder.state == "grace") & GiftOrder.refill_requested),
            False,
        ),
        (
            GiftOrder.frozen_at.is_not(None) | GiftOrder.refill_requested,
            GiftOrder.address.is_not(None),
        ),
        else_=or_(
            GiftOrder.address.is_not(None),
            Member.auto_fill_address.is_(True)
            & or_(Binding.address.is_not(None), Member.address.is_not(None)),
        ),
    )


def eligible_for_shipping():
    return (GiftOrder.state != "waived") & (
        GiftOrder.waived_by.is_(None)
        | GiftOrder.waived_by.is_distinct_from(
            func.coalesce(GiftOrder.owner_id, Binding.member_id)
        )
    )


def active_frozen_recipient():
    return (
        select(Member.id)
        .where(Member.id == GiftOrder.owner_id, Member.email.is_not(None))
        .exists()
    )


def collection_window_end():
    return case(
        (GiftOrder.state == "grace", GiftOrder.grace_until),
        (
            (GiftOrder.state == "ready") & active_frozen_recipient(),
            GiftOrder.extension_until,
        ),
    )


def month_has_orders():
    return (
        select(GiftOrder.uid)
        .where(
            GiftOrder.room_id == GiftMonth.room_id, GiftOrder.month == GiftMonth.month
        )
        .exists()
    )


async def active_collection_summary(db: AsyncSession, now: datetime):
    open_grace = (
        select(GiftOrder.uid)
        .where(
            GiftOrder.room_id == GiftMonth.room_id,
            GiftOrder.month == GiftMonth.month,
            collection_window_end() > now,
        )
        .exists()
    )
    month = await db.scalar(
        select(GiftMonth.month)
        .where(
            GiftMonth.room_id == ROOM,
            or_(
                month_has_orders(),
                GiftMonth.settled.is_(True) & GiftMonth.deadline.is_not(None),
            ),
            or_(GiftMonth.deadline.is_(None), GiftMonth.deadline > now, open_grace),
        )
        .order_by(GiftMonth.month.desc())
        .limit(1)
    )
    if month is None:
        return None

    total, collected = (
        await db.execute(
            select(func.count(), func.count(case((has_order_address(), 1))))
            .select_from(GiftOrder)
            .outerjoin(Binding, order_binding())
            .outerjoin(
                Member,
                Member.id == func.coalesce(GiftOrder.owner_id, Binding.member_id),
            )
            .where(
                GiftOrder.room_id == ROOM,
                GiftOrder.month == month,
                eligible_for_shipping(),
            )
        )
    ).one()
    return {"month": month, "collected": collected, "missing": total - collected}


def freeze(row, binding, member, address, when):
    if row.owner_id is None and binding:
        row.bound_username = binding.username
    row.owner_id = member.id
    row.frozen_at = when
    if row.waived_by == member.id:
        row.state = "waived"
        row.address = None
    else:
        row.state = "ready"
        row.address = address


def missing_address_reminders_query(month):
    return (
        select(Member.id, GiftOrder.uid)
        .select_from(GiftOrder)
        .join(Binding, order_binding())
        .join(Member, Member.id == Binding.member_id)
        .where(
            GiftOrder.room_id == ROOM,
            GiftOrder.month == month,
            GiftOrder.state == "open",
            GiftOrder.frozen_at.is_(None),
            GiftOrder.waived_by.is_distinct_from(Member.id),
            Member.email.is_not(None),
            Member.auto_fill_address.is_(False),
            GiftOrder.address.is_(None),
        )
        .order_by(Member.id, GiftOrder.uid)
    )


async def schedule_reminders(db, batch, now):
    already_notified = (
        select(GiftNotice.id)
        .where(
            GiftNotice.month == batch.month,
            GiftNotice.member_id == Member.id,
            or_(GiftNotice.sent.is_(True), GiftNotice.revision == batch.revision),
        )
        .exists()
    )
    recipients = defaultdict(list)
    for member_id, uid in await db.execute(
        missing_address_reminders_query(batch.month).where(~already_notified)
    ):
        recipients[member_id].append(uid)
    for member_id, uids in recipients.items():
        db.add(
            GiftNotice(
                month=batch.month,
                member_id=member_id,
                revision=batch.revision,
                kind="reminder",
                uids=uids,
                deadline=batch.deadline,
                next_try=now,
            )
        )


async def advance(db: AsyncSession, now: datetime, month: str | None = None):
    query = select(GiftMonth).where(
        GiftMonth.room_id == ROOM,
        GiftMonth.deadline <= now + timedelta(hours=48),
        or_(
            GiftMonth.reminded.is_(False),
            select(GiftOrder.uid)
            .where(
                GiftOrder.room_id == GiftMonth.room_id,
                GiftOrder.month == GiftMonth.month,
                or_(
                    (GiftOrder.state == "open") & (GiftMonth.deadline <= now),
                    (GiftOrder.state == "grace") & (GiftOrder.grace_until <= now),
                ),
            )
            .exists(),
        ),
    )
    if month is not None:
        query = query.where(GiftMonth.month == month)
    batches = (await db.scalars(query)).all()
    for batch in batches:
        if not batch.reminded:
            if now < batch.deadline:
                await schedule_reminders(db, batch, now)
            batch.reminded = True
        if now < batch.deadline:
            continue
        for row, binding, member in await orders_with_members(db, batch.month):
            address = order_address(row, binding, member)
            if row.state == "open":
                if member and (address or row.waived_by == member.id):
                    freeze(row, binding, member, address, batch.deadline)
                    continue
                row.state = "grace"
                row.grace_until = batch.deadline + DEFAULT_GRACE
            if row.state == "grace" and row.grace_until <= now:
                if row.refill_requested:
                    # Only an explicit save supplies a revision. Never ship the prefilled original.
                    address = row.address
                    row.returned_address = None
                    audit(
                        db,
                        "system",
                        "refill_address" if address and member else "refill_expired",
                        f"{row.month}/{row.uid}",
                    )
                if member and (address or row.waived_by == member.id):
                    freeze(row, binding, member, address, row.grace_until)
                else:
                    row.state = "overdue"
                    row.address = None
    await db.flush()


async def gift_month(db, month):
    batch = await db.get(GiftMonth, (ROOM, month))
    if batch is None:
        batch = GiftMonth(room_id=ROOM, month=month)
        db.add(batch)
        await db.flush()
    return batch


def record_qualification(db, month, uid, nickname, tier, source, row):
    if row is None:
        row = GiftOrder(
            room_id=ROOM,
            month=month,
            uid=uid,
            nickname=nickname,
            level=tier,
        )
        db.add(row)
        audit(db, "system", "qualification", f"{month}/{uid}", new=tier, source=source)
    elif tier < row.level:
        old = row.level
        row.level = tier
        if row.state == "shipped":
            row.needs_reship = True
        audit(
            db,
            "system",
            "auto_upgrade",
            f"{month}/{uid}",
            old=old,
            new=tier,
            source=source,
            needs_reship=bool(row.needs_reship),
        )
    if source == "roster":
        row.roster_level = min(tier, row.roster_level or tier)
        row.nickname = nickname or row.nickname
    return row


async def record_paid_levels(db, month, records):
    """Merge (UID, tier, nickname) from confirmed or explicitly grandfathered events."""
    for start in range(0, len(records), 250):
        statement = insert(PurchaseLevel).values(
            [
                {
                    "room_id": ROOM,
                    "month": month,
                    "uid": uid,
                    "level": tier,
                    "paid_level": tier,
                    "nickname": nickname,
                }
                for uid, tier, nickname in records[start : start + 250]
            ]
        )
        tier = statement.inserted.paid_level
        await db.execute(
            statement.on_duplicate_key_update(
                level=func.least(PurchaseLevel.level, tier),
                paid_level=func.least(
                    func.coalesce(PurchaseLevel.paid_level, tier), tier
                ),
                applied_level=case(
                    (PurchaseLevel.applied_level > tier, None),
                    else_=PurchaseLevel.applied_level,
                ),
                nickname=func.coalesce(
                    func.nullif(statement.inserted.nickname, ""), PurchaseLevel.nickname
                ),
            )
        )


async def month_orders_by_uid(db, month):
    return {
        row.uid: row
        for row in await db.scalars(
            select(GiftOrder).where(GiftOrder.room_id == ROOM, GiftOrder.month == month)
        )
    }


def apply_purchase(db, purchase, row, source="purchase"):
    tier = purchase.paid_level
    if tier is not None and (
        purchase.applied_level is None or tier < purchase.applied_level
    ):
        row = record_qualification(
            db,
            purchase.month,
            purchase.uid,
            purchase.nickname or "",
            tier,
            source,
            row,
        )
        purchase.applied_level = tier
    return row


async def apply_month_purchases(db, month, orders=None, *, source="purchase"):
    pending = (
        await db.scalars(
            select(PurchaseLevel)
            .where(
                PurchaseLevel.room_id == ROOM,
                PurchaseLevel.month == month,
                PurchaseLevel.paid_level.is_not(None),
                PurchaseLevel.applied_level.is_(None),
            )
            .with_for_update()
        )
    ).all()
    if not pending:
        return 0
    if orders is None:
        await gift_month(db, month)
        orders = await month_orders_by_uid(db, month)
    for purchase in pending:
        apply_purchase(db, purchase, orders.get(purchase.uid), source)
    return len(pending)


async def grant_paid_purchase(db, month, uid):
    system = await lock_system(db)
    if month < system.first_month:
        return
    purchase = await db.get(PurchaseLevel, (ROOM, month, uid), with_for_update=True)
    if (
        not purchase
        or purchase.paid_level is None
        or purchase.applied_level == purchase.paid_level
    ):
        return
    await gift_month(db, month)
    row = await db.get(GiftOrder, (ROOM, month, uid))
    apply_purchase(db, purchase, row)
    await db.flush()
    await advance(db, now_cn(), month)


async def settle(db, month: str, rows: list[dict]):
    system = await lock_system(db)
    if month < system.first_month:
        return
    batch = await gift_month(db, month)
    if batch.roster_finalized_at:
        return
    orders = await month_orders_by_uid(db, month)
    for record in rows:
        uid = record["uid"]
        orders[uid] = record_qualification(
            db,
            month,
            uid,
            record["name"],
            RANK[record["level"]],
            "roster",
            orders.get(uid),
        )
    await apply_month_purchases(db, month, orders)
    batch.settled = True
    batch.roster_finalized_at = now_cn()
    await db.flush()
    await advance(db, batch.roster_finalized_at, month)


async def set_deadline(db, month: str, day: date, actor: str):
    now = now_cn()
    boundary = midnight_after(day)
    if boundary <= now or month < (await db.get(GiftSystem, 1)).first_month:
        raise HTTPException(400, "截止日应为今天或以后，月份须在上线月及以后")
    batch = await db.get(GiftMonth, (ROOM, month))
    if not batch:
        batch = GiftMonth(room_id=ROOM, month=month)
        db.add(batch)
        await db.flush()
    if batch.deadline == boundary:
        return
    if batch.deadline and now >= batch.deadline:
        raise HTTPException(409, "正式收集已截止，请延长缺地址记录的宽限期")
    batch.deadline = boundary
    batch.revision += 1
    if batch.revision > 1:
        # A worker holds its notice row until SMTP finishes; retain successful deliveries for deduplication.
        await db.execute(
            delete(GiftNotice).where(
                GiftNotice.month == month, GiftNotice.sent.is_(False)
            )
        )
        batch.reminded = False
    audit(db, actor, "deadline", month, deadline=boundary.isoformat())
    await advance(db, now, month)


async def collection_schedule(db, month: str):
    batch = await db.get(GiftMonth, (ROOM, month))
    deadline = batch.deadline if batch else None
    settled = bool(batch and batch.settled)
    phase = "pending"
    next_change = None
    refill_deadline = None
    has_orders = (
        batch is not None
        and await db.scalar(
            select(GiftOrder.uid)
            .where(GiftOrder.room_id == ROOM, GiftOrder.month == month)
            .limit(1)
        )
        is not None
    )
    if has_orders and deadline is None:
        phase = "open"
    if (settled or has_orders) and deadline:
        now = now_cn()
        if now < deadline:
            phase, next_change = "open", deadline
        else:
            # A grace or extension still open for any UID keeps the month open
            # for address returns and delays its tracking template.
            end = collection_window_end()
            next_change, active_end, last_grace, last_extension = (
                await db.execute(
                    select(
                        # MySQL 5.7 returns a string for temporal CASE aggregates without a cast.
                        cast(func.min(case((end > now, end))), DateTime),
                        cast(func.max(end), DateTime),
                        func.max(GiftOrder.grace_until),
                        func.max(GiftOrder.extension_until),
                    ).where(GiftOrder.room_id == ROOM, GiftOrder.month == month)
                )
            ).one()
            refill_deadline = (
                active_end
                if next_change
                else max(
                    (end for end in (last_grace, last_extension) if end is not None),
                    default=None,
                )
            )
            phase = "grace" if next_change else "closed"
    return {
        "phase": phase,
        "settled": settled,
        "deadline": deadline,
        "refill_deadline": refill_deadline,
        "next_change": next_change,
    }


def collection_label(row, batch, has_address, registered, now):
    if not registered:
        return "未注册"
    if not batch.deadline or now < batch.deadline:
        return "已填写" if has_address else "收集中"
    if row.refill_requested and row.state == "grace" and row.grace_until > now:
        return "修改中"
    if has_address:
        return "已截止"
    if row.state == "grace" and row.extension_until and row.extension_until > now:
        return "延长收集中"
    return "未填写"


def order_view(row, batch, has_address, registered, now, waived=False):
    return {
        "month": row.month,
        "uid": row.uid,
        "nickname": row.nickname,
        "tier": TIERS[row.level],
        "roster_tier": TIERS.get(row.roster_level),
        "needs_reship": row.needs_reship,
        "state": row.state,
        "waived": waived or row.state == "waived",
        "has_address": has_address,
        "collection_status": collection_label(row, batch, has_address, registered, now),
        "deadline": batch.deadline,
        "grace_until": row.grace_until,
        "frozen_at": row.frozen_at,
        "refill_requested": row.refill_requested,
        "tracking": row.tracking,
    }


async def personal_orders(db, member: Member):
    results = (
        await db.execute(
            select(GiftOrder, GiftMonth, Binding)
            .join(
                GiftMonth,
                (GiftMonth.room_id == GiftOrder.room_id)
                & (GiftMonth.month == GiftOrder.month),
            )
            .outerjoin(Binding, order_binding())
            .where(
                GiftOrder.room_id == ROOM,
                or_(
                    GiftOrder.owner_id == member.id,
                    GiftOrder.owner_id.is_(None) & (Binding.member_id == member.id),
                ),
            )
            .order_by(GiftOrder.month.desc(), GiftOrder.uid)
        )
    ).all()
    gifts = []
    now = now_cn()
    for row, batch, binding in results:
        address = reveal(order_address(row, binding, member))
        gift = order_view(
            row, batch, bool(address), True, now, row.waived_by == member.id
        )
        username = row.bound_username if row.owner_id is not None else binding.username
        revising = row.refill_requested and row.state == "grace"
        gifts.append(
            {
                **gift,
                "username": username,
                "address": reveal(row.address) if revising else address,
                "returned_address": reveal(row.returned_address) if revising else None,
            }
        )
    return gifts


def can_edit_order(row, batch, now):
    if not batch or row.frozen_at:
        return False
    if row.state == "open":
        return batch.deadline is None or now < batch.deadline
    return (
        row.state == "grace" and row.grace_until is not None and now < row.grace_until
    )


async def get_order(db, month, uid):
    row = await db.get(GiftOrder, (ROOM, month, uid))
    if not row:
        raise HTTPException(404, "未找到该月资格")
    return row


def return_deadline(row, recipient, address, schedule):
    if (
        schedule["phase"] == "grace"
        and row.state in ("ready", "grace")
        and recipient
        and recipient.email
        and row.waived_by != recipient.id
        and address
    ):
        return schedule["refill_deadline"]
    return None


def roster_status(row, binding, member, batch, now):
    address = order_address(row, binding, member)
    waived = row.state == "waived" or (member is not None and row.waived_by == member.id)
    if waived:
        address = None
    status = order_view(row, batch, bool(address), bool(member and member.email), now, waived)
    if waived:
        status["collection_status"] = "已放弃"
    return status, address


async def roster_snapshot(db, batch, *, for_email=False):
    signature = hmac.new(get_settings().app_secret_key.encode(), b"harei-roster-v1", "sha256")
    rows = []
    now = now_cn()
    query = orders_with_members_query(batch.month).options(
        load_only(Member.id, Member.email, Member.auto_fill_address, Member.address),
        load_only(Binding.uid, Binding.address),
        defer(GiftOrder.returned_address),
    )
    stream = await db.stream(query.execution_options(yield_per=128))
    try:
        async for row, binding, member in stream:
            status, encrypted_address = roster_status(row, binding, member, batch, now)
            status["email"] = member.email if member else None
            status["bound"] = bool(status["email"])
            # Compare the same columns the attachment uses. Only delivery opens addresses.
            columns = roster_columns(status, (encrypted_address,))
            signature.update(json.dumps(columns, ensure_ascii=False).encode())
            signature.update(b"\n")
            if for_email:
                status["address"] = reveal(encrypted_address)
                rows.append(status)
    finally:
        await stream.close()
    return signature.hexdigest(), rows


async def read_page(db, query, page: int, size: int):
    count_query = query.order_by(None).with_only_columns(
        func.count(), maintain_column_froms=True
    )
    total = await db.scalar(count_query)
    page = min(page, max(1, (total + size - 1) // size))
    rows = (await db.execute(query.offset((page - 1) * size).limit(size))).all()
    return rows, {"page": page, "page_size": size, "total": total}


async def admin_record_page(db, month, kind, search, page):
    query = orders_with_members_query(month)
    if kind == "ready":
        query = query.where(GiftOrder.state == "ready")
    elif kind == "reship":
        query = query.where(GiftOrder.needs_reship.is_(True))
    elif kind == "missing":
        query = query.where(~has_order_address(), eligible_for_shipping())
    if search:
        query = query.where(
            or_(
                GiftOrder.uid.contains(search, autoescape=True),
                GiftOrder.nickname.contains(search, autoescape=True),
            )
        )
    rows, pagination = await read_page(db, query, page, 50)
    schedule = await collection_schedule(db, month)
    batch = await db.get(GiftMonth, (ROOM, month))
    items = []
    now = now_cn()
    for row, binding, member in rows:
        status, address = roster_status(row, binding, member, batch, now)
        status.pop("tracking")
        status["can_return_address"] = (
            return_deadline(row, member, address, schedule) is not None
        )
        items.append(status)
    return {**pagination, "schedule": schedule, "items": items}
