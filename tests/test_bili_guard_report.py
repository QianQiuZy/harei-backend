"""Offline guard-report regressions: no production settings, DB, SMTP or cookies."""

import asyncio
import datetime as dt
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import types
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from redis.asyncio import Redis


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def hset(self, key, field, value):
        self.commands.append((key, field, value))

    def expire(self, key, ttl):
        self.redis.ttls[key] = ttl

    async def execute(self):
        for key, field, value in self.commands:
            self.redis.hashes.setdefault(key, {})[field] = value


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {}
        self.ttls = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def pipeline(self, transaction=True):
        assert transaction
        return FakePipeline(self)


@pytest.fixture
def listener(monkeypatch, tmp_path):
    # The real db.session evaluates Settings at import time. Replace only that
    # boundary so the real listener/model/Redis code can load without .env reads.
    db = types.ModuleType("app.db.session")
    db.async_session_factory = Mock(side_effect=AssertionError("Unexpected DB access"))
    monkeypatch.setitem(sys.modules, "app.db.session", db)
    path = Path(__file__).parents[1] / "app/services/bili_captain_listener.py"
    spec = importlib.util.spec_from_file_location("guard_listener_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.GUARD_REPORT_ROOT = tmp_path
    module.test_redis = FakeRedis()
    module.get_redis_client = AsyncMock(return_value=module.test_redis)
    module._collect_monthly_captain_rows = AsyncMock(return_value=[])
    module.original_send_guard_report_email = module._send_guard_report_email
    module._send_guard_report_email = Mock()
    return module


def row(uid, name=None, level="舰长"):
    return {"uid": str(uid), "name": name or f"user{uid}", "level": level}


def raw(uid, level=3):
    return {"uinfo": {"uid": uid, "base": {"name": f"user{uid}"}, "guard": {"level": level}}}


def payload(items=(), top3=(), total=0, pages=1, page=1):
    return {"code": 0, "data": {"info": {"num": total, "page": pages, "now": page},
                               "list": list(items), "top3": list(top3)}}


class Response:
    def __init__(self, body=None, status=200, error=None):
        self.status = status
        self.body = body
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.body


@pytest.mark.parametrize("now, expected", [
    (dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 30, 23, 55)),
    (dt.datetime(2026, 10, 30, 23, 55), dt.datetime(2026, 10, 30, 23, 55)),
    (dt.datetime(2026, 10, 30, 23, 55, 1), dt.datetime(2026, 10, 31, 23, 55)),
    (dt.datetime(2026, 10, 31, 23, 55, 1), dt.datetime(2026, 11, 30, 23, 55)),
    (dt.datetime(2026, 4, 1), dt.datetime(2026, 4, 30, 23, 55)),
    (dt.datetime(2026, 2, 1), dt.datetime(2026, 2, 28, 23, 55)),
    (dt.datetime(2028, 2, 1), dt.datetime(2028, 2, 29, 23, 55)),
    (dt.datetime(2026, 12, 31, 23, 56), dt.datetime(2027, 1, 30, 23, 55)),
])
def test_snapshot_calendar(listener, now, expected):
    assert listener._next_guard_snapshot_time(now) == expected


def test_report_calendar_and_dedupe(listener):
    assert listener._guard_snapshot_target_for_month("202610") == dt.datetime(2026, 10, 31, 23, 55)
    assert listener._report_month_for_target(dt.datetime(2027, 1, 1, 0, 1)) == "202612"
    assert listener._next_month_report_time(dt.datetime(2026, 12, 1, 0, 1, 1)) == dt.datetime(2027, 1, 1, 0, 1)
    assert listener._merge_guard_rows([row(1)], [row("01", "new", "提督")]) == [row(1, "new", "提督")]


