import base64
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from redis.exceptions import RedisError
from sqlalchemy import select

from app.core.redis import ACCOUNT_DB, ADDRESS_DB
from app.models.member_gifts import Binding, GiftSystem, Member
from app.services import member_gifts as gifts
from app.services.hot_cache import TTL, fill_lock, pack, reserve, store
from app.services.member_security import digest, reveal, seal


@dataclass(frozen=True)
class Account:
    id: int
    email: str
    session_version: int
    password_hash: str
    bindings: list

    def accepts(self, identifier):
        return identifier == self.email or any(
            b["uid"] == identifier for b in self.bindings
        )


def alias_key(identifier):
    return "login:" + digest("login-lookup", identifier)


def account_cipher():
    return Fernet(base64.urlsafe_b64encode(bytes.fromhex(digest("account-cache-v1"))))


def account_key(owner):
    return f"account:{owner}"


def view_key(owner):
    return f"address:v2:{owner}"


async def share_fence(db):
    await db.execute(
        select(GiftSystem.id).where(GiftSystem.id == 1).with_for_update(read=True)
    )


async def load_account(db, owner):
    row = (
        await db.execute(
            select(
                Member.id, Member.email, Member.session_version, Member.password_hash
            ).where(Member.id == owner, Member.email.is_not(None))
        )
    ).one_or_none()
    if not row:
        return None
    bindings = (
        (
            await db.execute(
                select(Binding.uid, Binding.username)
                .where(Binding.member_id == owner)
                .order_by(Binding.uid)
            )
        )
        .mappings()
        .all()
    )
    return Account(*row, [dict(b) for b in bindings])


def account_values(owner, account):
    if account is None:
        return {account_key(owner): ""}
    aliases = [account.email, *(b["uid"] for b in account.bindings)]
    encoded = account_cipher().encrypt(pack(asdict(account)).encode()).decode()
    return {
        account_key(owner): encoded,
        **{alias_key(value): str(owner) for value in aliases},
    }


async def read_account(db, *, owner=None, identifier=None):
    cache = None
    try:
        cache = await store(ACCOUNT_DB)
        record_id = owner
        if identifier is not None:
            saved = await cache.redis.get(cache.key(alias_key(identifier)))
            record_id = int(saved) if saved and saved.isdecimal() else None
        if record_id is not None:
            raw = await cache.redis.get(cache.key(account_key(record_id)))
            if raw and not raw.startswith("!"):
                account = Account(**json.loads(account_cipher().decrypt(raw.encode())))
                if account.id == record_id and (
                    identifier is None or account.accepts(identifier)
                ):
                    return account
    except (InvalidToken, ValueError, KeyError, TypeError):
        pass  # Discard an invalid cache value and reconstruct it from MySQL.
    except RedisError:
        cache = None

    try:
        await share_fence(db)
        if identifier is not None:
            query = (
                select(Binding.member_id).where(Binding.uid == identifier)
                if identifier.isdecimal()
                else select(Member.id).where(Member.email == identifier)
            )
            owner = await db.scalar(query)
        account = await load_account(db, owner) if owner is not None else None
        if account and identifier is not None and not account.accepts(identifier):
            return None
        if account and cache:
            try:
                async with cache.redis.pipeline(transaction=True) as pipe:
                    for key, value in account_values(owner, account).items():
                        pipe.set(cache.key(key), value, ex=TTL)
                    await pipe.execute()
            except RedisError:
                pass
        return account
    finally:
        await db.close()


async def load_view(db, member):
    bindings = (
        await db.execute(
            select(Binding.uid, Binding.address).where(Binding.member_id == member.id)
        )
    ).all()
    orders = await gifts.personal_orders(db, member)
    # An overdue open/grace item must force a new advance even when the deadline
    # passes between advancing the clock and constructing this representation.
    boundaries = [
        g["deadline"] if g["state"] == "open" else g["grace_until"]
        for g in orders
        if g["state"] in ("open", "grace")
    ]
    until = min((v for v in boundaries if v), default=None)
    result = {
        "address": reveal(member.address),
        "auto_fill_address": member.auto_fill_address,
        "binding_addresses": {uid: reveal(address) for uid, address in bindings},
        "gifts": orders,
    }
    return result, until


def encoded_view(generation, member, value, until):
    return seal(
        {
            "generation": generation,
            "owner": member.id,
            "version": member.session_version,
            "until": until.isoformat() if until else None,
            "view": jsonable_encoder(value),
        }
    )


async def cached_view(cache, account):
    generation, raw = await cache.redis.mget(
        cache.key("gifts:version"), cache.key(view_key(account.id))
    )
    if generation and raw and not raw.startswith("!"):
        try:
            saved = reveal(raw)
            if (
                saved["generation"] == generation
                and saved["owner"] == account.id
                and saved["version"] == account.session_version
                and (
                    not saved["until"]
                    or gifts.now_cn() < datetime.fromisoformat(saved["until"])
                )
            ):
                return saved["view"]
        except (InvalidToken, ValueError, KeyError, TypeError):
            pass
    return None


async def read_view(db, account):
    cache = None
    try:
        cache = await store(ADDRESS_DB)
        value = await cached_view(cache, account)
        if value is not None:
            return value
    except RedisError:
        cache = None
    async with fill_lock(("address", account.id)):
        if cache:
            try:
                value = await cached_view(cache, account)
                if value is not None:
                    return value
            except RedisError:
                cache = None
        return await cold_view(db, account, cache)


async def cold_view(db, account, cache):
    # Advance calendar transitions durably before publishing even a cold response.
    await gifts.lock_system(db)
    await gifts.advance(db, gifts.now_cn())
    await db.commit()
    db.expire_all()
    await share_fence(db)
    member = await db.get(Member, account.id)
    if (
        not member
        or not member.email
        or member.session_version != account.session_version
    ):
        raise HTTPException(401, "请登录个人账号")
    value, until = await load_view(db, member)
    if cache:
        try:
            generation = await cache.generation("gifts")
            raw = encoded_view(generation, member, value, until)
            await cache.redis.set(cache.key(view_key(member.id)), raw, ex=TTL)
        except RedisError:
            pass
    return value


async def prepare_members(db, changes):
    # Account/fulfillment writers and cold readers share the existing gift-system fence.
    await gifts.lock_system(db)
    accounts = {m.id for m in changes.members} | changes.owners
    owners = accounts | changes.view_owners
    if len(changes.order_uids) > 50:
        # A roster import / bulk tracking write may touch thousands of owners.
        changes.months = True
        owners = set(accounts)
    if changes.order_uids and not changes.months:
        owners.update(
            await db.scalars(
                select(Binding.member_id).where(Binding.uid.in_(changes.order_uids))
            )
        )
    pending = []
    if accounts or changes.aliases:
        cache = await store(ACCOUNT_DB)
        values = {alias_key(old): "" for old in changes.aliases}
        for owner in accounts:
            values.update(account_values(owner, await load_account(db, owner)))
        pending.append(await reserve(cache, values))
    address_store = await store(ADDRESS_DB)
    if changes.months:
        await address_store.rotate("gifts")
    generation = await address_store.generation("gifts")
    views = {}
    for owner in owners:
        member = await db.get(Member, owner)
        if member and member.email:
            value, until = await load_view(db, member)
            views[view_key(owner)] = encoded_view(generation, member, value, until)
        else:
            views[view_key(owner)] = ""
    pending.append(await reserve(address_store, views))
    return pending
