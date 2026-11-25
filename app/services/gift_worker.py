import asyncio
import logging
from datetime import timedelta

from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.models.captain import Captain
from app.models.member_gifts import (
    GiftMonth,
    GiftNotice,
    GiftOrder,
    GiftSystem,
    Member,
    PurchaseLevel,
)
from app.services.member_gifts import (
    RANK,
    advance,
    apply_month_purchases,
    grant_paid_purchase,
    lock_system,
    missing_address_reminders_query,
    now_cn,
    record_paid_levels,
    settle,
)
from app.services.member_security import ROOM, deliver

logger = logging.getLogger(__name__)


def next_month(value: str) -> str:
    year, month = int(value[:4]), int(value[4:])
    return f"{year + (month == 12):04}{month % 12 + 1:02}"


async def restore_current_captains():
    now = now_cn()
    month = now.strftime("%Y%m")
    async with async_session_factory.begin() as db:
        if month < (await lock_system(db)).first_month:
            return 0
        paid = (Captain.room_id == ROOM) & (Captain.paid_price > 0)
        legacy = (
            Captain.legacy_assumed_paid.is_(True)
            & Captain.room_id.is_(None)
            & Captain.paid_price.is_(None)
        )
        rows = await db.execute(
            select(Captain.user_uid, Captain.level, Captain.username)
            .where(
                Captain.joined_month == month,
                or_(paid, legacy),
            )
            .order_by(Captain.joined_at, Captain.id)
        )
        best = {}
        for uid, level, nickname in rows:
            if not (
                uid.isascii() and uid.isdecimal() and 0 < len(uid) <= 20 and int(uid)
            ):
                continue
            uid = str(int(uid))
            previous, name = best.get(uid, (RANK[level], ""))
            best[uid] = min(previous, RANK[level]), nickname or name
        await record_paid_levels(db, month, [(uid, *best[uid]) for uid in sorted(best)])
        applied = await apply_month_purchases(db, month, source="captains")
        if applied:
            await db.flush()
            await advance(db, now, month)
    logger.info(
        "上舰启动补读 month=%s 符合条件UID=%d 本次处理=%d", month, len(best), applied
    )
    return applied


async def try_restore_captains():
    try:
        await restore_current_captains()
        return True
    except (SQLAlchemyError, RedisError, HTTPException) as exc:
        logger.warning("本月上舰启动补读待重试 (%s)", type(exc).__name__)
        return False


async def apply_pending_purchases():
    async with async_session_factory() as db:
        pending = (
            await db.execute(
                select(PurchaseLevel.month, PurchaseLevel.uid)
                .where(
                    PurchaseLevel.room_id == ROOM,
                    PurchaseLevel.month
                    >= select(GiftSystem.first_month)
                    .where(GiftSystem.id == 1)
                    .scalar_subquery(),
                    PurchaseLevel.paid_level.is_not(None),
                    PurchaseLevel.applied_level.is_(None),
                )
                .order_by(PurchaseLevel.month, PurchaseLevel.uid)
                .limit(100)
            )
        ).all()
    for month, uid in pending:
        async with async_session_factory.begin() as db:
            await grant_paid_purchase(db, month, uid)


async def settle_due():
    from app.services import bili_captain_listener as listener

    now = now_cn()
    if now.day == 1 and now.hour == 0 and now.minute < 1:
        return
    async with async_session_factory() as db:
        system = await db.get(GiftSystem, 1)
        if not system:
            return
        month = system.first_month
        done = set(
            await db.scalars(
                select(GiftMonth.month).where(
                    GiftMonth.room_id == ROOM,
                    GiftMonth.roster_finalized_at.is_not(None),
                )
            )
        )
    while month < now.strftime("%Y%m"):
        if month not in done:
            async with listener.GUARD_REPORT_LOCK:
                rows = await listener._load_guard_cache(month, "roster")
                if rows is not None:
                    async with async_session_factory.begin() as db:
                        await settle(db, month, rows)
        month = next_month(month)


async def send_pending():
    # Keep SMTP I/O out of the account/fulfillment transaction. A row lock
    # prevents two workers from delivering the same pending notice concurrently.
    for _ in range(20):
        async with async_session_factory.begin() as db:
            now = now_cn()
            notice = await db.scalar(
                select(GiftNotice)
                .where(GiftNotice.sent.is_(False), GiftNotice.next_try <= now)
                .order_by(GiftNotice.id)
                .limit(1)
                .with_for_update()
            )
            if not notice:
                return
            if notice.kind != "reminder" or notice.deadline <= now:
                await db.delete(notice)
                continue
            # Recheck each queued UID, the recipient and the current deadline before every delivery attempt.
            rows = (
                await db.execute(
                    missing_address_reminders_query(notice.month)
                    .with_only_columns(Member.email, GiftOrder.uid)
                    .join(
                        GiftMonth,
                        (GiftMonth.room_id == GiftOrder.room_id)
                        & (GiftMonth.month == GiftOrder.month),
                    )
                    .where(
                        Member.id == notice.member_id,
                        GiftOrder.uid.in_(notice.uids),
                        GiftMonth.revision == notice.revision,
                        GiftMonth.deadline == notice.deadline,
                    )
                )
            ).all()
            if not rows:
                await db.delete(notice)
                continue
            base = get_settings().member_site_url.rstrip("/")
            lines = [
                f"以下 UID 尚未提交 {notice.month} 舰礼的收件地址，请在 {notice.deadline:%Y-%m-%d %H:%M} 前填写。",
                *(f"UID {uid}  {base}/personal?uid={uid}" for _, uid in rows),
            ]
            try:
                await deliver(
                    rows[0].email,
                    f"{notice.month} 舰礼 · 请填写收件地址",
                    "\n".join(lines),
                    bulk=True,
                )
            except (OSError, RuntimeError, ValueError, RedisError) as exc:
                notice.attempts += 1
                notice.next_try = now + timedelta(
                    seconds=min(3600, 30 * 2 ** min(7, notice.attempts))
                )
                logger.warning(
                    "舰礼通知投递待重试 id=%s type=%s", notice.id, type(exc).__name__
                )
            else:
                notice.uids = [uid for _, uid in rows]
                notice.sent = True


async def run(*, restore_pending=False):
    while True:
        if restore_pending:
            restore_pending = not await try_restore_captains()
        for action in (apply_pending_purchases, settle_due, tick, send_pending):
            try:
                await action()
            except Exception as exc:  # noqa: BLE001 - retry each independent job on the next tick
                logger.warning(
                    "舰礼任务 %s 暂未完成 (%s)", action.__name__, type(exc).__name__
                )
        await asyncio.sleep(15)


async def tick():
    async with async_session_factory.begin() as db:
        await lock_system(db)
        await advance(db, now_cn())