@pytest.mark.asyncio
async def test_top3_repeated_every_page_merged_once(listener, monkeypatch):
    bodies = [payload([raw(4), raw(5)], [raw(1, 1), raw(2, 2), raw(3)], 6, 2),
              payload([raw(6)], [raw(1, 1), raw(2, 2), raw(3)], 6, 2, 2)]
    listener.aiohttp_session = types.SimpleNamespace(get=Mock(side_effect=[Response(b) for b in bodies]))
    monkeypatch.setattr(listener.asyncio, "sleep", AsyncMock())
    rows = await listener._collect_guard_rows()
    assert len(rows) == 6
    assert rows[0] == row(1, level="总督")
    assert rows[1] == row(2, level="提督")
    assert [r["uid"] for r in rows] == [str(i) for i in range(1, 7)]
    assert listener.aiohttp_session.get.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [payload(), payload(top3=[raw(1), raw(2)], total=2)])
async def test_empty_and_top3_only_are_valid(listener, body):
    listener.aiohttp_session = types.SimpleNamespace(get=Mock(return_value=Response(body)))
    assert len(await listener._collect_guard_rows()) == body["data"]["info"]["num"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    Response(status=503), Response({"code": -352}), Response(error=asyncio.TimeoutError()),
    Response(error=aiohttp.ClientConnectionError()), Response("not a JSON object"),
    Response({"code": 0, "data": {}}), Response(payload([raw(0)], total=1)),
])
async def test_page_failure_is_not_empty_success(listener, monkeypatch, response):
    listener.aiohttp_session = types.SimpleNamespace(get=Mock(return_value=response))
    monkeypatch.setattr(listener.asyncio, "sleep", AsyncMock())
    with pytest.raises(listener.GuardSnapshotError):
        await listener._collect_guard_rows()
    assert listener.aiohttp_session.get.call_count == 3


@pytest.mark.asyncio
async def test_transient_failure_retries(listener, monkeypatch):
    listener.aiohttp_session = types.SimpleNamespace(get=Mock(side_effect=[
        Response(status=503), Response(payload(top3=[raw(1)], total=1))]))
    monkeypatch.setattr(listener.asyncio, "sleep", AsyncMock())
    assert await listener._collect_guard_rows() == [row(1)]


@pytest.mark.asyncio
async def test_partial_later_page_aborts(listener, monkeypatch):
    first = Response(payload([raw(4)], [raw(1), raw(2), raw(3)], 5, 2))
    listener.aiohttp_session = types.SimpleNamespace(get=Mock(side_effect=[first] + [Response(status=503)] * 3))
    monkeypatch.setattr(listener.asyncio, "sleep", AsyncMock())
    with pytest.raises(listener.GuardSnapshotError):
        await listener._collect_guard_rows()
    assert not list(listener.GUARD_REPORT_ROOT.iterdir())


@pytest.mark.asyncio
async def test_count_mismatch_never_saves_partial(listener, monkeypatch):
    listener.aiohttp_session = types.SimpleNamespace(get=Mock(return_value=Response(
        payload(top3=[raw(1)], total=2))))
    monkeypatch.setattr(listener.asyncio, "sleep", AsyncMock())
    with pytest.raises(listener.GuardSnapshotError, match="人数校验"):
        await listener._collect_guard_rows()


@pytest.mark.asyncio
async def test_total_changes_trigger_full_refetch(listener, monkeypatch):
    listener._fetch_guard_page = AsyncMock(side_effect=[
        listener.GuardPage([row(1)], 2, 2), listener.GuardPage([row(2)], 3, 2),
        listener.GuardPage([row(1), row(2), row(3)], 3, 1)])
    monkeypatch.setattr(listener.asyncio, "sleep", AsyncMock())
    assert len(await listener._collect_guard_rows()) == 3
    assert [c.args[0] for c in listener._fetch_guard_page.call_args_list] == [1, 2, 1]


