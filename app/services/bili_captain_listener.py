from __future__ import annotations

import asyncio
import calendar
import datetime
import http.cookies
import json
import logging
import random
import re
import smtplib
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import ContentTypeError
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout

import blivedm
from app.core.config import get_settings
from app.core.redis import get_redis_client
from app.db.session import async_session_factory
from app.models.captain import Captain
from app.models.gift_ranking import GiftRanking
from app.services.captain_reports import save_captains_xlsx
from app.services.gift_mail import send_gift_message, send_smtp_message
from app.services.member_gifts import RANK, grant_paid_purchase, record_paid_levels
from app.services.member_security import DM_CODE_PATTERN, accept_dm

logger = logging.getLogger(__name__)


aiohttp_session: Optional[aiohttp.ClientSession] = None

ROOM_IDS: list[int] = [1820703922]
ROOM_ANCHORS = {1820703922: "花礼Harei"}
ROOM_CLIENTS: Dict[int, blivedm.BLiveClient] = {}
LAST_RECONNECT: Dict[int, datetime.datetime] = {}

ROOM_UIDS: Dict[int, int] = {}

LAST_STATUS: Dict[int, int] = {}
LIVE_INFO: Dict[int, Dict[str, str]] = {}
GUARD_REPORT_LOCK = asyncio.Lock()
GUARD_LATE_EVENTS: dict[str, dict[str, dict[str, Any]]] = {}
GUARD_EVENT_TASKS: set[asyncio.Task] = set()

COOKIE_ALERT_SENT = False


def _send_cookie_invalid_email_async(log_line: str) -> None:
    global COOKIE_ALERT_SENT
    if COOKIE_ALERT_SENT:
        return
    COOKIE_ALERT_SENT = True

    settings = get_settings()
    if not (settings.smtp_host and settings.email_from and settings.email_to):
        logger.warning("[SMTP] 未配置 SMTP_HOST/EMAIL_FROM/EMAIL_TO，跳过 Cookies 告警邮件")
        return

    def _worker() -> None:
        try:
            subject = "B站直播监听 Cookies 失效告警"
            body = (
                "检测到 B 站直播消息 uid=0，疑似 SESSDATA Cookies 已失效。\n\n"
                f"原始日志：{log_line}\n"
                "请尽快检查并更新 BILI_* Cookies 环境变量。"
            )
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = settings.email_from
            msg["To"] = settings.email_to

            use_ssl = int(settings.smtp_port) == 465
            tls = ssl.create_default_context()
            connection = (
                smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=12, context=tls)
                if use_ssl else smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=12)
            )
            with connection as server:
                if not use_ssl:
                    server.starttls(context=tls)
                if settings.smtp_user and settings.smtp_pass:
                    server.login(settings.smtp_user, settings.smtp_pass)
                send_smtp_message(server, msg, settings.email_from, [settings.email_to], login=settings.smtp_user)

            logger.info("[SMTP] Cookies 失效告警邮件已发送")
        except Exception as e:
            logger.error("[SMTP] 发送 Cookies 失效告警失败: %s", e)

    threading.Thread(target=_worker, daemon=True).start()


def _now() -> datetime.datetime:
    return datetime.datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)


def month_str(dt: Optional[datetime.datetime] = None) -> str:
    dt = dt or _now()
    return dt.strftime("%Y%m")


def _next_month_report_time(now: datetime.datetime) -> datetime.datetime:
    target = datetime.datetime(year=now.year, month=now.month, day=1, hour=0, minute=1, second=0)
    if now <= target:
        return target
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return datetime.datetime(year=year, month=month, day=1, hour=0, minute=1, second=0)


def _next_guard_snapshot_time(now: datetime.datetime) -> datetime.datetime:
    last_day = calendar.monthrange(now.year, now.month)[1]
    for day in ([30, 31] if last_day == 31 else [last_day]):
        target = datetime.datetime(now.year, now.month, day, 23, 55)
        if now <= target:
            return target
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    last_day = calendar.monthrange(year, month)[1]
    return datetime.datetime(year, month, 30 if last_day == 31 else last_day, 23, 55)


def _report_month_for_target(target_time: datetime.datetime) -> str:
    if target_time.month == 1:
        return f"{target_time.year - 1}12"
    return f"{target_time.year}{target_time.month - 1:02d}"


def _parse_room_ids_from_env(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        s = part.strip()
        if not s:
            continue
        try:
            out.append(int(s))
        except ValueError:
            continue
    return out


def init_session() -> None:
    settings = get_settings()

    cookies_base = {
        "SESSDATA": settings.bili_sessdata,
        "bili_jct": settings.bili_bili_jct,
        "DedeUserID": settings.bili_dedeuserid,
        "DedeUserID__ckMd5": settings.bili_dedeuserid_ckmd5,
        "sid": settings.bili_sid,
        "buvid3": settings.bili_buvid3,
        "deviceFingerprint": settings.bili_device_fingerprint,
    }

    cookies = http.cookies.SimpleCookie()
    for k, v in cookies_base.items():
        if not v:
            continue
        cookies[k] = v
        cookies[k]["domain"] = "bilibili.com"
        cookies[k]["secure"] = True

    global aiohttp_session
    connector = aiohttp.TCPConnector(ssl=False)
    aiohttp_session = aiohttp.ClientSession(connector=connector)
    aiohttp_session.cookie_jar.update_cookies(cookies)

    logger.info("[session] 已初始化 Cookies：%s", ",".join(cookies.keys()) or "(empty)")

    raw_room_ids = settings.bili_room_ids or ""
    if raw_room_ids.strip():
        ids = _parse_room_ids_from_env(raw_room_ids)
        if ids:
            global ROOM_IDS
            ROOM_IDS = ids
            logger.info("[session] 使用 env 覆盖 ROOM_IDS，总数=%d", len(ROOM_IDS))

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)

