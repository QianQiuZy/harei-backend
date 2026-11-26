"""Full account and fulfillment workflow against explicitly provisioned disposable local services."""

import asyncio
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def test_member_workflow():
    required = (
        "HAREI_QA_MYSQL_PORT",
        "HAREI_QA_REDIS_PORT",
        "HAREI_QA_SMTP_PORT",
        "HAREI_QA_MAIL_PORT",
    )
    if not all(os.environ.get(k) for k in required):
        pytest.skip(
            "set the four HAREI_QA_*_PORT values for disposable MySQL, Redis, SMTP and Mailpit"
        )
    subprocess.run([sys.executable, "-B", __file__, "run"], check=True)


async def workflow():
    import asyncmy
    import httpx
    from argon2 import PasswordHasher

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    database = "harei_qa_" + secrets.token_hex(6)
    port = int(os.environ["HAREI_QA_MYSQL_PORT"])
    mysql = await asyncmy.connect(
        host="127.0.0.1", port=port, user="root", autocommit=True
    )
    async with mysql.cursor() as cursor:
        await cursor.execute(f"CREATE DATABASE {database} CHARACTER SET utf8mb4")
        await cursor.execute(f"USE {database}")
        sql = (root / "db.sql").read_text()
        for stmt in sql[sql.index("CREATE TABLE") :].split(";"):
            if stmt.strip():
                await cursor.execute(stmt)
    mysql.close()
    roster_recipient = "shipping-test@harei.test"
    os.environ.update(
        GIFT_MAIL_RATE_REDIS_URL="",
        MYSQL_HOST="127.0.0.1",
        MYSQL_PORT=str(port),
        MYSQL_USER="root",
        MYSQL_PASSWORD="",
        MYSQL_DATABASE=database,
        MYSQL_POOL_SIZE="3",
        MYSQL_MAX_OVERFLOW="1",
        MYSQL_POOL_TIMEOUT="0.5",
        REDIS_URL="redis://127.0.0.1:" + os.environ["HAREI_QA_REDIS_PORT"] + "/0",
        GIFT_SMTP_HOST="127.0.0.1",
        GIFT_SMTP_PORT=os.environ["HAREI_QA_SMTP_PORT"],
        SMTP_STARTTLS="false",
        GIFT_SMTP_PASS="",
        GIFT_EMAIL_FROM="noreply@harei.cn",
        GIFT_ROSTER_RECIPIENT=roster_recipient,
        APP_ENV="test",
        APP_SECRET_KEY=secrets.token_urlsafe(40),
        GIFT_ADDRESS_KEY="",
        BILI_MONITOR_ENABLED="false",
        BILI_SESSDATA="",
        AUTH_USERNAME="test-admin",
        AUTH_PASSWORD_HASH=PasswordHasher().hash("admin-test-123"),
        MEMBER_SITE_URL="http://localhost:3000",
    )
    from sqlalchemy import event, select
    from contextlib import contextmanager

    from app.api import box, gift_admin
    from app.api import member as member_api
    from app.core.redis import (
        close_redis_client,
        get_rate_limit_redis,
        get_redis_client,
    )
    from app.db.session import async_session_factory, engine
    from app.deps import member as deps
    from app.main import app
    from app.models.captain_gift_archive import CaptainGiftArchive
    from app.models.member_gifts import (
        GiftAudit,
        GiftMonth,
        GiftNotice,
        GiftOrder,
        GiftSystem,
    )
    from app.services import (
        bili_captain_listener as listener,
    )
    from app.services import (
        gift_worker,
    )
    from app.services import (
        member_gifts as gifts,
    )
    from app.services import member_security as security
    from app.services.member_security import ROOM

    from app.core.redis import ACCOUNT_DB, ADDRESS_DB, PUBLIC_DB, get_session_redis
    from app.services.hot_cache import TTL, store
    from app.services.member_cache import account_key, alias_key, view_key

    @contextmanager
    def count_checkouts():
        checkouts = []

        def checkout(*_):
            checkouts.append(1)

        event.listen(engine.sync_engine, "checkout", checkout)
        try:
            yield checkouts
        finally:
            event.remove(engine.sync_engine, "checkout", checkout)

    clock = datetime(2032, 1, 1, 10)

    def now():
        return clock

    gifts.now_cn = deps.now_cn = gift_admin.now_cn = gift_worker.now_cn = now
    member_api.now_cn = now
    month = "203112"
    alice, bob, late, absent = (
        "3490000000000001",
        "3490000000000002",
        "3490000000000003",
        "3490000000000004",
    )
    prefix = secrets.token_hex(5)
    a_mail, b_mail = prefix + "a@harei.test", prefix + "b@harei.test"
    pw = "sample-password-42"
    redis = await get_redis_client()
    # Existing live limits retain their contents and expiration when moved from the configured DB.
    legacy = "rate_limit:box:uploads:" + prefix
    await redis.zadd(legacy, {"old": 100})
    await redis.expire(legacy, 90)
    await redis.set(legacy + ":seq", 1, ex=90)
    limits = await get_rate_limit_redis()
    assert limits.connection_pool.connection_kwargs["db"] == 2
    assert await redis.exists(legacy, legacy + ":seq") == 0
    assert await limits.zcard(legacy) == 1 and await limits.get(legacy + ":seq") == "1"
    assert 0 < await limits.ttl(legacy) <= 90
    legacy_session = deps.session_key("legacy-" + prefix)
    await redis.set(legacy_session, "1:1", ex=140)
    sessions = await get_session_redis()
    assert sessions.connection_pool.connection_kwargs["db"] == 5
    assert await sessions.get(legacy_session) == "1:1" and not await redis.exists(
        legacy_session
    )
    assert 0 < await sessions.ttl(legacy_session) <= 140

    qa_mail = "http://127.0.0.1:" + os.environ["HAREI_QA_MAIL_PORT"]
    transport = httpx.ASGITransport(app=app)

    def client():
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost:6555",
            headers={"X-Harei-Request": "1", "Origin": "http://localhost:3000"},
        )

    async with (
        client() as a,
        client() as b,
        client() as admin,
        httpx.AsyncClient(base_url=qa_mail, trust_env=False) as inbox,
    ):

        async def req(c, method, url, data=None, expect=200):
            kwargs = {"json": data} if data is not None else {}
            res = await c.request(method, url, **kwargs)
            assert res.status_code == expect, (
                method,
                url,
                res.status_code,
                res.text[:300],
            )
            return (
                res.json()
                if "json" in res.headers.get("content-type", "")
                else res.content
            )

        async def email_message(recipient, subject=None):
            items = (await inbox.get("/api/v1/messages", params={"limit": 150})).json()[
                "messages"
            ]
            item = next(
                m
                for m in items
                if any(t["Address"] == recipient for t in m["To"])
                and (not subject or subject in m["Subject"])
            )
            assert item["From"]["Address"] == "noreply@harei.cn"
            assert [to["Address"] for to in item["To"]] == [recipient]
            return (await inbox.get("/api/v1/message/" + item["ID"])).json()["Text"]

        async def email_code(recipient, operation):
            body = await email_message(recipient, f"harei.cn {operation}验证码")
            first_line, expiry = body.strip().splitlines()
            match = re.fullmatch(
                r"([0-9]{6}) 是harei\.cn" + re.escape(operation) + "的验证码",
                first_line,
            )
            assert match and expiry == "有效期10分钟"
            return match[1]

        async def check_code_reservations():
            with patch.object(security.secrets, "randbelow", return_value=1234):
                assert security.random_dm_code() == "00001234"
            first = await security.new_flow(
                redis, kind="register", owner="", identity=prefix + "1"
            )
            second = await security.new_flow(
                redis, kind="register", owner="", identity=prefix + "2"
            )
            candidates = set()
            while len(candidates) < 3:
                candidates.add(security.random_dm_code())
            candidate, alternate, replacement = sorted(candidates)
            with patch.object(
                security,
                "random_dm_code",
                side_effect=[candidate, candidate, alternate],
            ):
                codes = await asyncio.gather(
                    security.issue_dm(redis, first), security.issue_dm(redis, second)
                )
            assert set(codes) == {candidate, alternate}
            same_identity = await security.new_flow(
                redis, kind="register", owner="", identity=prefix + "1"
            )
            response = await a.post(
                "/user-auth/uid-challenge", json={"flow": same_identity}
            )
            assert (
                response.status_code == 429 and int(response.headers["retry-after"]) > 0
            )
            assert await security.accept_dm(redis, ROOM, alice, "甲", codes[0])
            assert not await security.accept_dm(redis, ROOM, bob, "乙", codes[0])
            await redis.delete(security.resend_key("register", prefix + "1"))
            with patch.object(
                security,
                "random_dm_code",
                side_effect=[codes[1], replacement],
            ):
                rotated = await security.issue_dm(redis, first)
            assert rotated not in codes
            assert not await security.accept_dm(redis, ROOM, alice, "甲", codes[0])
            status = await security.dm_status(redis, first)
            assert (
                not status["verified"]
                and status["uid"] is None
                and status["username"] is None
            )
            assert 0 < await redis.ttl(security.dm_key(codes[0])) <= 600
            assert await security.accept_dm(redis, ROOM, bob, "乙", rotated)
            assert (await security.dm_status(redis, first))["uid"] == bob
            status_path = "/user-auth/verification-status"
            plain_headers = {"Content-Type": "text/plain; charset=UTF-8"}
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost:6555") as plain:
                response = await plain.post(status_path, content=first, headers={
                    **plain_headers, "Origin": "http://localhost:3000"
                })
                assert response.status_code == 200 and response.json()["uid"] == bob
                assert response.headers["cache-control"] == "private, no-store"
                for origin in (None, "https://invalid.test"):
                    headers = plain_headers | ({"Origin": origin} if origin else {})
                    assert (await plain.post(status_path, content=first, headers=headers)).status_code == 403
                allowed_plain = plain_headers | {"Origin": "http://localhost:3000"}
                assert (await plain.post(status_path, content="bad", headers=allowed_plain)).status_code == 422
                assert (await plain.post("/user-auth/uid-challenge", content=first, headers=allowed_plain)).status_code == 403
            # Once an expired code is reallocated, the old pending page must still show it as expired.
            await redis.delete(security.dm_key(codes[1]))
            third = await security.new_flow(
                redis, kind="register", owner="", identity=prefix + "3"
            )
            with patch.object(security, "random_dm_code", return_value=codes[1]):
                assert await security.issue_dm(redis, third) == codes[1]
            expired = await security.dm_status(redis, second)
            assert expired["code"] is None and expired["expires_in"] == 0

            legacy = await security.new_flow(
                redis, kind="register", owner="", identity=prefix + "legacy"
            )
            legacy_key = security.flow_key(legacy)
            old_code = "A1B2C3"
            proof_key = security.dm_key(old_code)
            await redis.set(proof_key, legacy_key, ex=600)
            await redis.hset(legacy_key, mapping={"code": old_code, "proof": proof_key, "verified": "0"})
            assert (await security.dm_status(redis, legacy))["code"] is None
            assert not await security.accept_dm(redis, ROOM, alice, "甲", old_code)
            await redis.hset(legacy_key, mapping={"verified": "1", "uid": alice})
            assert (await security.verified_flow(redis, legacy, "register", "", alice))["code"] == old_code

        await check_code_reservations()

        async def proof(c, flow, uid):
            info = await req(c, "POST", "/user-auth/uid-challenge", {"flow": flow})
            assert info["uid"] is None and info["username"] is None
            assert re.fullmatch(r"[0-9]{8}", info["code"])
            assert info["expires_in"] == 600 and 59 <= info["retry_after"] <= 60
            again = await c.post("/user-auth/uid-challenge", json={"flow": flow})
            assert (
                again.status_code == 429
                and 1 <= int(again.headers["retry-after"]) <= 60
            )

            def dm(room, sender, mirror=False, username="千秋紫莹"):
                listener.MyHandler()._on_danmaku(
                    SimpleNamespace(room_id=room),
                    SimpleNamespace(
                        uid=sender,
                        uname=username,
                        msg=info["code"],
                        dm_type=0,
                        is_mirror=mirror,
                    ),
                )

            dm(ROOM + 1, uid)
            dm(ROOM, uid, True)
            dm(ROOM, "0")
            await asyncio.gather(*list(listener._dm_tasks))
            status = await req(
                c, "POST", "/user-auth/verification-status", {"flow": flow}
            )
            assert not status["verified"] and status["code"] == info["code"]
            dm(ROOM, uid)
            await asyncio.gather(*list(listener._dm_tasks))
            dm(ROOM, "12", username="其他发送者")
            await asyncio.gather(*list(listener._dm_tasks))
            status = await req(
                c, "POST", "/user-auth/verification-status", {"flow": flow}
            )
            assert (
                status["verified"]
                and status["uid"] == uid
                and status["username"] == "千秋紫莹"
            )

        async def register(c, email, uid):
            await req(
                c,
                "POST",
                "/user-auth/email-code",
                {"email": email, "purpose": "register"},
            )
            again = await c.post(
                "/user-auth/email-code", json={"email": email, "purpose": "register"}
            )
            assert (
                again.status_code == 429
                and 1 <= int(again.headers["retry-after"]) <= 60
            )
            code = await email_code(email, "账号注册")
            flow = (
                await req(
                    c,
                    "POST",
                    "/user-auth/registration/email-verify",
                    {"email": email, "code": code},
                )
            )["flow"]
            await proof(c, flow, uid)
            registration = {"flow": flow, "uid": uid, "password": pw}
            await req(c, "POST", "/user-auth/register", registration, expect=422)
            mismatch = await req(
                c,
                "POST",
                "/user-auth/register",
                {**registration, "confirm_password": pw + "x"},
                expect=400,
            )
            assert mismatch["detail"] == "两次输入的密码不一致"
            registration["confirm_password"] = pw
            await req(
                c,
                "POST",
                "/user-auth/register",
                {**registration, "uid": "13"},
                expect=409,
            )
            await req(
                c,
                "POST",
                "/user-auth/register",
                registration,
            )
            await req(
                c,
                "POST",
                "/user-auth/register",
                registration,
                expect=409,
            )

        async def bind(c, uid):
            flow = (await req(c, "POST", "/me/binding-flow", {}))["flow"]
            # Move these independent binding operations beyond this test account's cooldown.
            owner = await redis.hget(security.flow_key(flow), "identity")
            await redis.delete(security.resend_key("bind", owner))
            await proof(c, flow, uid)
            await req(c, "POST", "/me/bind", {"flow": flow, "uid": uid})

        await req(b, "POST", "/me/binding-flow", {}, expect=401)
        await register(a, a_mail, alice)
        # Exercise the normal HTTP routes with an isolated client IP and a private SMTP stub.
        from zoneinfo import ZoneInfo
        from datetime import time as midnight_time
        from fastapi import HTTPException

        wall_time = datetime.now(ZoneInfo("Asia/Shanghai"))
        next_day = int(
            datetime.combine(
                wall_time.date() + timedelta(days=1),
                midnight_time.min,
                wall_time.tzinfo,
            ).timestamp()
        )
        clock_call = "tonumber(redis.call('TIME')[1])"
        limiter_ip = (
            "198.18." + str(int(prefix[:2], 16)) + "." + str(int(prefix[2:4], 16))
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(limiter_ip, 1234)),
            base_url="http://localhost",
            headers={"X-Harei-Request": "1"},
        ) as public:
            # The admin endpoints share a failure-only counter. Exercise concurrency,
            # a correct password at the limit, a second IP, and Beijing midnight.
            from app.core import rate_limit
            from app.services.auth_service import AuthService

            budget = "rate_limit:admin-login:" + limiter_ip
            valid = {"username": "test-admin", "password": "admin-test-123"}
            invalid = {"username": "absent-admin", "password": "invalid-test"}
            admin_session = await public.post("/login", json=valid)
            assert admin_session.status_code == 200
            assert admin_session.headers["cache-control"] == "private, no-store"
            verified = await public.get("/auth", headers={"Authorization": "Bearer " + admin_session.json()["token"]})
            assert verified.status_code == 200 and "no-store" in verified.headers["cache-control"]
            assert not await limits.exists(budget)
            for index in range(8):
                path = "/music-manage/login" if index % 2 else "/login"
                assert (await public.post(path, json=invalid)).status_code == 401
            assert (
                await public.post("/music-manage/login", json=valid)
            ).status_code == 200
            assert await limits.hget(budget, "count") == "8"
            burst = await asyncio.gather(
                *(
                    public.post(path, json=invalid)
                    for path in ("/login", "/music-manage/login") * 4
                )
            )
            assert sorted(r.status_code for r in burst) == [401] * 2 + [412] * 6
            assert await limits.hget(budget, "count") == "10"
            assert (
                not await redis.exists(budget)
                and await limits.expiretime(budget) == next_day
            )
            with patch.object(
                AuthService,
                "verify_credentials",
                side_effect=AssertionError("blocked before hashing"),
            ):
                for path in ("/login", "/music-manage/login"):
                    denied = await public.post(path, json=valid)
                    assert (
                        denied.status_code == 412
                        and int(denied.headers["retry-after"]) > 0
                    )
            assert (await admin.post("/login", json=valid)).status_code == 200
            after_midnight = rate_limit.ADMIN_LOGIN_WINDOW.replace(
                clock_call, str(next_day)
            )
            with patch.object(rate_limit, "ADMIN_LOGIN_WINDOW", after_midnight):
                assert (await public.post("/login", json=invalid)).status_code == 401
                assert await limits.hget(budget, "count") == "1"

            def submit(email, purpose="register"):
                return public.post(
                    "/user-auth/email-code", json={"email": email, "purpose": purpose}
                )

            await limits.delete(
                security.PREFIX + "limit:" + security.digest("email-resend", a_mail)
            )
            with patch.object(
                security, "deliver", new_callable=AsyncMock
            ) as deliveries:
                assert (await submit(a_mail, "reset")).status_code == 200
                assert (await submit(a_mail)).status_code == 429
                result = await asyncio.gather(
                    *(submit(f"{prefix}-{n}@harei.test") for n in range(11))
                )
                assert sorted(r.status_code for r in result) == [200] * 9 + [429] * 2
                assert deliveries.await_count == 10
                key = "rate_limit:mail:ip:" + security.digest("ip", limiter_ip)
                assert await limits.hget(
                    key, "count"
                ) == "10" and not await redis.exists(key)
                assert await limits.expiretime(key) == next_day
                before = security.EMAIL_LIMIT_SCRIPT.replace(
                    clock_call, str(next_day - 1)
                )
                with patch.object(security, "EMAIL_LIMIT_SCRIPT", before):
                    rejected = await submit(prefix + "unknown@harei.test", "reset")
                    assert (
                        rejected.status_code == 429
                        and rejected.headers["retry-after"] == "1"
                    )
                after = security.EMAIL_LIMIT_SCRIPT.replace(clock_call, str(next_day))
                with patch.object(security, "EMAIL_LIMIT_SCRIPT", after):
                    assert (
                        await submit(prefix + "midnight@harei.test")
                    ).status_code == 200
                assert deliveries.await_count == 11
            response = await public.post("/box/uploads", data={"message": "测试"})
            assert response.status_code == 400
            box_key = "rate_limit:box:uploads:" + limiter_ip
            assert await limits.zcard(box_key) == 1 and not await redis.exists(box_key)

        from redis.exceptions import ConnectionError as RedisConnectionError

        async def unavailable_limits():
            raise RedisConnectionError("temporary QA outage")

        app.dependency_overrides[get_rate_limit_redis] = unavailable_limits
        try:
            with patch.object(
                security, "deliver", new_callable=AsyncMock
            ) as deliveries:
                offline = await a.post(
                    "/user-auth/email-code",
                    json={
                        "email": prefix + "offline@harei.test",
                        "purpose": "register",
                    },
                )
                assert offline.status_code == 503 and offline.headers["retry-after"]
                deliveries.assert_not_awaited()
        finally:
            del app.dependency_overrides[get_rate_limit_redis]

        async def box_at(ip, stamp, retry=None):
            with patch.object(
                box,
                "RATE_LIMIT_SCRIPT",
                box.RATE_LIMIT_SCRIPT.replace(clock_call, str(stamp)),
            ):
                if retry is None:
                    await box._enforce_upload_rate_limit(limits, ip)
                else:
                    with pytest.raises(HTTPException) as caught:
                        await box._enforce_upload_rate_limit(limits, ip)
                    assert caught.value.status_code == 429
                    assert int(caught.value.headers["Retry-After"]) == retry - stamp
                    assert caught.value.detail["retry_at"] == retry

        for delta in (-11000, -10960, -10920, -3800, -120):
            await box_at("daily-" + prefix, next_day + delta)
        await box_at("daily-" + prefix, next_day - 1, next_day)
        await box_at("daily-" + prefix, next_day)
        await box_at("daily-" + prefix, next_day + 5, next_day + 30)
        for delta in (-120, -80, -40):
            await box_at("hourly-" + prefix, next_day + delta)
        await box_at("hourly-" + prefix, next_day, next_day + 3480)

        await bind(a, bob)
        assert [
            item["username"] for item in (await req(a, "GET", "/me"))["bindings"]
        ] == ["千秋紫莹", "千秋紫莹"]
        await req(b, "GET", "/me", expect=401)
        assert "no-store" in (await b.get("/me")).headers["cache-control"]
        assert (
            await a.post(
                "/me/deletion",
                json={"password": pw},
                headers={"Origin": "https://invalid.test"},
            )
        ).status_code == 403
        address = {
            "name": "测试甲",
            "phone": "13800000001",
            "address": "测试省测试市一号",
        }
        override = {**address, "name": "测试乙", "phone": "13800000002"}
        await req(a, "PUT", "/me/address", address)
        await req(a, "PUT", "/me/address/" + bob, override)
        login = await req(
            admin,
            "POST",
            "/login",
            {"username": "test-admin", "password": "admin-test-123"},
        )
        admin.headers["Authorization"] = "Bearer " + login["token"]
        accounts, addresses, catalog = [
            await store(n) for n in (ACCOUNT_DB, ADDRESS_DB, PUBLIC_DB)
        ]
        assert [
            s.redis.connection_pool.connection_kwargs["db"]
            for s in (accounts, addresses, catalog)
        ] == [3, 4, 6]
        owner_id = int(await accounts.redis.get(accounts.key(alias_key(a_mail))))
        account_cache_key = accounts.key(account_key(owner_id))
        address_cache_key = addresses.key(view_key(owner_id))
        assert await sessions.exists(
            deps.session_key(a.cookies.get(security.SESSION_COOKIE))
        )
        assert await redis.exists("token:" + login["token"])
        assert not await sessions.exists("token:" + login["token"])
        for cache, key in (
            (accounts, account_cache_key),
            (addresses, address_cache_key),
        ):
            raw = await cache.redis.get(key)
            assert raw and not any(
                v in raw
                for v in (
                    pw,
                    a_mail,
                    address["phone"],
                    address["address"],
                    "pbkdf2_sha256",
                )
            )
            assert TTL - 5 <= await cache.redis.ttl(key) <= TTL
        await addresses.redis.expire(address_cache_key, 30)
        await req(a, "PUT", "/me/address", override)
        assert await addresses.redis.ttl(address_cache_key) > TTL - 5
        with count_checkouts() as used:
            assert (await req(a, "GET", "/me"))["address"] == override
            assert (await req(a, "GET", "/me/account"))["email"] == a_mail
            async with client() as side:
                await req(
                    side,
                    "POST",
                    "/user-auth/login",
                    {"identifier": alice, "password": pw},
                )
            assert not used, len(used)
        await req(a, "PUT", "/me/address", address)
        await addresses.redis.delete(address_cache_key)
        with count_checkouts() as used:
            assert (await req(a, "GET", "/me"))["address"] == address
            assert used
        await req(b, "GET", "/music")
        await req(b, "GET", "/tag/active")
        with count_checkouts() as used:
            await asyncio.gather(*(req(b, "GET", "/music") for _ in range(24)))
            assert not used
        with count_checkouts() as used:
            await asyncio.gather(*(req(b, "GET", "/music?page=2") for _ in range(16)))
            assert len(used) == 1
        # Reserve every slot in the deliberately small QA pool. Hot reads still
        # work; a cold SQL read returns promptly and recovers once slots are released.
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as holders:
            for _ in range(4):
                await holders.enter_async_context(engine.connect())
            with count_checkouts() as used:
                await req(a, "GET", "/me")
                await req(b, "GET", "/music")
                full = await b.get("/huangdou/rank")
                assert full.status_code == 503 and full.headers["retry-after"]
                assert not used
        assert (await b.get("/huangdou/rank")).status_code == 200
        with patch(
            "app.services.hot_cache.get_cache_redis",
            side_effect=RedisConnectionError("QA cache offline"),
        ):
            with count_checkouts() as used:
                assert (await b.get("/music")).status_code == 200 and used
        public_keys = await catalog.redis.zrange(catalog.key("music:items"), 0, -1)
        await catalog.redis.delete(*public_keys)
        await req(b, "GET", "/music")
        song = {
            "title": "Cache QA",
            "artist": "QA",
            "artists": ["QA"],
            "genre": "QA",
            "language": "中文",
            "work_type": "原创",
        }
        created = await req(admin, "POST", "/music-manage/songs", song, expect=201)
        with count_checkouts() as used:
            listed = await b.get("/music")
            assert listed.json()["items"][0]["title"] == song["title"] and not used
            assert (
                await b.get("/music", headers={"If-None-Match": listed.headers["etag"]})
            ).status_code == 304
            assert not used
        detail_url = "/music/" + created["source_key"]
        await req(b, "GET", detail_url)
        await req(b, "GET", "/music?q=Cache")
        await req(
            admin,
            "PUT",
            "/music-manage/songs/" + str(created["song_id"]),
            {"title": "Renamed", "version": 1},
        )
        with count_checkouts() as used:
            assert (await req(b, "GET", detail_url))["item"]["title"] == "Renamed"
            assert not (await req(b, "GET", "/music?q=Cache"))["items"] and not used
        assert (
            await b.get("/music", headers={"If-None-Match": listed.headers["etag"]})
        ).status_code == 200
        # Lost acknowledgement after MySQL commit leaves a miss; rollback of an
        # invalidation must never publish the proposed address.
        from app.db import session as db_layer
        from app.models.member_gifts import Member

        with patch.object(db_layer, "publish", new_callable=AsyncMock):
            await req(a, "PUT", "/me/address", override)
        assert (await addresses.redis.get(address_cache_key)).startswith("!")
        assert (await req(a, "GET", "/me"))["address"] == override
        async with async_session_factory() as db:
            await gifts.lock_system(db)
            (await db.get(Member, owner_id)).address = security.seal(address)
            await db_layer.prepare_commit(db)
            await db.rollback()
        assert (await req(a, "GET", "/me"))["address"] == override
        await req(a, "PUT", "/me/address", address)
        from tempfile import TemporaryDirectory
        from app.api import download as download_api

        with (
            TemporaryDirectory() as temp,
            patch.object(download_api, "DOWNLOAD_ROOT", Path(temp)),
        ):
            for payload in ([], {"description": 7, "path": "https://example.test/"},
                            {"description": "测" * 256, "path": "https://example.test/"}):
                assert (await admin.post("/download/add", json=payload)).status_code == 422
            assert not list(Path(temp).iterdir())
            uploaded = await admin.post(
                "/download/add",
                data={"description": "测试文件"},
                files={"file": ("sample.txt", b"sample", "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            item = (await req(b, "GET", "/download/active"))["items"][0]
            assert item["path"] == "/download/file?download_id=" + str(
                uploaded.json()["download_id"]
            )
            document = await b.get(item["path"])
            assert document.content == b"sample" and document.headers[
                "content-disposition"
            ].startswith("attachment;")
            assert document.headers["x-content-type-options"] == "nosniff"
        too_large = await admin.post(
            "/music-manage/performances/import",
            content=b"",
            headers={"Content-Length": str(6 * 1024 * 1024 + 1)},
        )
        assert too_large.status_code == 413
        base = "/admin/member-gifts/months/" + month
        summary = "/admin/member-gifts/summary"
        from app.services.auth_service import AuthService

        music_token, _ = await AuthService(redis).issue_token(
            "test-music", ["music:manage"]
        )
        assert (
            await b.get(summary, headers={"Authorization": "Bearer " + music_token})
        ).status_code == 403
        assert (await req(admin, "GET", summary)) == {
            "archive": {"month": "203201", "uploaded": False},
            "collection": None,
        }
        assert (
            await b.post(
                base + "/email",
                json={},
                headers={"Authorization": "Bearer " + music_token},
            )
        ).status_code == 403
        async with async_session_factory.begin() as db:
            (await db.get(GiftSystem, 1)).first_month = month
            db.add_all([
                CaptainGiftArchive(gift_month="203201", image_path="203201_older.jpg"),
                CaptainGiftArchive(gift_month="203201", image_path="203201_test.jpg"),
            ])
        january_archives = [item for item in (await req(b, "GET", "/captaingift"))["items"]
                            if item["month"] == "203201"]
        assert january_archives == [{"month": "203201", "path": "203201_test.jpg"}]

        def toast(uid, level, price, when=datetime(2031, 12, 29), room=ROOM):
            pay = {"num": 1, "unit": "天" if price == 0 else "月"}
            if price is not None:
                pay["price"] = price
            packet = {
                "sender_uinfo": {"uid": int(uid), "base": {"name": "测试"}},
                "guard_info": {"guard_level": level, "start_time": 0, "end_time": 0},
                "pay_info": pay,
                "gift_info": {"gift_id": 10003, "price": 198000},
                "option": {"source": 0},
                "toast_msg": "",
            }
            with patch.object(listener, "_now", return_value=when):
                listener.MyHandler().handle(
                    SimpleNamespace(room_id=room),
                    {"cmd": "USER_TOAST_MSG_V2", "data": packet},
                )

        writer = asyncio.create_task(listener.captain_writer_worker())
        try:
            from app.models.member_gifts import PurchaseLevel

            # This pre-migration tier carries no price. It cannot prove a paid upgrade.
            async with async_session_factory.begin() as db:
                db.add(PurchaseLevel(room_id=ROOM, month=month, uid=bob, level=1))
            toast(bob, 1, 0)
            toast(bob, 1, 198000, room=ROOM + 1)
            toast(bob, 1, 198000, when=datetime(2031, 11, 30, 23, 59, 59))
            toast("99100", 3, 0, when=datetime(2031, 12, 31, 23, 59, 59))
            toast("99101", 3, None)
            await listener.drain_captain_queue()
            assert (await req(admin, "GET", base))["total"] == 0
            assert await listener._late_guard_rows(month) == []
            from app.models.captain import Captain

            async with async_session_factory() as db:
                blind_box = await db.scalar(
                    select(Captain).where(Captain.user_uid == "99100")
                )
                missing_price = await db.scalar(
                    select(Captain).where(Captain.user_uid == "99101")
                )
                assert (
                    blind_box.room_id,
                    blind_box.paid_price,
                    blind_box.legacy_assumed_paid,
                ) == (ROOM, 0, False)
                assert (
                    missing_price.room_id == ROOM and missing_price.paid_price is None
                )
            toast(alice, 3, 1900)
            await listener.drain_captain_queue()
            (g,) = (await req(a, "GET", "/me"))["gifts"]
            assert (g["tier"], g["roster_tier"], g["deadline"], g["state"]) == (
                "舰长",
                None,
                None,
                "open",
            )
            await req(a, "PUT", f"/me/gifts/{month}/{alice}/address", override)
            assert (await req(a, "GET", "/me"))["gifts"][0]["address"] == override
            with patch.object(
                listener,
                "grant_paid_purchase",
                side_effect=RuntimeError("temporary cache outage"),
            ):
                toast(alice, 2, 1998000)
                await listener.drain_captain_queue()
            await gift_worker.apply_pending_purchases()
            toast(alice, 3, 198000)
            toast(alice, 1, 0)
            await listener.drain_captain_queue()
            assert (await req(a, "GET", "/me"))["gifts"][0]["tier"] == "提督"
            assert (await req(admin, "GET", base))["total"] == 1
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        roster = [
            {"uid": uid, "name": "测试", "level": "舰长"} for uid in (bob, late, absent)
        ]
        # A legacy report with an unverified late event must never stand in for the API roster.
        await listener._save_guard_cache(
            month,
            "final",
            [*roster, {"uid": "99100", "name": "盲盒", "level": "舰长"}],
            datetime(2031, 12, 31, 23, 55),
        )
        await gift_worker.settle_due()
        assert (await req(admin, "GET", base))["total"] == 1
        from tempfile import TemporaryDirectory

        with (
            TemporaryDirectory() as snapshots,
            patch.object(listener, "GUARD_REPORT_ROOT", Path(snapshots)),
        ):
            await listener._save_guard_cache(
                month, "roster", roster, datetime(2031, 12, 31, 23, 55)
            )
            await redis.delete(listener._guard_cache_key(month, "roster"))
            await gift_worker.settle_due()
        await redis.set(listener._guard_cache_key(month, "sent"), "1", ex=60)
        await gift_worker.settle_due()
        await gift_worker.settle_due()
        verified = (await req(a, "GET", "/me"))["gifts"]
        assert [(g["uid"], g["tier"], g["roster_tier"]) for g in verified] == [
            (alice, "提督", None),
            (bob, "舰长", "舰长"),
        ]
        assert verified[0]["address"] == override and verified[0]["deadline"] is None
        assert (await req(admin, "GET", summary))["collection"] == {
            "month": month,
            "collected": 2,
            "missing": 2,
        }
        await req(a, "POST", "/me/unbind/" + alice, {"password": pw})
        await bind(a, alice)
        await req(admin, "PUT", base, {"date": "2032-01-04"})
        assert (await req(admin, "GET", summary)) == {
            "archive": {"month": "203201", "uploaded": True},
            "collection": {"month": month, "collected": 2, "missing": 2},
        }
        orders = (await req(a, "GET", "/me"))["gifts"]
        assert [(g["uid"], g["tier"]) for g in orders] == [
            (alice, "提督"),
            (bob, "舰长"),
        ]
        assert [g["address"] for g in orders] == [address, override]
        preference = "/me/address-preference"
        assert (await req(a, "GET", "/me"))["auto_fill_address"] is True
        await req(b, "PUT", preference, {"auto_fill_address": False}, expect=401)
        await req(a, "PUT", preference, {"auto_fill_address": False})
        profile = await req(a, "GET", "/me")
        assert (
            profile["address"] == address
            and profile["bindings"][1]["address"] == override
        )
        assert all(g["address"] is None for g in profile["gifts"])
        assert (await req(admin, "GET", base + "?kind=missing"))["total"] == 4
        assert (await req(admin, "GET", summary))["collection"]["collected"] == 0
        await req(a, "PUT", f"/me/gifts/{month}/{bob}/address", {"use_default": True})
        profile = await req(a, "GET", "/me")
        assert [g["address"] for g in profile["gifts"]] == [None, address]
        assert profile["bindings"][1]["address"] == override
        assert (await req(admin, "GET", base + "?kind=missing"))["total"] == 3
        await req(a, "PUT", preference, {"auto_fill_address": True})
        assert all(
            g["username"] == "千秋紫莹" and g["username"] != g["nickname"]
            for g in orders
        )
        await req(admin, "POST", f"{base}/{alice}/return-address", {}, expect=409)
        await req(
            a,
            "PUT",
            f"/me/gifts/{month}/{bob}/address",
            {**override, "name": "仅修改该 UID"},
        )
        edited = await req(a, "GET", "/me")
        assert (
            edited["address"] == address
            and edited["gifts"][1]["address"]["name"] == "仅修改该 UID"
        )
        await req(a, "PUT", f"/me/gifts/{month}/{bob}/address", override)

        assert all(g["collection_status"] == "已填写" for g in orders)
        await req(a, "PUT", "/me/address", None)
        await req(
            a,
            "PUT",
            f"/me/gifts/{month}/{alice}/address",
            {"use_default": True},
            expect=409,
        )
        opened = await req(admin, "GET", base)
        assert opened["schedule"]["phase"] == "open"
        assert {r["uid"]: r["collection_status"] for r in opened["items"]} == {
            alice: "收集中",
            bob: "已填写",
            late: "未注册",
            absent: "未注册",
        }
        assert all(
            not (
                {"email", "recipient", "phone", "address", "bound", "tracking"}
                & row.keys()
            )
            for row in opened["items"]
        )
        await req(a, "PUT", "/me/address", address)
        await req(admin, "GET", base + "/tracking-template", expect=409)
        await req(a, "POST", f"/me/gifts/{month}/{bob}", {"waive": True})
        assert (await req(admin, "GET", summary))["collection"] == {
            "month": month,
            "collected": 1,
            "missing": 2,
        }
        # A waived UID must not contribute its address to a shipping export before cutoff, either.
        async with async_session_factory() as db:
            batch = await db.get(GiftMonth, (security.ROOM, month))
            _, email_rows = await gifts.roster_snapshot(db, batch, for_email=True)
            waived = next(row for row in email_rows if row["uid"] == bob)
            assert waived["address"] is None and waived["collection_status"] == "已放弃"
        await req(a, "POST", f"/me/gifts/{month}/{bob}", {"waive": False})
        # Even an empty address book does not trigger reminders while auto-fill is on.
        await req(a, "PUT", "/me/address", None)
        clock = datetime(2032, 1, 3)
        await gift_worker.tick()
        async with async_session_factory() as db:
            assert (await db.scalars(select(GiftNotice))).all() == []

        await req(admin, "PUT", base, {"date": "2032-01-06"})
        await req(a, "PUT", preference, {"auto_fill_address": False})
        await req(a, "PUT", "/me/address", address)
        await req(admin, "PUT", base, {"date": "2032-01-04"})
        async with async_session_factory() as db:
            (notice,) = (await db.scalars(select(GiftNotice))).all()
            assert notice.uids == [alice] and notice.kind == "reminder"
        # Moving outside the window cancels an outstanding reminder with the old deadline.
        await req(admin, "PUT", base, {"date": "2032-01-06"})
        async with async_session_factory() as db:
            assert (await db.scalars(select(GiftNotice))).all() == []
        await req(admin, "PUT", base, {"date": "2032-01-04"})
        await gift_worker.send_pending()
        reminder = await email_message(a_mail, "请填写收件地址")
        assert alice in reminder and bob not in reminder
        assert "2032-01-05 00:00" in reminder and "尚未提交" in reminder

        await req(admin, "PUT", base, {"date": "2032-01-05"})
        async with async_session_factory.begin() as db:
            (sent,) = (await db.scalars(select(GiftNotice))).all()
            assert sent.sent and sent.kind == "reminder"
            # An old-version change notification already in the queue must also be suppressed.
            batch = await db.get(GiftMonth, (ROOM, month))
            db.add(
                GiftNotice(
                    month=month,
                    member_id=owner_id,
                    revision=batch.revision,
                    kind="rescheduled",
                    uids=[alice],
                    deadline=batch.deadline,
                    next_try=clock,
                )
            )
        with patch.object(gift_worker, "deliver", new_callable=AsyncMock) as delivery:
            await gift_worker.send_pending()
            clock = datetime(2032, 1, 4)
            await gift_worker.tick()
            await gift_worker.send_pending()
            delivery.assert_not_awaited()
        async with async_session_factory() as db:
            (sent,) = (await db.scalars(select(GiftNotice))).all()
            assert sent.sent and sent.uids == [alice]
        await req(a, "PUT", preference, {"auto_fill_address": True})
        clock = datetime(2032, 1, 6)
        await req(a, "PUT", "/me/address", {**address, "name": "新地址"})
        await req(a, "PUT", f"/me/gifts/{month}/{alice}/address", override, expect=409)
        data = (await req(admin, "GET", base))["items"]
        assert {g["uid"] for g in data if g["can_return_address"]} == {alice, bob}
        await req(admin, "POST", f"{base}/{absent}/return-address", {}, expect=409)
        assert (
            sum(r["state"] == "ready" for r in data) == 2
            and sum(r["state"] == "grace" for r in data) == 2
        )
        assert all(
            key not in row
            for row in data
            for key in ("address", "returned_address", "email", "phone")
        )
        assert address["phone"] not in str(data)
        assert (await admin.get("/me")).status_code == 401
        invalid = await a.put(
            "/me/address", json={**address, "phone": "private-test-phone"}
        )
        assert invalid.status_code == 422 and "private-test-phone" not in invalid.text
        async with async_session_factory.begin() as db:
            gifts.audit(
                db,
                "test-admin",
                "correct_address",
                month + "/" + alice,
                old=address,
                new=override,
            )
        history = await req(admin, "GET", "/admin/member-gifts/audit?month=" + month)
        assert (
            next(e for e in history["items"] if e["action"] == "correct_address")[
                "details"
            ]
            == {}
        )
        assert address["phone"] not in str(history) and override["phone"] not in str(
            history
        )
        assert (await req(admin, "GET", summary))["collection"]["missing"] == 2
        from email import policy
        from email.parser import BytesParser
        from io import BytesIO

        from openpyxl import load_workbook

        from app.services.gift_workbook import tracking_workbook

        async def upload(contents, status=200):
            result = await admin.post(
                base + "/tracking-import", files={"file": ("tracking.xlsx", contents)}
            )
            assert result.status_code == status, (status, result.text)
            return result.json()

        async def emailed_workbook():
            result = await req(
                admin, "POST", base + "/email?recipient=other@harei.test", {}
            )
            assert result == {"recipient": roster_recipient, "count": 4}
            async with async_session_factory() as db:
                delivery = await db.scalar(
                    select(GiftAudit)
                    .where(GiftAudit.subject == month, GiftAudit.action == "export")
                    .order_by(GiftAudit.id.desc())
                    .limit(1)
                )
                assert delivery.details["recipient"] == roster_recipient
            listing = (
                await inbox.get("/api/v1/messages", params={"limit": 150})
            ).json()["messages"]
            message = next(
                m for m in listing if m["Subject"] == month + " 舰礼寄送名单"
            )
            raw = (await inbox.get("/api/v1/message/" + message["ID"] + "/raw")).content
            mime = BytesParser(policy=policy.default).parsebytes(raw)
            assert (
                str(mime["From"]) == "noreply@harei.cn"
                and str(mime["To"]) == roster_recipient
            )
            assert mime["Cc"] is None and mime["Bcc"] is None
            (attachment,) = mime.iter_attachments()
            filename = f"captain_addresses_{month}.xlsx"
            assert attachment.get_filename() == filename
            assert attachment.get_param("name") == filename
            assert attachment.get_content_disposition() == "attachment"
            assert (
                attachment.get_content_type()
                == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            return load_workbook(BytesIO(attachment.get_payload(decode=True)))

        info = await req(admin, "GET", base)
        assert info["roster_recipient"] == roster_recipient
        assert info["schedule"]["phase"] == "grace"
        assert (
            info["schedule"]["refill_deadline"]
            == (clock + timedelta(hours=72)).isoformat()
        )
        await req(admin, "GET", base + "/tracking-template", expect=409)
        sample = tracking_workbook(month, [(alice, "测试", None)])
        await upload(sample, 409)
        await req(
            admin, "POST", base + "/email", {"to": "another@harei.test"}, expect=422
        )
        await req(admin, "GET", base + "/export", expect=404)
        exported = await emailed_workbook()
        assert exported.sheetnames == ["已填写", "未填写"]
        filled, missing = exported.worksheets
        for sheet in (filled, missing):
            assert sheet.freeze_panes == "C2" and sheet.auto_filter.ref
            assert sheet["A1"].font.bold and sheet.column_dimensions["J"].width >= 45
            assert sheet.row_dimensions[2].height >= 24
        assert {r[1]: r[7] for r in list(filled.values)[1:]} == {
            alice: address["name"],
            bob: override["name"],
        }
        assert all(r[6] == "已截止" for r in list(filled.values)[1:])
        assert [(r[1], r[4], r[5], r[6]) for r in list(missing.values)[1:]] == [
            (late, "未注册", None, "未注册"),
            (absent, "未注册", None, "未注册"),
        ]
        exported.close()
        # A frozen address can be returned in the normal monthly grace window.
        assert (await req(a, "GET", "/me"))["gifts"][0]["address"] == address
        await req(
            admin,
            "PUT",
            base + "/" + alice + "/address",
            {**override, "reason": "用户申请"},
            expect=404,
        )
        assert (await req(a, "GET", "/me"))["gifts"][0]["address"] == address
        await req(admin, "POST", base + "/extend", {"uid": bob, "date": "2032-01-07"})
        info = await req(admin, "GET", base)
        assert info["schedule"]["next_change"] == "2032-01-08T00:00:00"
        assert info["schedule"]["refill_deadline"] == "2032-01-09T00:00:00"
        await req(admin, "POST", f"{base}/{alice}/return-address", {})
        assert (await req(admin, "GET", base))["needs_resend"]
        with patch.object(
            gift_admin, "send_roster", side_effect=OSError("synthetic SMTP error")
        ):
            await req(admin, "POST", base + "/email", {}, expect=502)
        assert (await req(admin, "GET", base))["needs_resend"]
        returned_book = await emailed_workbook()
        returned_row = next(r for r in returned_book["未填写"].values if r[1] == alice)
        assert returned_row[6] == "修改中" and returned_row[7:10] == (None, None, None)
        returned_book.close()
        personal_address = f"/me/gifts/{month}/{alice}/address"
        await req(a, "PUT", personal_address, override, expect=409)
        await req(a, "PUT", personal_address, {"use_default": True}, expect=409)
        pending = (await req(a, "GET", "/me"))["gifts"][0]
        assert pending["address"] is None and pending["returned_address"] == address
        await req(a, "PUT", personal_address, {**address, "confirm_refill": True})
        await req(a, "PUT", personal_address, {**override, "confirm_refill": True})
        saved = (await req(a, "GET", "/me"))["gifts"][0]
        assert saved["address"] == override and saved["returned_address"] is None
        assert saved["state"] == "grace" and saved["frozen_at"] is None
        assert saved["collection_status"] == "修改中"
        assert not (await req(admin, "GET", base))["needs_resend"]
        draft_book = await emailed_workbook()
        draft_row = next(r for r in draft_book["未填写"].values if r[1] == alice)
        assert draft_row[6] == "修改中" and draft_row[7:10] == (None, None, None)
        draft_book.close()
        # The unfrozen order follows an eligible UID even when it moves within the grace period.
        await bind(a, late)
        assert (await req(admin, "GET", base))["needs_resend"]
        refreshed = await emailed_workbook()
        refreshed.close()
        with patch.object(gifts, "reveal", side_effect=AssertionError("admin view decrypted an address")):
            assert not (await req(admin, "GET", base))["needs_resend"]
        await req(a, "PUT", "/me/address/" + late, override)
        assert (await req(admin, "GET", base))["needs_resend"]
        await req(a, "PUT", f"/me/gifts/{month}/{late}/address", {"use_default": True})
        await req(a, "POST", f"/me/gifts/{month}/{late}", {"waive": True})
        await req(a, "POST", "/me/unbind/" + late, {"password": pw})
        await register(b, b_mail, late)
        assert (await req(b, "GET", "/me"))["gifts"][0]["collection_status"] == "未填写"
        await req(b, "PUT", f"/me/gifts/{month}/{late}/address", override)
        assert (await req(b, "GET", "/me"))["address"] == override
        b_gift = (await req(b, "GET", "/me"))["gifts"][0]
        assert (
            b_gift["uid"] == late
            and not b_gift["waived"]
            and b_gift["state"] == "grace"
        )
        clock += timedelta(hours=73)
        await gift_worker.tick()
        closed = await req(admin, "GET", base)
        assert closed["schedule"]["phase"] == "closed" and closed["needs_resend"]
        frozen = (await req(a, "GET", "/me"))["gifts"][0]
        assert frozen["state"] == "ready" and frozen["address"] == override
        assert frozen["frozen_at"] == "2032-01-09T00:00:00"
        await req(
            a, "PUT", personal_address, {**address, "confirm_refill": True}, expect=409
        )
        await req(admin, "POST", f"{base}/{alice}/return-address", {}, expect=409)
        fresh = await emailed_workbook()
        assert fresh.active["H2"].value == override["name"]
        fresh.close()
        assert not (await req(admin, "GET", base))["needs_resend"]

        async def template():
            return load_workbook(
                BytesIO(await req(admin, "GET", base + "/tracking-template"))
            )

        def contents(book):
            result = BytesIO()
            book.save(result)
            return result.getvalue()

        book = await template()
        sheet = book.active
        assert [r[1] for r in list(sheet.values)[1:]] == [alice, bob, late]
        assert sheet.max_column == 5 and not (
            {"收件人", "手机", "完整地址"} & set(next(sheet.values))
        )
        assert all(
            address["phone"] not in str(cell.value) for row in sheet for cell in row
        )
        assert sheet["B2"].data_type == "s" and sheet["E2"].number_format == "@"
        sheet["E2"] = sheet["E3"] = "00012345678901234567"
        sheet["E4"] = "=1+1"
        await upload(contents(book), 422)
        sheet["E4"] = None
        sheet["B4"] = absent
        await upload(contents(book), 422)
        assert all(not g["tracking"] for g in (await req(a, "GET", "/me"))["gifts"])
        sheet["B4"] = late
        sheet["A4"] = "209912"
        await upload(contents(book), 422)
        sheet["A4"] = month
        sheet["B4"] = bob
        await upload(contents(book), 422)
        sheet["B4"] = late
        data = contents(book)
        assert await upload(data) == {"changed": 2, "unchanged": 1}
        assert await upload(data) == {"changed": 0, "unchanged": 3}
        assert all(
            g["tracking"] == "00012345678901234567" and g["state"] == "shipped"
            for g in (await req(a, "GET", "/me"))["gifts"]
        )
        sheet["E2"] = "alternative"
        await upload(contents(book), 409)
        book.close()
        book = await template()
        book.active["E2"] = None
        assert (await upload(contents(book)))["changed"] == 1
        book.close()
        states = {
            r["uid"]: r["state"] for r in (await req(admin, "GET", base))["items"]
        }
        assert states == {
            alice: "ready",
            bob: "shipped",
            late: "ready",
            absent: "overdue",
        }
        writer = asyncio.create_task(listener.captain_writer_worker())
        try:
            for tier, price in [(1, 0), (2, 1980000), (3, 198000), (2, 1980000)]:
                toast(bob, tier, price)
            await listener.drain_captain_queue()
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        shipped = next(
            g for g in (await req(a, "GET", "/me"))["gifts"] if g["uid"] == bob
        )
        assert shipped["needs_reship"] and shipped["tier"] == "提督"
        assert (
            shipped["state"] == "shipped"
            and shipped["tracking"] == "00012345678901234567"
        )
        assert shipped["frozen_at"] and shipped["address"] == override
        marked = await req(admin, "GET", base)
        assert (
            next(r for r in marked["items"] if r["uid"] == bob)["needs_reship"]
            and marked["needs_resend"]
        )
        assert [
            r["uid"] for r in (await req(admin, "GET", base + "?kind=reship"))["items"]
        ] == [bob]
        resend_book = await emailed_workbook()
        assert any(
            r[1] == bob and r[-1] == "需人工补寄" for r in resend_book.active.values
        )
        resend_book.close()
        book = await template()
        assert (await upload(contents(book)))["changed"] == 0
        assert next(
            g for g in (await req(a, "GET", "/me"))["gifts"] if g["uid"] == bob
        )["needs_reship"]
        book.active["E3"] = "reship002"
        assert (await upload(contents(book)))["changed"] == 1
        book.close()
        shipped = next(
            g for g in (await req(a, "GET", "/me"))["gifts"] if g["uid"] == bob
        )
        assert not shipped["needs_reship"] and shipped["tracking"] == "reship002"
        await bind(b, absent)
        expired = next(
            r for r in (await req(admin, "GET", base))["items"] if r["uid"] == absent
        )
        assert expired["collection_status"] == "未填写" and not expired["has_address"]
        assert (await req(admin, "GET", summary))["collection"] is None
        await req(
            admin, "POST", base + "/extend", {"uid": absent, "date": "2032-01-11"}
        )
        assert (
            next(
                r
                for r in (await req(admin, "GET", base))["items"]
                if r["uid"] == absent
            )["state"]
            == "grace"
        )
        reopened = next(
            r for r in (await req(b, "GET", "/me"))["gifts"] if r["uid"] == absent
        )
        assert reopened["has_address"] and reopened["state"] == "grace"
        assert (await req(admin, "GET", summary))["collection"] == {
            "month": month,
            "collected": 4,
            "missing": 0,
        }
        assert (await req(admin, "GET", base))["schedule"]["phase"] == "grace"
        await req(admin, "GET", base + "/tracking-template", expect=409)
        await upload(data, 409)
        await req(b, "PUT", "/me/address", None)
        extended = next(
            g for g in (await req(b, "GET", "/me"))["gifts"] if g["uid"] == absent
        )
        assert extended["collection_status"] == "延长收集中"
        await req(b, "PUT", "/me/address", override)
        assert (
            next(
                g for g in (await req(b, "GET", "/me"))["gifts"] if g["uid"] == absent
            )["collection_status"]
            == "已截止"
        )
        await req(admin, "POST", f"{base}/{absent}/return-address", {})
        assert not next(
            g for g in (await req(b, "GET", "/me"))["gifts"] if g["uid"] == absent
        )["has_address"]
        await req(b, "PUT", f"/me/gifts/{month}/{absent}/address", address, expect=409)
        await req(b, "POST", "/me/unbind/" + absent, {"password": pw})
        async with async_session_factory() as db:
            assert (await db.get(GiftOrder, (ROOM, month, absent))).address is None
        await req(a, "POST", "/me/deletion", {"password": pw}, expect=422)
        await limits.delete(
            security.PREFIX + "limit:" + security.digest("email-resend", a_mail)
        )
        await req(a, "POST", "/me/deletion-code", {})
        await req(a, "POST", "/me/deletion-code", {}, expect=429)
        deletion_code = await email_code(a_mail, "注销账号")
        await req(
            a,
            "POST",
            "/me/deletion",
            {"code": deletion_code, "confirmed": False},
            expect=422,
        )
        await req(
            a,
            "POST",
            "/me/deletion",
            {"code": f"{(int(deletion_code) + 1) % 1000000:06d}", "confirmed": True},
            expect=400,
        )
        assert (await req(a, "GET", "/me/account"))["email"] == a_mail
        await req(a, "POST", "/me/deletion", {"code": deletion_code, "confirmed": True})
        assert (await req(admin, "GET", summary))["collection"] == {
            "month": month,
            "collected": 3,
            "missing": 1,
        }
        await req(a, "GET", "/me", expect=401)
        # A new owner cannot see previously frozen shipments.
        from app.services.member_security import PREFIX, digest

        await bind(b, alice)
        assert [g["uid"] for g in (await req(b, "GET", "/me"))["gifts"]] == [late]
        await req(b, "POST", "/user-auth/logout", {})
        await req(b, "POST", "/user-auth/login", {"identifier": alice, "password": pw})
        appeal = await req(
            b,
            "POST",
            "/user-auth/appeals",
            {"contact": b_mail, "message": "请求核对我的测试绑定", "uid": alice},
        )
        items = (await req(admin, "GET", "/admin/member-gifts/appeals"))["items"]
        await req(
            admin,
            "POST",
            "/admin/member-gifts/appeals/" + str(items[0]["id"]),
            {"reply": "已经核对"},
        )
        assert (await req(a, "POST", "/user-auth/appeals/status", appeal))["closed"]
        for uid in (late, alice):
            await req(b, "POST", "/me/unbind/" + uid, {"password": pw})
        assert (await req(b, "GET", "/me"))["bindings"] == []
        # Remove only this test recipient's resend cooldown to advance past the 60-second wait.
        await limits.delete(PREFIX + "limit:" + digest("email-resend", b_mail))
        await req(
            b, "POST", "/user-auth/email-code", {"email": b_mail, "purpose": "reset"}
        )
        code = await email_code(b_mail, "重置密码")
        new_password = "reset-password-test"
        await req(
            b,
            "POST",
            "/user-auth/password-reset",
            {"email": b_mail, "code": code, "password": new_password},
        )
        await req(b, "GET", "/me", expect=401)
        await req(
            b,
            "POST",
            "/user-auth/login",
            {"identifier": b_mail, "password": new_password},
        )
        new_email = prefix + "new@harei.test"
        await limits.delete(PREFIX + "limit:" + digest("email-resend", b_mail))
        await req(
            b,
            "POST",
            "/me/email/new-code",
            {"flow": "x" * 40, "email": new_email},
            expect=410,
        )
        await req(b, "POST", "/me/email-code", {})
        await req(b, "POST", "/me/email-code", {}, expect=429)
        old_code = await email_code(b_mail, "换绑邮箱（验证原邮箱）")
        mail_items = (
            await inbox.get("/api/v1/messages", params={"limit": 150})
        ).json()["messages"]
        assert not any(
            to["Address"] == new_email for msg in mail_items for to in msg["To"]
        )
        await req(
            b,
            "POST",
            "/me/email/verify-old",
            {"code": f"{(int(old_code) + 1) % 1000000:06d}"},
            expect=400,
        )
        flow = (await req(b, "POST", "/me/email/verify-old", {"code": old_code}))[
            "flow"
        ]
        await req(b, "POST", "/me/email/verify-old", {"code": old_code}, expect=400)
        target = {"flow": flow, "email": new_email}
        async with client() as other:
            await req(
                other,
                "POST",
                "/user-auth/login",
                {"identifier": b_mail, "password": new_password},
            )
            await req(other, "POST", "/me/email/new-code", target, expect=410)
        await req(b, "POST", "/me/email/new-code", target)
        await req(b, "POST", "/me/email/new-code", target, expect=429)
        await req(
            b,
            "POST",
            "/me/email/new-code",
            {**target, "email": prefix + "other@harei.test"},
            expect=410,
        )
        new_code = await email_code(new_email, "换绑邮箱（验证新邮箱）")
        await req(
            b,
            "POST",
            "/me/email",
            {**target, "code": f"{(int(new_code) + 1) % 1000000:06d}"},
            expect=400,
        )
        await req(b, "POST", "/me/email", {**target, "code": new_code})
        await req(b, "GET", "/me", expect=401)
        await req(
            b,
            "POST",
            "/user-auth/login",
            {"identifier": new_email, "password": new_password},
        )
        with patch.object(security, "deliver", new_callable=AsyncMock) as deliveries:
            for step in range(8):
                await limits.delete(
                    PREFIX + "limit:" + digest("email-resend", new_email)
                )
                route = "/me/deletion-code" if step % 2 == 0 else "/me/email-code"
                assert (await b.post(route, json={})).status_code == 200
            await limits.delete(PREFIX + "limit:" + digest("email-resend", new_email))
            for route in ("/me/deletion-code", "/me/email-code"):
                denied = await b.post(route, json={})
                assert (
                    denied.status_code == 429
                    and 0 < int(denied.headers["retry-after"]) <= 86400
                )
            assert deliveries.await_count == 8
        async with async_session_factory() as db:
            row = await db.get(GiftOrder, (ROOM, month, alice))
            assert override["phone"] not in row.address and row.owner_id
        from app.models.member_gifts import Appeal

        list_month = "203202"
        async with async_session_factory.begin() as db:
            db.add(
                GiftMonth(
                    room_id=ROOM,
                    month=list_month,
                    settled=True,
                    deadline=datetime(2033, 1, 1),
                )
            )
            await db.flush()
            for n in range(55):
                db.add(
                    GiftOrder(
                        room_id=ROOM,
                        month=list_month,
                        uid=str(1234567890123400 + n),
                        nickname="仅末页%_O'Brien" if n == 54 else "舰礼用户",
                        level=3,
                        roster_level=3,
                    )
                )
            for n in range(11):
                db.add(
                    Appeal(
                        ticket=f"test-appeal-{n}",
                        contact="test@harei.test",
                        message="测试",
                        created_at=clock,
                        closed=n >= 6,
                    )
                )
        endpoint = "/admin/member-gifts/months/" + list_month
        first = await req(admin, "GET", endpoint)
        last = await req(admin, "GET", endpoint + "?page=99")
        assert (
            first["page_size"] == 50
            and first["total"] == 55
            and len(first["items"]) == 50
        )
        assert last["page"] == 2 and len(last["items"]) == 5
        for query in (
            "?q=仅末页",
            "?kind=missing&q=1234567890123454",
            "?q=%25_",
            "?q=O%27Brien",
        ):
            result = await req(admin, "GET", endpoint + query)
            assert (
                result["total"] == 1
                and result["items"][0]["nickname"] == "仅末页%_O'Brien"
            )
        assert (await req(admin, "GET", endpoint + "?kind=ready&q=仅末页"))[
            "total"
        ] == 0
        with patch.object(gift_admin, "send_roster") as delivery:
            assert (await req(admin, "POST", endpoint + "/email", {}))["count"] == 55
        entire = load_workbook(BytesIO(delivery.call_args.args[1]), read_only=True)
        assert entire["未填写"].max_row == 56
        entire.close()
        appeals_url = "/admin/member-gifts/appeals"
        open_list = await req(admin, "GET", appeals_url)
        assert (
            open_list["total"] == 6
            and open_list["page_size"] == 5
            and len(open_list["items"]) == 5
        )
        assert not any(a["closed"] for a in open_list["items"])
        closed_list = await req(admin, "GET", appeals_url + "?status=closed")
        assert closed_list["total"] == 6 and all(
            a["closed"] for a in closed_list["items"]
        )
        all_list = await req(admin, "GET", appeals_url + "?status=all&page=3")
        assert all_list["total"] == 12 and len(all_list["items"]) == 2
        remaining = (await req(admin, "GET", appeals_url + "?page=2"))["items"]
        await req(
            admin,
            "POST",
            appeals_url + "/" + str(remaining[0]["id"]),
            {"reply": "已处理"},
        )
        clamped = await req(admin, "GET", appeals_url + "?page=2")
        assert clamped["page"] == 1 and clamped["total"] == 5

        audit_month = "203104"
        async with async_session_factory.begin() as db:
            for i in range(207):
                gifts.audit(db, "test-admin", "tier", f"{audit_month}/{i}")
            gifts.audit(db, "test-admin", "bind", audit_month + "000123")
        audit_url = "/admin/member-gifts/audit?month=" + audit_month
        pages = [
            await req(admin, "GET", audit_url + f"&page={n}") for n in (1, 2, 3, 4, 99)
        ]
        assert [p["page"] for p in pages] == [1, 2, 3, 4, 5]
        assert [len(p["items"]) for p in pages] == [50, 50, 50, 50, 7]
        assert all(p["total"] == 207 and p["page_size"] == 50 for p in pages)
        ids = [e["id"] for p in pages for e in p["items"]]
        assert ids == sorted(set(ids), reverse=True)
        await req(admin, "GET", audit_url + "&page=0", expect=422)

        # A frozen parcel remains with the original recipient even when the UID moves
        # during the reopened window. Only an explicit refill may replace its snapshot.
        from app.models.member_gifts import Binding, Member

        returned_month = "203111"
        returned_base = "/admin/member-gifts/months/" + returned_month
        first_uid, second_uid = "3491000000000001", "3491000000000002"
        async with async_session_factory.begin() as db:
            original = Member(
                email=prefix + "refill@harei.test",
                password_hash=security.legacy_hasher.hash(pw),
                address=security.seal(address),
            )
            db.add(original)
            db.add(
                GiftMonth(
                    room_id=ROOM,
                    month=returned_month,
                    settled=True,
                    deadline=clock + timedelta(days=1),
                )
            )
            await db.flush()
            for uid in (first_uid, second_uid):
                db.add(Binding(uid=uid, username="原绑定用户名", member_id=original.id))
                db.add(
                    GiftOrder(
                        room_id=ROOM,
                        month=returned_month,
                        uid=uid,
                        nickname="名单昵称",
                        roster_level=3,
                        level=3,
                    )
                )
        async with client() as owner:
            await req(
                owner,
                "POST",
                "/user-auth/login",
                {"identifier": first_uid, "password": pw},
            )
            async with async_session_factory() as db:
                upgraded = await db.scalar(
                    select(Member.password_hash).where(Member.id == original.id)
                )
                assert upgraded.startswith(
                    "pbkdf2_sha256$"
                ) and await security.password_ok(upgraded, pw)
            clock += timedelta(days=2)
            schedule = (await req(admin, "GET", returned_base))["schedule"]
            assert schedule["phase"] == "closed" and schedule["refill_deadline"] is None
            until = (clock + timedelta(days=4)).date().isoformat()
            await req(
                admin,
                "POST",
                returned_base + "/extend",
                {"uid": first_uid, "date": until},
            )
            info = await req(admin, "GET", returned_base)
            assert info["schedule"]["phase"] == "grace"
            assert all(r["can_return_address"] for r in info["items"])
            await req(admin, "POST", f"{returned_base}/{first_uid}/return-address", {})
            await req(
                admin,
                "POST",
                f"{returned_base}/{first_uid}/return-address",
                {},
                expect=409,
            )
            missing = await req(admin, "GET", returned_base + "?kind=missing")
            assert (
                missing["total"] == 1
                and missing["items"][0]["collection_status"] == "修改中"
            )
            await req(owner, "POST", "/me/unbind/" + first_uid, {"password": pw})
            await bind(b, first_uid)
            personal_path = f"/me/gifts/{returned_month}/{first_uid}/address"
            await req(b, "PUT", personal_path, override, expect=404)
            assert not any(
                g["month"] == returned_month
                for g in (await req(b, "GET", "/me"))["gifts"]
            )
            await req(owner, "PUT", personal_path, {**override, "confirm_refill": True})
            me = await req(owner, "GET", "/me")
            assert me["address"] == address
            assert [(g["address"], g["username"]) for g in me["gifts"]] == [
                (override, "原绑定用户名"),
                (address, "原绑定用户名"),
            ]
            clock += timedelta(days=6)
            assert (await req(admin, "GET", returned_base))["schedule"][
                "phase"
            ] == "closed"
            await req(owner, "PUT", personal_path, address, expect=409)
            until = (clock + timedelta(days=3)).date().isoformat()
            await req(admin, "POST", returned_base + "/extend", {"date": until})
            assert all(
                r["can_return_address"]
                for r in (await req(admin, "GET", returned_base))["items"]
            )
            await req(admin, "POST", f"{returned_base}/{second_uid}/return-address", {})
            await req(owner, "PUT", "/me/address", override)
            pending = (await req(owner, "GET", "/me"))["gifts"][1]
            assert (
                pending["collection_status"] == "修改中" and pending["address"] is None
            )
            clock += timedelta(days=5)
            after = (await req(owner, "GET", "/me"))["gifts"]
            assert [(g["state"], g["address"]) for g in after] == [
                ("ready", override),
                ("overdue", None),
            ]
            assert after[1]["collection_status"] == "未填写"
            second_path = f"/me/gifts/{returned_month}/{second_uid}/address"
            await req(
                owner,
                "PUT",
                second_path,
                {**override, "confirm_refill": True},
                expect=409,
            )
            schedule = (await req(admin, "GET", returned_base))["schedule"]
            assert schedule["phase"] == "closed"
            assert (
                schedule["refill_deadline"]
                == gifts.midnight_after(
                    datetime.fromisoformat(until).date()
                ).isoformat()
            )
            # A pending refill written by an earlier version also submits at its deadline.
            async with async_session_factory.begin() as db:
                legacy = await db.get(GiftOrder, (ROOM, returned_month, second_uid))
                legacy.state = "grace"
                legacy.grace_until = legacy.extension_until = clock + timedelta(days=1)
                legacy.address = security.seal(address)
            legacy_draft = (await req(owner, "GET", "/me"))["gifts"][1]
            assert (
                legacy_draft["collection_status"] == "修改中"
                and legacy_draft["frozen_at"] is None
            )
            clock += timedelta(days=1)
            restored = (await req(owner, "GET", "/me"))["gifts"][1]
            assert (
                restored["state"] == "ready"
                and restored["frozen_at"] == clock.isoformat()
            )
            assert restored["address"] == address

            # Opting out leaves the address book intact. A one-off selection belongs to one UID/month.
            third_uid = "3491000000000003"
            current, upcoming = "203205", "203206"
            await req(owner, "PUT", preference, {"auto_fill_address": False})
            async with async_session_factory.begin() as db:
                account = await db.get(
                    Member, (await db.get(Binding, second_uid)).member_id
                )
                db.add(Binding(uid=third_uid, member_id=account.id, username="测试"))
                for gift_month, delay in ((current, 1), (upcoming, 30)):
                    db.add(
                        GiftMonth(
                            room_id=ROOM,
                            month=gift_month,
                            settled=True,
                            deadline=clock + timedelta(days=delay),
                        )
                    )
                    await db.flush()
                    for uid in (second_uid, third_uid):
                        db.add(
                            GiftOrder(
                                room_id=ROOM,
                                month=gift_month,
                                uid=uid,
                                nickname="测试",
                                roster_level=3,
                                level=3,
                            )
                        )
            await gift_worker.tick()
            async with async_session_factory() as db:
                (notice,) = (
                    await db.scalars(
                        select(GiftNotice).where(GiftNotice.month == current)
                    )
                ).all()
                assert notice.uids == [second_uid, third_uid]
            order_url = f"/me/gifts/{current}/{second_uid}/address"
            await req(owner, "PUT", order_url, {"use_default": True})
            await req(owner, "PUT", order_url, address)
            profile = await req(owner, "GET", "/me")
            assert profile["address"] == override and not profile["auto_fill_address"]
            selected = {(g["month"], g["uid"]): g for g in profile["gifts"]}
            assert selected[current, second_uid]["address"] == address
            assert selected[current, third_uid]["address"] is None
            assert selected[upcoming, second_uid]["address"] is None
            assert selected[returned_month, first_uid]["frozen_at"]
            await gift_worker.send_pending()
            reminder = await email_message(
                prefix + "refill@harei.test", current + " 舰礼"
            )
            assert third_uid in reminder and second_uid not in reminder
            async with async_session_factory() as db:
                (sent,) = (
                    await db.scalars(
                        select(GiftNotice).where(GiftNotice.month == current)
                    )
                ).all()
                assert sent.sent and sent.uids == [third_uid]
            clock += timedelta(days=1)
            profile = await req(owner, "GET", "/me")
            selected = {(g["month"], g["uid"]): g for g in profile["gifts"]}
            assert selected[current, second_uid]["state"] == "ready"
            assert selected[current, second_uid]["address"] == address
            assert selected[current, third_uid]["state"] == "grace"
            assert selected[current, third_uid]["address"] is None
            # A failed attempt must recheck the preference before retrying.
            clock += timedelta(days=27)
            await gift_worker.tick()
            with patch.object(
                gift_worker, "deliver", side_effect=OSError("test SMTP outage")
            ) as failure:
                await gift_worker.send_pending()
                failure.assert_awaited_once()
                body = failure.await_args.args[2]
                assert second_uid in body and third_uid in body
            await req(owner, "PUT", preference, {"auto_fill_address": True})
            clock += timedelta(minutes=2)
            with patch.object(
                gift_worker, "deliver", new_callable=AsyncMock
            ) as delivery:
                await gift_worker.send_pending()
                delivery.assert_not_awaited()
            async with async_session_factory() as db:
                assert not (
                    await db.scalars(
                        select(GiftNotice).where(GiftNotice.month == upcoming)
                    )
                ).all()

    # A real application startup scans only this Beijing month, including opted-in legacy rows.
    clock = datetime(2032, 7, 1, 0, 1)
    startup_rows = [
        ("970001", "舰长", None, None, True, "203207"),
        ("970001", "提督", None, None, True, "203207"),
        ("970001", "总督", ROOM, 0, False, "203207"),
        ("970002", "舰长", ROOM, 1900, False, "203207"),
        ("970002", "总督", None, None, True, "203206"),
        ("970002", "总督", ROOM, 9999, False, "203208"),
        ("970003", "舰长", ROOM, 0, False, "203207"),
        ("970004", "提督", ROOM + 1, 198000, False, "203207"),
        ("970005", "总督", ROOM, None, False, "203207"),
        ("970006", "总督", None, None, False, "203207"),
        ("970007", "总督", None, None, True, "203206"),
    ]
    async with async_session_factory.begin() as db:
        for uid, level, room, price, old, stamp in startup_rows:
            db.add(
                Captain(
                    user_uid=uid,
                    username="启动补读",
                    joined_at=datetime.strptime(stamp, "%Y%m"),
                    joined_month=stamp,
                    level=level,
                    ship_count=1,
                    room_id=room,
                    paid_price=price,
                    legacy_assumed_paid=old,
                )
            )
    scans = []

    def record_query(_conn, _cursor, statement, params, _context, _many):
        if statement.lstrip().upper().startswith(
            "SELECT"
        ) and "FROM captains " in " ".join(statement.split()):
            scans.append((statement, params))

    event.listen(engine.sync_engine, "before_cursor_execute", record_query)
    try:
        with (
            patch("app.main.migrate_legacy_captaingift_images", new_callable=AsyncMock),
            patch.object(gift_worker, "run", new_callable=AsyncMock) as worker,
        ):
            for n in range(2):
                async with app.router.lifespan_context(app):
                    async with async_session_factory() as db:
                        orders = (
                            await db.execute(
                                select(GiftOrder.month, GiftOrder.uid, GiftOrder.level)
                                .where(GiftOrder.uid.like("97000%"))
                                .order_by(GiftOrder.uid)
                            )
                        ).all()
                        assert orders == [
                            ("203207", "970001", 2),
                            ("203207", "970002", 3),
                        ]
                        entries = (
                            await db.scalars(
                                select(GiftAudit).where(
                                    GiftAudit.subject.like("203207/97000%"),
                                    GiftAudit.action == "qualification",
                                )
                            )
                        ).all()
                        assert len(entries) == 2 and all(
                            e.details["source"] == "captains" for e in entries
                        )
                assert len(scans) == n + 1
                worker.assert_called_with(restore_pending=False)
        assert all(
            "captains.joined_month =" in query and "203207" in str(params)
            for query, params in scans
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_query)
    await close_redis_client()
    await engine.dispose()


if __name__ == "__main__" and sys.argv[-1] == "run":
    asyncio.run(workflow())