@pytest.mark.asyncio
async def test_day30_redis_then_day31_union_and_restart(listener, monkeypatch):
    target = dt.datetime(2026, 10, 30, 23, 55)
    monkeypatch.setattr(listener, "_now", lambda: target)
    listener._collect_guard_rows = AsyncMock(return_value=[row(1), row(2)])
    assert await listener._snapshot_guard_report(target) is None
    assert not listener._guard_snapshot_path("202610").exists()
    assert listener.test_redis.ttls[listener._guard_cache_key("202610", "day30")] == 90 * 86400
    target = dt.datetime(2026, 10, 31, 23, 55)
    listener._collect_guard_rows.return_value = [row(2, "renamed", "提督"), row(3)]
    path = await listener._snapshot_guard_report(target)
    assert path.exists()
    assert listener._read_guard_snapshot("202610") == [row(1), row(2, "renamed", "提督"), row(3)]
    # Recovery depends on persisted data, not the previous process's counters.
    path.unlink()
    await listener._run_guard_report(dt.datetime(2026, 11, 1, 0, 1))
    assert path.exists()
    assert listener._send_guard_report_email.call_args.args[3] == 3
    await listener._run_guard_report(dt.datetime(2026, 11, 1, 0, 1))
    assert listener._send_guard_report_email.call_count == 1


@pytest.mark.asyncio
async def test_missing_day30_blocks_final(listener, monkeypatch):
    target = dt.datetime(2026, 10, 31, 23, 55)
    monkeypatch.setattr(listener, "_now", lambda: target)
    listener._collect_guard_rows = AsyncMock()
    with pytest.raises(listener.GuardSnapshotError, match="30 号"):
        await listener._snapshot_guard_report(target)
    listener._collect_guard_rows.assert_not_called()


@pytest.mark.asyncio
async def test_failed_collection_preserves_existing_file(listener, monkeypatch):
    target = dt.datetime(2026, 11, 30, 23, 55)
    monkeypatch.setattr(listener, "_now", lambda: target)
    path = listener._build_guard_report_xlsx([row(1)], "202611")
    contents = path.read_bytes()
    listener._collect_guard_rows = AsyncMock(side_effect=listener.GuardSnapshotError("failure"))
    with pytest.raises(listener.GuardSnapshotError):
        await listener._snapshot_guard_report(target)
    assert path.read_bytes() == contents
    assert await listener._load_guard_cache("202611", "final") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target, kind", [
    (dt.datetime(2026, 10, 30, 23, 55), "day30"),
    (dt.datetime(2026, 11, 30, 23, 55), "final"),
])
async def test_restart_reuses_completed_snapshot_without_refetch(listener, monkeypatch, target, kind):
    month = target.strftime("%Y%m")
    monkeypatch.setattr(listener, "_now", lambda: target + dt.timedelta(minutes=1))
    await listener._save_guard_cache(month, kind, [row(1)], target)
    listener._collect_guard_rows = AsyncMock(side_effect=AssertionError("Do not replace completed snapshot"))
    result = await listener._snapshot_guard_report(target)
    listener._collect_guard_rows.assert_not_called()
    assert await listener._load_guard_cache(month, kind) == [row(1)]
    if kind == "final":
        assert result.exists()
        assert listener._read_guard_snapshot(month) == [row(1)]
    else:
        assert result is None


def event(listener, when, uid=7, level="舰长"):
    return listener.CaptainEvent(str(uid), f"event{uid}", level, 1, False, when)


@pytest.mark.asyncio
async def test_late_events_during_fetch_and_after_file_before_send(listener, monkeypatch):
    target = dt.datetime(2026, 11, 30, 23, 55)
    monkeypatch.setattr(listener, "_now", lambda: target)

    async def collect():
        listener._queue_late_guard_event(event(listener, target + dt.timedelta(seconds=1)), listener.GUARD_REPORT_ROOM_ID)
        return [row(1)]

    listener._collect_guard_rows = collect
    await listener._snapshot_guard_report(target)
    listener._queue_late_guard_event(event(listener, target + dt.timedelta(minutes=4), uid=8), listener.GUARD_REPORT_ROOM_ID)
    listener._queue_late_guard_event(event(listener, target + dt.timedelta(minutes=4, seconds=1), level="提督"), listener.GUARD_REPORT_ROOM_ID)
    await listener._run_guard_report(dt.datetime(2026, 12, 1, 0, 1))
    assert listener._read_guard_snapshot("202611") == [row(1), row(7, "event7", "提督"), row(8, "event8")]
    assert listener._send_guard_report_email.call_args.args[3] == 3
    assert "202611" not in listener.GUARD_LATE_EVENTS