LIVE_STATUS_API = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
ROOM_INFO_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
GUARD_LIST_API = "https://api.live.bilibili.com/xlive/app-room/v2/guardTab/topListNew"
GUARD_REPORT_RECEIVER = "harei0301@163.com"
GUARD_REPORT_ROOT = Path("download_files")
GUARD_REPORT_RUID = 1048135385
GUARD_REPORT_ROOM_ID = 1820703922
GUARD_CACHE_TTL = 90 * 24 * 60 * 60
GUARD_FETCH_ATTEMPTS = 3


async def _fetch_room_uid(room_id: int) -> Optional[int]:
    if aiohttp_session is None:
        logger.error("[RoomInfo] aiohttp_session 未初始化")
        return None

    url = f"{ROOM_INFO_API}?room_id={room_id}"
    try:
        async with aiohttp_session.get(
            url,
            timeout=5,
            headers={"User-Agent": USER_AGENT, "Referer": "https://live.bilibili.com"},
        ) as resp:
            if resp.status != 200:
                logger.warning("[RoomInfo] room_id=%s get_info HTTP %s", room_id, resp.status)
                return None
            try:
                payload = await resp.json(content_type=None)
            except ContentTypeError:
                text = (await resp.text())[:200]
                logger.warning("[RoomInfo] room_id=%s get_info 非 JSON，前 200 字：%s", room_id, text)
                return None
    except Exception as e:
        logger.error("[RoomInfo] room_id=%s 请求异常: %s", room_id, e)
        return None

    data = payload.get("data") or {}
    uid_raw = data.get("uid")
    try:
        uid = int(uid_raw)
    except (TypeError, ValueError):
        uid = 0

    if uid:
        return uid
    logger.warning("[RoomInfo] room_id=%s uid 获取失败，原始值=%r", room_id, uid_raw)
    return None


async def init_uids_once(max_rounds: int = 5) -> None:
    logger.info("[init] 开始初始化 UID（get_info）")
    for round_idx in range(1, max_rounds + 1):
        missing = [rid for rid in ROOM_IDS if rid not in ROOM_UIDS]
        if not missing:
            logger.info("[init] 所有 UID 已成功获取")
            return
        logger.info("[init] 第 %d/%d 轮获取 UID，待获取房间数=%d", round_idx, max_rounds, len(missing))
        for room_id in missing:
            uid = await _fetch_room_uid(room_id)
            if uid:
                ROOM_UIDS[room_id] = uid
            await asyncio.sleep(0.3)

    missing = [rid for rid in ROOM_IDS if rid not in ROOM_UIDS]
    if missing:
        logger.error("[init] 经过 %d 轮仍有 UID 获取失败，将在状态轮询中跳过: %s", max_rounds, missing)


async def monitor_all_rooms_status() -> None:
    while not ROOM_UIDS:
        logger.info("[LiveStatus] 等待 UID 初始化...")
        await asyncio.sleep(1)

    logger.info("[LiveStatus] UID 初始化完成，可用房间数=%d，启动状态轮询", len(ROOM_UIDS))

    for rid in ROOM_IDS:
        LAST_STATUS.setdefault(rid, 0)
        LIVE_INFO.setdefault(rid, {"live_time": "0000-00-00 00:00:00", "title": ""})

    while True:
        try:
            if aiohttp_session is None:
                logger.error("[LiveStatus] aiohttp_session 未初始化")
                await asyncio.sleep(3)
                continue

            params = [("uids[]", str(uid)) for uid in ROOM_UIDS.values()]
            async with aiohttp_session.get(
                LIVE_STATUS_API,
                params=params,
                timeout=10,
                headers={"User-Agent": USER_AGENT, "Referer": "https://live.bilibili.com"},
            ) as resp:
                if resp.status != 200:
                    logger.warning("[LiveStatus] HTTP %s", resp.status)
                    await asyncio.sleep(3)
                    continue
                try:
                    payload = await resp.json(content_type=None)
                except ContentTypeError:
                    text = (await resp.text())[:200]
                    logger.warning("[LiveStatus] 非 JSON，前 200 字：%s", text)
                    await asyncio.sleep(3)
                    continue

            data = payload.get("data") or {}
            now = _now()

            for room_id in ROOM_IDS:
                uid = ROOM_UIDS.get(room_id)
                info = data.get(str(uid)) if uid is not None else None
                prev = LAST_STATUS.get(room_id, 0)

                if not info or "live_status" not in info:
                    # 沿用上一轮
                    continue

                raw_status = int(info.get("live_status", 0))
                status = 0 if raw_status == 2 else raw_status
                LAST_STATUS[room_id] = status

                if status == 1:
                    live_time_raw = info.get("live_time", 0)
                    try:
                        if isinstance(live_time_raw, (int, float)) or str(live_time_raw).isdigit():
                            start_dt = datetime.datetime.fromtimestamp(int(live_time_raw))
                        else:
                            start_dt = now
                    except (ValueError, OSError, OverflowError):
                        start_dt = now

                    raw_title = info.get("title") or ""
                    LIVE_INFO[room_id]["live_time"] = start_dt.strftime("%Y-%m-%d %H:%M:%S")
                    LIVE_INFO[room_id]["title"] = raw_title

                    if prev == 0:
                        logger.info("[LiveStatus] room_id=%s 上播 %s", room_id, LIVE_INFO[room_id]["live_time"])
                else:
                    LIVE_INFO[room_id]["live_time"] = "0000-00-00 00:00:00"
                    LIVE_INFO[room_id]["title"] = ""
                    if prev == 1:
                        logger.info("[LiveStatus] room_id=%s 下播", room_id)

        except Exception as e:
            logger.error("[LiveStatus] 调用异常: %s", e)

        await asyncio.sleep(3)

