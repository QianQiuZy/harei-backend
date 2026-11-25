import logging
import smtplib
import ssl
import time
from email.message import EmailMessage, Message
from email.utils import parseaddr
from uuid import uuid4

from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.redis import get_sync_rate_limit_redis

logger = logging.getLogger(__name__)
NOREPLY = "noreply@harei.cn"
MAIL_TPS = 20
MAIL_WINDOW_KEY = "harei:rate:mail:" + NOREPLY
# Redis time is shared by every worker. A recipient occupies a slot through SMTP
# submission and for one further second. A crashed sender releases its lease after 60s.
MAIL_SLOTS = """
local clock = redis.call('TIME')
local now = clock[1] * 1000000 + clock[2]
local count = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
if ARGV[1] == 'finish' then
    for i = 1, count do
        redis.call('ZADD', KEYS[1], 'XX', now + 1000000, ARGV[4] .. ':' .. i)
    end
    return 0
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local deficit = redis.call('ZCARD', KEYS[1]) + count - limit
if deficit > 0 then
    if ARGV[1] == 'priority' then redis.call('PSETEX', KEYS[2], 150, '1') end
    local next_free = redis.call('ZRANGE', KEYS[1], deficit - 1, deficit - 1, 'WITHSCORES')
    return math.min(100000, math.max(1000, tonumber(next_free[2]) - now))
end
if ARGV[1] == 'bulk' and redis.call('EXISTS', KEYS[2]) == 1 then return 50000 end
for i = 1, count do
    redis.call('ZADD', KEYS[1], now + 60000000, ARGV[4] .. ':' .. i)
end
redis.call('EXPIRE', KEYS[1], 61)
return 0
"""


def send_smtp_message(smtp, message, sender, recipients, *, login=None, priority=False):
    metered = any(
        parseaddr(address or "")[1].casefold() == NOREPLY for address in (sender, login)
    )
    if not metered:
        smtp.send_message(message, from_addr=sender, to_addrs=recipients)
        return
    redis = get_sync_rate_limit_redis()
    for start in range(0, len(recipients), MAIL_TPS):
        group = recipients[start : start + MAIL_TPS]
        args = (len(group), MAIL_TPS, uuid4().hex)
        keys = (MAIL_WINDOW_KEY, MAIL_WINDOW_KEY + ":priority")
        mode = "priority" if priority else "bulk"
        while delay := redis.eval(MAIL_SLOTS, 2, *keys, mode, *args):
            time.sleep(delay / 1_000_000)
        try:
            smtp.send_message(message, from_addr=sender, to_addrs=group)
        finally:
            try:
                redis.eval(MAIL_SLOTS, 2, *keys, "finish", *args)
            except RedisError:
                # The original lease remains conservative. Already submitted mail must not be retried.
                logger.warning("邮件投递限速槽将按租期释放")


def send_gift_message(
    settings: Settings,
    message: Message,
    recipients: list[str],
    *,
    sender: str | None = None,
    priority: bool = False,
) -> None:
    sender = sender or settings.gift_email_from
    host, port = settings.gift_smtp_host, settings.gift_smtp_port
    if not host or not sender:
        raise RuntimeError("舰礼发信服务尚未配置")
    password = settings.gift_smtp_pass.get_secret_value()
    if port != 465 and not settings.smtp_starttls and (
        password or not settings.local_mode
    ):
        raise RuntimeError("舰礼发信需要 TLS 加密连接")
    message["From"] = sender
    tls = ssl.create_default_context()
    if port == 465:
        connection = smtplib.SMTP_SSL(host, port, timeout=12, context=tls)
    else:
        connection = smtplib.SMTP(host, port, timeout=12)
    with connection as smtp:
        if port != 465 and settings.smtp_starttls:
            smtp.starttls(context=tls)
        if password:
            smtp.login(sender, password)
        send_smtp_message(smtp, message, sender, recipients, priority=priority)


def send_roster(month: str, attachment: bytes, *, settings: Settings):
    message = EmailMessage()
    recipient = settings.gift_roster_recipient
    message["To"] = recipient
    message["Subject"] = f"{month} 舰礼寄送名单"
    message.set_content(f"附件为 {month} 舰礼收集名单，按已填写和未填写分表。")
    filename = f"captain_addresses_{month}.xlsx"
    message.add_attachment(
        attachment,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
        params={"name": filename},
    )
    send_gift_message(settings, message, [recipient], sender=NOREPLY)