@pytest.mark.asyncio
async def test_event_window_room_isolation_and_redis_recovery(listener):
    boundary = dt.datetime(2026, 11, 30, 23, 55)
    listener._queue_late_guard_event(event(listener, boundary - dt.timedelta(seconds=1), 1), listener.GUARD_REPORT_ROOM_ID)
    listener._queue_late_guard_event(event(listener, boundary, 2), 999)
    listener._queue_late_guard_event(event(listener, dt.datetime(2026, 12, 1), 3), listener.GUARD_REPORT_ROOM_ID)
    listener._queue_late_guard_event(event(listener, boundary, 4), listener.GUARD_REPORT_ROOM_ID)
    # Submit out of chronological order; Redis completion order must not choose
    # an older level for the same user.
    listener._queue_late_guard_event(event(listener, boundary + dt.timedelta(seconds=2), 4, "提督"), listener.GUARD_REPORT_ROOM_ID)
    listener._queue_late_guard_event(event(listener, boundary + dt.timedelta(seconds=1), 4), listener.GUARD_REPORT_ROOM_ID)
    await asyncio.gather(*list(listener.GUARD_EVENT_TASKS))
    listener.GUARD_LATE_EVENTS.clear()
    assert await listener._late_guard_rows("202611") == [row(4, "event4", "提督")]


@pytest.mark.asyncio
async def test_missing_snapshot_never_fetches_next_month(listener):
    listener._collect_guard_rows = AsyncMock()
    with pytest.raises(listener.GuardSnapshotError, match="次月"):
        await listener._run_guard_report(dt.datetime(2026, 11, 1, 0, 1))
    listener._collect_guard_rows.assert_not_called()
    listener._send_guard_report_email.assert_not_called()


@pytest.mark.asyncio
async def test_file_only_restart_count_and_empty_snapshot(listener):
    listener._build_guard_report_xlsx([row(1), row(2)], "202611")
    await listener._run_guard_report(dt.datetime(2026, 12, 1, 0, 1))
    assert listener._send_guard_report_email.call_args.args[3] == 2
    listener._build_guard_report_xlsx([], "202612")
    await listener._run_guard_report(dt.datetime(2027, 1, 1, 0, 1))
    assert listener._send_guard_report_email.call_args.args[3] == 0


@pytest.mark.asyncio
async def test_cross_day_fetch_cannot_create_historical_snapshot(listener, monkeypatch):
    target = dt.datetime(2026, 11, 30, 23, 55)
    clock = [target]
    monkeypatch.setattr(listener, "_now", lambda: clock[0])

    async def slow_collect():
        clock[0] = dt.datetime(2026, 12, 1, 0, 2)
        return [row(1)]

    listener._collect_guard_rows = slow_collect
    with pytest.raises(listener.GuardSnapshotError, match="跨日"):
        await listener._snapshot_guard_report(target)
    assert not listener._guard_snapshot_path("202611").exists()
    with pytest.raises(listener.GuardSnapshotError, match="历史"):
        await listener._snapshot_guard_report(target)


@pytest.mark.asyncio
async def test_email_waits_for_snapshot_without_skipping_due_target(listener, monkeypatch):
    target = dt.datetime(2026, 11, 30, 23, 55)
    monkeypatch.setattr(listener, "_now", lambda: target)
    started, release = asyncio.Event(), asyncio.Event()

    async def collect():
        started.set()
        await release.wait()
        return [row(1)]

    listener._collect_guard_rows = collect
    snapshot = asyncio.create_task(listener._snapshot_guard_report(target))
    await started.wait()
    report = asyncio.create_task(listener._run_guard_report(dt.datetime(2026, 12, 1, 0, 1)))
    await asyncio.sleep(0)
    assert not report.done()
    release.set()
    await asyncio.gather(snapshot, report)
    assert listener._send_guard_report_email.call_args.args[2] == "202611"