@dataclass(slots=True)
class CaptainEvent:
    uid: str
    username: str
    level: str
    ship_count: int
    is_red_packet: bool
    joined_at: datetime.datetime
    room_id: int = 0
    paid_price: int | None = None

    @property
    def is_paid(self) -> bool:
        return self.paid_price is not None and self.paid_price > 0


CAPTAIN_ENQUEUE_TASKS: set[asyncio.Task] = set()
CAPTAIN_QUEUE: "asyncio.Queue[CaptainEvent]" = asyncio.Queue(maxsize=5000)

def _level_name(guard_level: Any) -> Optional[str]:
    try:
        gl = int(guard_level)
    except Exception:
        return None
    # bilibili: 3=舰长, 2=提督, 1=总督
    if gl == 3:
        return "舰长"
    if gl == 2:
        return "提督"
    if gl == 1:
        return "总督"
    return None


class GuardSnapshotError(RuntimeError):
    """An incomplete/failed snapshot must never become a successful report."""


@dataclass(slots=True)
class GuardPage:
    rows: list[dict[str, Any]]
    total: int
    pages: int


async def _fetch_guard_page(page: int) -> GuardPage:
    if aiohttp_session is None:
        raise GuardSnapshotError("aiohttp_session 未初始化")

    params = {
        "ruid": GUARD_REPORT_RUID,
        "roomid": GUARD_REPORT_ROOM_ID,
        "page": page,
        "page_size": 30,
        "typ": 5,
    }
    for attempt in range(GUARD_FETCH_ATTEMPTS):
        try:
            async with aiohttp_session.get(
                GUARD_LIST_API,
                params=params,
                timeout=10,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": f"https://live.bilibili.com/{GUARD_REPORT_ROOM_ID}",
                },
            ) as resp:
                if resp.status != 200:
                    raise GuardSnapshotError(f"page={page} HTTP {resp.status}")
                payload = await resp.json(content_type=None)
            if payload.get("code") != 0:
                raise GuardSnapshotError(f"page={page} API code={payload.get('code')}")
            data = payload["data"]
            info = data["info"]
            total, page_count = int(info["num"]), int(info["page"])
            if total < 0 or not 0 <= page_count <= 10000 or int(info["now"]) != page:
                raise GuardSnapshotError(f"page={page} 分页元数据无效")
            pages = max(1, page_count)
            raw_list = [] if data["list"] is None else data["list"]
            top3 = [] if data["top3"] is None else data["top3"]
            if not isinstance(raw_list, list) or not isinstance(top3, list):
                raise GuardSnapshotError(f"page={page} 名单结构无效")
            # top3 is returned on every page, separately from list (rank 4+).
            return GuardPage(_normalize_guard_rows(top3 + raw_list), total, pages)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError,
                KeyError, AttributeError, GuardSnapshotError) as exc:
            if attempt + 1 == GUARD_FETCH_ATTEMPTS:
                raise GuardSnapshotError(f"page={page} 抓取失败（已重试）") from exc
            logger.warning("[Guard] page=%s 抓取失败，重试 %s/%s: %s",
                           page, attempt + 1, GUARD_FETCH_ATTEMPTS, exc)
            await asyncio.sleep(attempt + 1)
    raise GuardSnapshotError("抓取重试耗尽")


def _normalize_guard_rows(raw_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in raw_list:
        uinfo = item.get("uinfo") or {}
        base = uinfo.get("base") or {}
        guard = uinfo.get("guard") or {}
        uid = str(int(uinfo.get("uid") or base.get("uid") or 0))
        name = base.get("name") or ""
        level = _level_name(guard.get("level")) or ""
        if int(uid) <= 0 or not level:
            raise GuardSnapshotError("在舰列表包含无效 UID 或等级")
        rows.append({"uid": uid, "name": str(name), "level": level})
    return rows


def _merge_guard_rows(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each UID's highest tier across the sampled days and its latest nickname."""
    merged: dict[str, dict[str, Any]] = {}
    for source in sources:
        for row in source:
            uid = str(int(row["uid"]))
            if int(uid) <= 0 or row["level"] not in ("舰长", "提督", "总督"):
                raise GuardSnapshotError("快照包含无效 UID 或等级")
            level = row["level"]
            if uid in merged and RANK[merged[uid]["level"]] < RANK[level]:
                level = merged[uid]["level"]
            merged[uid] = {"uid": uid, "name": str(row["name"]), "level": level}
    return list(merged.values())


async def _collect_guard_rows() -> list[dict[str, Any]]:
    for attempt in range(GUARD_FETCH_ATTEMPTS):
        first = await _fetch_guard_page(1)
        rows = first.rows
        consistent = True
        for page in range(2, first.pages + 1):
            await asyncio.sleep(0.5)
            current = await _fetch_guard_page(page)
            if (current.total, current.pages) != (first.total, first.pages):
                consistent = False
                break
            rows = _merge_guard_rows(rows, current.rows)
        rows = _merge_guard_rows(rows)
        if consistent and len(rows) == first.total:
            return rows
        logger.warning("[Guard] 名单变化/人数不符 expected=%s actual=%s，整轮重试 %s/%s",
                       first.total, len(rows), attempt + 1, GUARD_FETCH_ATTEMPTS)
    raise GuardSnapshotError("在舰列表人数校验失败，拒绝生成不完整快照")


def _build_guard_report_xlsx(rows: list[dict[str, Any]], month: str) -> Path:
    GUARD_REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = f"{month}在舰列表"

    headers = ["UID", "用户名", "舰长等级"]
    worksheet.append(headers)

    column_widths = [len(header) for header in headers]
    for row_number, row in enumerate(rows, start=2):
        values = [row.get("uid", ""), row.get("name", ""), row.get("level", "")]
        worksheet.append(values)
        for index, value in enumerate(values):
            if isinstance(value, str):
                worksheet.cell(row_number, index + 1).data_type = "s"
            value_length = len(str(value))
            if value_length > column_widths[index]:
                column_widths[index] = value_length

    for index, width in enumerate(column_widths, start=1):
        worksheet.column_dimensions[get_column_letter(index)].width = width + 2

    filename = f"{month}在舰列表.xlsx"
    file_path = GUARD_REPORT_ROOT / filename
    # Replace atomically: a restart must not leave a half-written attachment.
    with tempfile.NamedTemporaryFile(dir=GUARD_REPORT_ROOT, suffix=".xlsx", delete=False) as tmp:
        temporary_path = Path(tmp.name)
    try:
        workbook.save(temporary_path)
        temporary_path.replace(file_path)
    finally:
        workbook.close()
        temporary_path.unlink(missing_ok=True)
    return file_path


async def _collect_monthly_captain_rows(month: str) -> list[Captain]:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Captain).where(Captain.joined_month == month).order_by(Captain.joined_at.asc())
        )
        return list(result.scalars().all())


def _guard_cache_key(month: str, kind: str) -> str:
    return f"harei:guard-report:{GUARD_REPORT_ROOM_ID}:{month}:{kind}"


def _observed_roster_path(month: str, kind: str) -> Path:
    return GUARD_REPORT_ROOT / ".snapshots" / f"{GUARD_REPORT_ROOM_ID}-{month}-{kind}.json"