@pytest.mark.asyncio
async def test_separate_scheduler_tasks_cancellation(listener):
    started_snapshot, started_email = asyncio.Event(), asyncio.Event()

    async def snapshot():
        started_snapshot.set()
        await asyncio.Event().wait()

    async def email():
        started_email.set()
        await asyncio.Event().wait()

    listener._guard_snapshot_scheduler = snapshot
    listener._guard_email_scheduler = email
    task = asyncio.create_task(listener.guard_report_scheduler())
    await asyncio.wait_for(asyncio.gather(started_snapshot.wait(), started_email.wait()), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_email_scheduler_keeps_overdue_target(listener, monkeypatch):
    clock = [dt.datetime(2026, 11, 30, 23, 55)]
    monkeypatch.setattr(listener, "_now", lambda: clock[0])
    targets = []

    async def sleep(seconds):
        clock[0] += dt.timedelta(seconds=seconds + 125)

    async def report(target):
        targets.append(target)
        if len(targets) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(listener.asyncio, "sleep", sleep)
    listener._run_guard_report = report
    with pytest.raises(asyncio.CancelledError):
        await listener._guard_email_scheduler()
    assert targets == [dt.datetime(2026, 12, 1, 0, 1), dt.datetime(2027, 1, 1, 0, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("now, expected", [
    (dt.datetime(2026, 10, 11, 15, 14), dt.datetime(2026, 11, 1, 0, 1)),
    (dt.datetime(2026, 10, 1, 0, 0, 59), dt.datetime(2026, 10, 1, 0, 1)),
    (dt.datetime(2026, 10, 1, 0, 1), dt.datetime(2026, 10, 1, 0, 1)),
    (dt.datetime(2026, 10, 1, 0, 1, 1), dt.datetime(2026, 11, 1, 0, 1)),
    (dt.datetime(2026, 12, 31, 23, 59), dt.datetime(2027, 1, 1, 0, 1)),
])
async def test_email_scheduler_restart_does_not_backfill(listener, monkeypatch, now, expected):
    monkeypatch.setattr(listener, "_now", lambda: now)
    # Cancel at the timer boundary; startup must first wait for the scheduled
    # target without inspecting old attachments, Redis markers, or sending mail.
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(listener.asyncio, "sleep", sleep)
    listener._run_guard_report = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await listener._guard_email_scheduler()
    sleep.assert_awaited_once_with((expected - now).total_seconds())
    listener._run_guard_report.assert_not_awaited()
    listener.get_redis_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_after_2355_catches_todays_snapshot(listener, monkeypatch):
    monkeypatch.setattr(listener, "_now", lambda: dt.datetime(2026, 10, 31, 23, 56))
    monkeypatch.setattr(listener.asyncio, "sleep", AsyncMock())
    listener._snapshot_guard_report = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await listener._guard_snapshot_scheduler()
    listener._snapshot_guard_report.assert_awaited_once_with(dt.datetime(2026, 10, 31, 23, 55))


@pytest.mark.asyncio
async def test_handler_captures_late_event_even_if_db_queue_is_full(listener, monkeypatch):
    monkeypatch.setattr(listener, "_now", lambda: dt.datetime(2026, 11, 30, 23, 59))
    listener.CAPTAIN_QUEUE = asyncio.Queue(maxsize=1)
    listener.CAPTAIN_QUEUE.put_nowait("already full")
    message = types.SimpleNamespace(uid=7, username="event7", guard_level=3, num=1, price=0)
    client = types.SimpleNamespace(room_id=listener.GUARD_REPORT_ROOM_ID)
    listener.MyHandler()._on_user_toast_v2(client, message)
    await asyncio.gather(*list(listener.GUARD_EVENT_TASKS))
    assert await listener._late_guard_rows("202611") == [row(7, "event7")]
    assert listener.CAPTAIN_QUEUE.qsize() == 1
    listener.CAPTAIN_QUEUE.get_nowait()
    listener.CAPTAIN_QUEUE.task_done()


@pytest.mark.asyncio
async def test_late_event_redis_failure_retains_memory_copy(listener):
    listener.get_redis_client = AsyncMock(side_effect=RuntimeError("Redis unavailable"))
    listener._queue_late_guard_event(event(listener, dt.datetime(2026, 11, 30, 23, 56)), listener.GUARD_REPORT_ROOM_ID)
    await asyncio.gather(*list(listener.GUARD_EVENT_TASKS))
    listener.get_redis_client = AsyncMock(return_value=listener.test_redis)
    assert await listener._late_guard_rows("202611") == [row(7, "event7")]


@pytest.mark.asyncio
async def test_failed_email_is_not_marked_sent(listener):
    listener._build_guard_report_xlsx([row(1)], "202611")
    listener._send_guard_report_email.side_effect = RuntimeError("SMTP unavailable")
    with pytest.raises(RuntimeError, match="SMTP"):
        await listener._run_guard_report(dt.datetime(2026, 12, 1, 0, 1))
    assert await listener.test_redis.get(listener._guard_cache_key("202611", "sent")) is None
    listener._send_guard_report_email.side_effect = None
    await listener._run_guard_report(dt.datetime(2026, 12, 1, 0, 1))
    assert await listener.test_redis.get(listener._guard_cache_key("202611", "sent")) == "1"


def test_smtp_message_counts_attachments_and_cc(listener, monkeypatch):
    from email import policy
    from email.parser import Parser

    settings = types.SimpleNamespace(smtp_host="smtp.example.invalid", smtp_port=465,
                                     smtp_user="", smtp_pass="", email_from="from@example.invalid",
                                     email_cc_list=["cc@example.invalid"])
    monkeypatch.setattr(listener, "get_settings", lambda: settings)
    server = Mock()
    context = Mock()
    context.__enter__ = Mock(return_value=server)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(listener.smtplib, "SMTP_SSL", Mock(return_value=context))
    guard = listener._build_guard_report_xlsx([row(1), row(2)], "202611")
    captain = listener.save_captains_xlsx([], "202611", listener.GUARD_REPORT_ROOT)
    listener.original_send_guard_report_email(guard, captain, "202611", 2, 0)
    sender, receivers, text = server.sendmail.call_args.args
    assert sender == settings.email_from
    assert receivers == [listener.GUARD_REPORT_RECEIVER, "cc@example.invalid"]
    msg = Parser(policy=policy.default).parsestr(text)
    assert "在舰列表人数：2 人" in msg.get_body().get_content()
    assert "上舰记录数量：0 条" in msg.get_body().get_content()
    attachments = list(msg.iter_attachments())
    assert [a.get_filename() for a in attachments] == [guard.name, captain.name]
    assert attachments[0].get_payload(decode=True) == guard.read_bytes()
    assert attachments[1].get_payload(decode=True) == captain.read_bytes()


def test_unconfigured_smtp_is_failure(listener, monkeypatch):
    monkeypatch.setattr(listener, "get_settings", lambda: types.SimpleNamespace(smtp_host="", email_from=""))
    with pytest.raises(listener.GuardSnapshotError, match="未发送"):
        listener.original_send_guard_report_email(Path("unused"), Path("unused"), "202611", 0, 0)


@pytest.mark.asyncio
async def test_real_redis_snapshot_and_event_roundtrip(listener, tmp_path):
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server is not installed")
    socket = tmp_path / "redis.sock"
    process = subprocess.Popen([binary, "--port", "0", "--unixsocket", str(socket),
                                "--save", "", "--appendonly", "no"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    redis = Redis(unix_socket_path=str(socket), decode_responses=True)
    try:
        for _ in range(50):
            if socket.exists():
                break
            await asyncio.sleep(0.02)
        await redis.ping()
        listener.get_redis_client = AsyncMock(return_value=redis)
        target = dt.datetime(2026, 10, 30, 23, 55)
        await listener._save_guard_cache("202610", "day30", [row(1)], target)
        assert await listener._load_guard_cache("202610", "day30") == [row(1)]
        assert await redis.ttl(listener._guard_cache_key("202610", "day30")) > 89 * 86400
        listener._queue_late_guard_event(event(listener, dt.datetime(2026, 10, 31, 23, 59)), listener.GUARD_REPORT_ROOM_ID)
        await asyncio.gather(*list(listener.GUARD_EVENT_TASKS))
        listener.GUARD_LATE_EVENTS.clear()
        assert await listener._late_guard_rows("202610") == [row(7, "event7")]
    finally:
        await redis.aclose()
        process.terminate()
        process.wait(timeout=5)