def _write_observed_roster(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        temp_path.write_text(contents, encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


async def _load_guard_cache(month: str, kind: str) -> Optional[list[dict[str, Any]]]:
    redis = await get_redis_client()
    raw = await redis.get(_guard_cache_key(month, kind))
    if raw is None and kind in ("day30", "roster"):
        try:
            raw = await asyncio.to_thread(_observed_roster_path(month, kind).read_text, encoding="utf-8")
        except FileNotFoundError:
            return None
    if raw is None:
        return None
    data = json.loads(raw)
    if (data["month"] != month or data["room_id"] != GUARD_REPORT_ROOM_ID
            or not isinstance(data["rows"], list)):
        raise GuardSnapshotError("Redis 快照元数据无效")
    return _merge_guard_rows(data["rows"])


async def _save_guard_cache(month: str, kind: str, rows: list[dict[str, Any]],
                            target_time: datetime.datetime) -> None:
    redis = await get_redis_client()
    data = {"month": month, "room_id": GUARD_REPORT_ROOM_ID,
            "target_time": target_time.isoformat(), "rows": rows}
    contents = json.dumps(data, ensure_ascii=False)
    if kind in ("day30", "roster"):
        # The mailed XLSX also contains late purchases. Retain the actual API roster separately.
        await asyncio.to_thread(_write_observed_roster, _observed_roster_path(month, kind), contents)
    await redis.set(_guard_cache_key(month, kind), contents, ex=GUARD_CACHE_TTL)


async def _persist_late_guard_event(month: str, field: str, row: dict[str, Any]) -> None:
    try:
        redis = await get_redis_client()
        async with redis.pipeline(transaction=True) as pipe:
            pipe.hset(_guard_cache_key(month, "late-paid-events"), field,
                      json.dumps(row, ensure_ascii=False))
            pipe.expire(_guard_cache_key(month, "late-paid-events"), GUARD_CACHE_TTL)
            await pipe.execute()
    except Exception:
        # Retain the in-process copy; the database event is also written normally.
        logger.exception("[Guard] month=%s 新上舰事件 Redis 缓存失败", month)


def _queue_late_guard_event(ev: CaptainEvent, room_id: int) -> None:
    month = month_str(ev.joined_at)
    if not ev.is_paid or room_id != GUARD_REPORT_ROOM_ID or ev.joined_at < _guard_snapshot_target_for_month(month):
        return
    # Record synchronously before scheduling Redis I/O, so a concurrent fetch
    # cannot miss an event that has already reached the handler.
    field = f"{ev.joined_at.isoformat()}:{ev.uid}:{RANK[ev.level]}"
    row = {"uid": ev.uid, "name": ev.username, "level": ev.level}
    GUARD_LATE_EVENTS.setdefault(month, {})[field] = row
    task = asyncio.create_task(_persist_late_guard_event(month, field, row))
    GUARD_EVENT_TASKS.add(task)
    task.add_done_callback(GUARD_EVENT_TASKS.discard)


async def _late_guard_rows(month: str) -> list[dict[str, Any]]:
    redis = await get_redis_client()
    events = await redis.hgetall(_guard_cache_key(month, "late-paid-events"))
    records = {field: json.loads(value) for field, value in events.items()}
    records.update(GUARD_LATE_EVENTS.get(month, {}))
    return _merge_guard_rows([records[field] for field in sorted(records)])


async def _snapshot_guard_report(target_time: datetime.datetime) -> Optional[Path]:
    async with GUARD_REPORT_LOCK:
        # Never fetch next month's live state under the previous month's label.
        if _now().date() != target_time.date():
            raise GuardSnapshotError("已跨过抓取日期，不能用当前名单冒充历史快照")
        month = target_time.strftime("%Y%m")
        last_day = calendar.monthrange(target_time.year, target_time.month)[1]
        is_preliminary = last_day == 31 and target_time.day == 30
        saved = await _load_guard_cache(month, "day30" if is_preliminary else "final")
        if not is_preliminary and saved is None:
            saved = await _load_guard_cache(month, "roster")
        if saved is not None:
            # Restart/retry must not replace an already-complete 23:55 snapshot
            # with a later state where some of those members have expired.
            if is_preliminary:
                return None
            rows = _merge_guard_rows(saved, await _late_guard_rows(month))
            await _save_guard_cache(month, "final", rows, target_time)
            return await asyncio.to_thread(_build_guard_report_xlsx, rows, month)
        prior_rows: list[dict[str, Any]] = []
        if last_day == 31 and not is_preliminary:
            cached = await _load_guard_cache(month, "day30")
            if cached is None:
                raise GuardSnapshotError(f"{month} 缺少 30 号快照，不能生成双日合并名单")
            prior_rows = cached
        logger.info("[Guard] 开始拉取 %s 在舰列表快照 day=%s", month, target_time.day)
        rows = await _collect_guard_rows()
        if _now().date() != target_time.date():
            raise GuardSnapshotError("抓取过程跨日，拒绝将跨日数据写入快照")
        if is_preliminary:
            await _save_guard_cache(month, "day30", rows, target_time)
            logger.info("[Guard] 30 号快照已存入 Redis month=%s count=%s", month, len(rows))
            return None
        roster = _merge_guard_rows(prior_rows, rows)
        await _save_guard_cache(month, "roster", roster, target_time)
        rows = _merge_guard_rows(roster, await _late_guard_rows(month))
        await _save_guard_cache(month, "final", rows, target_time)
        path = await asyncio.to_thread(_build_guard_report_xlsx, rows, month)
        logger.info("[Guard] 在舰列表快照已生成 month=%s count=%s path=%s", month, len(rows), path)
        return path


def _guard_snapshot_path(month: str) -> Path:
    return GUARD_REPORT_ROOT / f"{month}在舰列表.xlsx"


def _guard_snapshot_target_for_month(month: str) -> datetime.datetime:
    year = int(month[:4])
    month_num = int(month[4:6])
    last_day = calendar.monthrange(year, month_num)[1]
    return datetime.datetime(year, month_num, last_day, 23, 55)


def _read_guard_snapshot(month: str) -> list[dict[str, Any]]:
    workbook = load_workbook(_guard_snapshot_path(month), read_only=True)
    try:
        worksheet = workbook.active
        assert worksheet is not None
        values = worksheet.iter_rows(values_only=True)
        if next(values, None) != ("UID", "用户名", "舰长等级"):
            raise GuardSnapshotError("在舰附件表头无效")
        return _merge_guard_rows([
            {"uid": uid, "name": name or "", "level": level}
            for uid, name, level in values
        ])
    finally:
        workbook.close()


def _send_guard_report_email(
    guard_file_path: Path,
    captain_file_path: Path,
    month: str,
    guard_total: int,
    captain_total: int,
) -> None:
    settings = get_settings()
    if not (settings.gift_smtp_host and settings.gift_email_from):
        raise GuardSnapshotError("未配置舰礼发信服务，月度舰长邮件未发送")

    cc_list = settings.email_cc_list

    subject = f"{month}在舰列表与上舰情况（自动发送）"
    body = (
        f"{month} 在舰列表与上舰情况已生成。\n"
        f"在舰列表人数：{guard_total} 人\n"
        f"上舰记录数量：{captain_total} 条"
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["To"] = GUARD_REPORT_RECEIVER
    if cc_list:
        msg["Cc"] = ",".join(cc_list)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for file_path in (guard_file_path, captain_file_path):
        with file_path.open("rb") as f:
            attachment = MIMEApplication(
                f.read(),
                _subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        attachment.add_header("Content-Disposition", "attachment", filename=file_path.name)
        msg.attach(attachment)

    send_gift_message(settings, msg, [GUARD_REPORT_RECEIVER, *cc_list])


async def _run_guard_report(target_time: datetime.datetime) -> None:
    await drain_captain_queue()
    if GUARD_EVENT_TASKS:
        await asyncio.gather(*list(GUARD_EVENT_TASKS))
    month = _report_month_for_target(target_time)
    async with GUARD_REPORT_LOCK:
        redis = await get_redis_client()
        if await redis.get(_guard_cache_key(month, "sent")):
            logger.info("[Guard] month=%s 月度邮件已经发送，跳过", month)
            return
        logger.info("[Guard] 开始生成 %s 月度舰长邮件", month)
        rows = await _load_guard_cache(month, "final")
        if rows is None:
            path = _guard_snapshot_path(month)
            if not path.exists():
                raise GuardSnapshotError(f"{month} 缺少月底快照，拒绝用次月实时数据补拉")
            # Also supports existing attachments made before Redis persistence.
            rows = await asyncio.to_thread(_read_guard_snapshot, month)
        rows = _merge_guard_rows(rows, await _late_guard_rows(month))
        await _save_guard_cache(month, "final", rows, _guard_snapshot_target_for_month(month))
        guard_file_path = await asyncio.to_thread(_build_guard_report_xlsx, rows, month)
        captain_rows = await _collect_monthly_captain_rows(month)
        captain_file_path = await asyncio.to_thread(
            save_captains_xlsx, captain_rows, month, GUARD_REPORT_ROOT)
        await asyncio.to_thread(_send_guard_report_email, guard_file_path, captain_file_path,
                                month, len(rows), len(captain_rows))
        await redis.set(_guard_cache_key(month, "sent"), "1", ex=GUARD_CACHE_TTL)
        GUARD_LATE_EVENTS.pop(month, None)
        logger.info("[Guard] 月度舰长邮件已发送 month=%s guard_count=%s captain_count=%s",
                    month, len(rows), len(captain_rows))


async def _guard_snapshot_scheduler() -> None:
    now = _now()
    last_day = calendar.monthrange(now.year, now.month)[1]
    today = now.replace(hour=23, minute=55, second=0, microsecond=0)
    # A restart during the last five minutes may still capture today's state.
    if now.day in ([30, 31] if last_day == 31 else [last_day]) and now >= today:
        target = today
    else:
        target = _next_guard_snapshot_time(now)
    while True:
        await asyncio.sleep(max(0, (target - _now()).total_seconds()))
        while True:
            try:
                await _snapshot_guard_report(target)
                break
            except Exception:
                logger.exception("[Guard] 在舰快照任务失败 target=%s", target)
                if _now().date() != target.date():
                    break
                await asyncio.sleep(30)
        target = _next_guard_snapshot_time(target + datetime.timedelta(seconds=1))


async def _guard_email_scheduler() -> None:
    now = _now()
    # Startup/restart waits for the next scheduled send, never backfills a
    # historical report just because its Redis sent marker is missing.
    target = _next_month_report_time(now)
    while True:
        await asyncio.sleep(max(0, (target - _now()).total_seconds()))
        for attempt in range(3):
            try:
                await _run_guard_report(target)
                break
            except Exception:
                logger.exception("[Guard] 月度邮件任务失败 target=%s", target)
                if attempt < 2:
                    await asyncio.sleep(30)
        # Advance from the scheduled target, not completion time. Long-running
        # snapshots cannot push a due 00:01 report to the following month.
        target = _next_month_report_time(target + datetime.timedelta(seconds=1))


async def guard_report_scheduler() -> None:
    snapshot_task = asyncio.create_task(_guard_snapshot_scheduler())
    email_task = asyncio.create_task(_guard_email_scheduler())
    try:
        await asyncio.gather(snapshot_task, email_task)
    finally:
        snapshot_task.cancel()
        email_task.cancel()
        await asyncio.gather(snapshot_task, email_task, return_exceptions=True)


async def record_paid_level(db, ev):
    await record_paid_levels(db, month_str(ev.joined_at), [(ev.uid, RANK[ev.level], ev.username)])


async def write_captain_event(ev):
    paid = ev.room_id == GUARD_REPORT_ROOM_ID and ev.is_paid
    try:
        async with async_session_factory.begin() as db:
            db.add(
                Captain(
                    user_uid=ev.uid,
                    username=ev.username,
                    joined_at=ev.joined_at,
                    joined_month=month_str(ev.joined_at),
                    level=ev.level,
                    ship_count=int(ev.ship_count or 1),
                    is_red_packet=ev.is_red_packet,
                    room_id=ev.room_id,
                    paid_price=ev.paid_price,
                )
            )
            if paid:
                await record_paid_level(db, ev)
    except (OperationalError, InterfaceError, PoolTimeout):
        if not paid:
            raise
        # A disconnect during commit has an unknown outcome. Replay only the idempotent evidence.
        delay = 1
        while True:
            logger.warning("[Captain] 上舰付费记录待重试 uid=%s", ev.uid)
            await asyncio.sleep(delay)
            try:
                async with async_session_factory.begin() as db:
                    await record_paid_level(db, ev)
                break
            except (OperationalError, InterfaceError, PoolTimeout):
                delay = min(30, delay * 2)
    if paid:
        # A cache/fulfillment failure cannot roll back the confirmed payment. The gift worker retries it.
        async with async_session_factory.begin() as db:
            await grant_paid_purchase(db, month_str(ev.joined_at), ev.uid)


async def captain_writer_worker() -> None:
    while True:
        ev = await CAPTAIN_QUEUE.get()
        try:
            await write_captain_event(ev)
        except Exception:
            logger.exception("[Captain] 写入或资格同步失败 uid=%s", ev.uid)
        finally:
            CAPTAIN_QUEUE.task_done()


async def drain_captain_queue():
    if CAPTAIN_ENQUEUE_TASKS:
        await asyncio.gather(*list(CAPTAIN_ENQUEUE_TASKS))
    await CAPTAIN_QUEUE.join()

async def _start_client(room_id: int) -> None:
    global _last_sender_valid, _last_heartbeat
    if room_id == GUARD_REPORT_ROOM_ID:
        _last_sender_valid, _last_heartbeat = None, 0.0
    client = blivedm.BLiveClient(room_id, session=aiohttp_session)
    client.set_handler(MyHandler())
    client.start()
    ROOM_CLIENTS[room_id] = client
    LAST_RECONNECT.setdefault(room_id, _now() - datetime.timedelta(days=random.random() * 3.0))
    logger.info("[connect] 已连接房间 %s", room_id)


async def run_clients_loop() -> None:
    for room_id in ROOM_IDS:
        await _start_client(room_id)
        await asyncio.sleep(3)


async def _reconnect_one(room_id: int) -> None:
    if LAST_STATUS.get(room_id, 0) == 1:
        logger.info("[reconnect] room_id=%s 在播，跳过重连", room_id)
        return

    client = ROOM_CLIENTS.get(room_id)
    if client is not None:
        try:
            await client.stop_and_close()
        except asyncio.CancelledError:
            logger.debug("[reconnect] room_id=%s stop_and_close Cancelled（预期）", room_id)
        except Exception as e:
            logger.warning("[reconnect] room_id=%s stop_and_close 异常: %s", room_id, e)

    await asyncio.sleep(3)
    await _start_client(room_id)
    LAST_RECONNECT[room_id] = _now()
    logger.info("[reconnect] room_id=%s 重连完成", room_id)


async def reconnect_scheduler() -> None:
    while True:
        now = _now()
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        sleep_sec = max(1.0, (target - now).total_seconds())
        logger.info("[reconnect] 距离下一次全量重连还有约 %.1f 分钟", sleep_sec / 60.0)
        await asyncio.sleep(sleep_sec)

        logger.info("[reconnect] 开始执行每日全量重连任务")
        for room_id in ROOM_IDS:
            if LAST_STATUS.get(room_id, 0) == 1:
                logger.info("[reconnect] room_id=%s 在播，跳过", room_id)
                continue
            try:
                await _reconnect_one(room_id)
            except asyncio.CancelledError:
                logger.debug("[reconnect] room_id=%s Cancelled（预期）", room_id)
            except Exception as e:
                logger.error("[reconnect] room_id=%s 重连失败: %s", room_id, e)
            await asyncio.sleep(3 + random.uniform(0.5, 2.0))
        logger.info("[reconnect] 本日全量重连任务完成")


_last_heartbeat = 0.0
_last_sender_valid: bool | None = None
_dm_tasks: set[asyncio.Task] = set()


def verification_listener_status() -> str:
    client = ROOM_CLIENTS.get(GUARD_REPORT_ROOM_ID)
    if not client or not client.is_running:
        return "offline"
    if time.monotonic() - _last_heartbeat > 75:
        return "connecting"
    if _last_sender_valid is False or not get_settings().bili_sessdata:
        return "uid_unavailable"
    if _last_sender_valid is None:
        return "awaiting_danmaku"
    return "online"


async def _match_verification(uid: str, username: str, message: str):
    try:
        await accept_dm(await get_redis_client(), GUARD_REPORT_ROOM_ID, uid, username, message)
    except Exception as exc:
        logger.warning("弹幕验证暂不可用 (%s)", type(exc).__name__)


class MyHandler(blivedm.BaseHandler):
    def _on_danmaku(self, client, message):
        global _last_sender_valid, _last_heartbeat
        if client.room_id != GUARD_REPORT_ROOM_ID or message.is_mirror or message.dm_type != 0:
            return
        uid = str(message.uid or "")
        _last_heartbeat = time.monotonic()
        _last_sender_valid = bool(re.fullmatch(r"[1-9][0-9]{0,19}", uid))
        if _last_sender_valid and DM_CODE_PATTERN.fullmatch(message.msg) and len(_dm_tasks) < 128:
            task = asyncio.create_task(_match_verification(uid, message.uname or "", message.msg))
            _dm_tasks.add(task)
            task.add_done_callback(_dm_tasks.discard)

    def _on_heartbeat(self, client: blivedm.BLiveClient, message: Any) -> None:  # noqa: N802
        global _last_heartbeat
        if client.room_id == GUARD_REPORT_ROOM_ID:
            _last_heartbeat = time.monotonic()

    def _on_gift(self, client: blivedm.BLiveClient, message: Any) -> None:  # noqa: N802
        try:
            uid = getattr(message, "uid", 0) or 0
            if int(uid) == 0:
                _send_cookie_invalid_email_async(f"[{client.room_id}] gift uid=0")
                return

            gift_name = getattr(message, "gift_name", "") or ""
            if gift_name != "口水黄豆":
                return

            num = getattr(message, "num", 1) or 1
            username = getattr(message, "uname", None) or getattr(message, "username", None) or ""
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning("[Gift] 未找到运行中的事件循环，跳过记录 uid=%s", uid)
                return

            loop.create_task(
                _record_gift_ranking(uid=str(uid), username=str(username), gift_count=int(num))
            )
            logger.info(
                "[%s] %s 送了 %s 个口水黄豆",
                client.room_id,
                username,
                num,
            )
        except Exception as e:
            logger.error("[Gift] 处理礼物记录异常: %s", e)

    def _on_user_toast_v2(self, client: blivedm.BLiveClient, message: Any) -> None:  # noqa: N802
        try:
            uid = getattr(message, "uid", 0) or 0
            if int(uid) == 0:
                _send_cookie_invalid_email_async(f"[{client.room_id}] user_toast uid=0")
                return

            username = getattr(message, "username", None) or getattr(message, "uname", None) or ""
            level = _level_name(getattr(message, "guard_level", None))
            if not level:
                return

            num = getattr(message, "num", 1) or 1
            # USER_TOAST_MSG_V2.pay_info.price is the transaction price in gold seeds.
            # A blind-box award can contain days and a gift with a face value while costing zero.
            raw_price = getattr(message, "price", None)
            price = int(raw_price) if re.fullmatch(r"[0-9]{1,18}", str(raw_price)) else None

            ev = CaptainEvent(
                uid=str(uid),
                username=str(username),
                level=level,
                ship_count=int(num),
                is_red_packet=price == 1900,
                paid_price=price,
                joined_at=_now(),
                room_id=client.room_id,
            )

            _queue_late_guard_event(ev, client.room_id)

            try:
                CAPTAIN_QUEUE.put_nowait(ev)
            except asyncio.QueueFull:
                if ev.is_paid and ev.room_id == GUARD_REPORT_ROOM_ID:
                    task = asyncio.create_task(CAPTAIN_QUEUE.put(ev))
                    CAPTAIN_ENQUEUE_TASKS.add(task)
                    task.add_done_callback(CAPTAIN_ENQUEUE_TASKS.discard)
                else:
                    logger.warning("[Captain] 记录队列已满 uid=%s room_id=%s", ev.uid, client.room_id)
        except Exception as e:
            logger.error("[Captain] 处理 USER_TOAST_V2 异常: %s", e)

async def _record_gift_ranking(uid: str, username: str, gift_count: int) -> None:
    async with async_session_factory() as session:
        try:
            stmt = insert(GiftRanking).values(
                user_uid=uid,
                username=username or None,
                gift_count=gift_count,
            )
            update_values = {
                "gift_count": GiftRanking.gift_count + gift_count,
            }
            if username:
                update_values["username"] = username
            stmt = stmt.on_duplicate_key_update(**update_values)
            await session.execute(stmt)
            await session.commit()
        except Exception as e:
            logger.error("[Gift] 写入 gift_ranking 失败 uid=%s: %s", uid, e)
            try:
                await session.rollback()
            except Exception:
                pass


def live_status_snapshot() -> Dict[str, Any]:
    room_id = ROOM_IDS[0] if ROOM_IDS else 0
    info = LIVE_INFO.get(room_id, {}) if room_id else {}
    return {
        "status": LAST_STATUS.get(room_id, 0) if room_id else 0,
        "live_time": info.get("live_time", "0000-00-00 00:00:00"),
        "title": info.get("title", ""),
    }

_tasks: list[asyncio.Task] = []


async def bootstrap() -> None:
    settings = get_settings()
    if not settings.bili_monitor_enabled:
        logger.info("[bili] BILI_MONITOR_ENABLED 未开启，跳过监听启动")
        return

    init_session()

    try:
        await init_uids_once()
    except Exception as e:
        logger.error("[bili] init_uids_once 失败: %s", e)

    loop = asyncio.get_running_loop()
    _tasks.extend(
        [
            loop.create_task(captain_writer_worker(), name="bili:cpt_writer"),
            loop.create_task(run_clients_loop(), name="bili:clients"),
            loop.create_task(monitor_all_rooms_status(), name="bili:live_status"),
            loop.create_task(reconnect_scheduler(), name="bili:reconnect"),
            loop.create_task(guard_report_scheduler(), name="bili:guard_report"),
        ]
    )
    logger.info("[bili] 后台监听已启动，tasks=%d", len(_tasks))


async def shutdown() -> None:
    tasks = list(_tasks)
    writer = next((t for t in tasks if t.get_name() == "bili:cpt_writer"), None)
    producers = [t for t in tasks if t is not writer]
    for task in producers:
        task.cancel()
    _tasks.clear()
    await asyncio.gather(*producers, return_exceptions=True)

    # 关闭客户端
    for rid, client in list(ROOM_CLIENTS.items()):
        try:
            await client.stop_and_close()
        except Exception:
            pass
    ROOM_CLIENTS.clear()
    if writer:
        try:
            await asyncio.wait_for(drain_captain_queue(), timeout=12)
        except TimeoutError:
            logger.warning("[Captain] 关闭时写入队列仍有待处理事件")
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
    if _dm_tasks:
        await asyncio.gather(*list(_dm_tasks), return_exceptions=True)
    if GUARD_EVENT_TASKS:
        await asyncio.gather(*list(GUARD_EVENT_TASKS))

    global aiohttp_session
    if aiohttp_session:
        try:
            await aiohttp_session.close()
        except Exception:
            pass
        aiohttp_session = None
