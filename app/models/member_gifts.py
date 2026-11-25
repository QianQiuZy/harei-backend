from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GiftSystem(Base):
    __tablename__ = "gift_system"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    first_month: Mapped[str] = mapped_column(String(6))


class Member(Base):
    __tablename__ = "member_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    auto_fill_address: Mapped[bool] = mapped_column(default=True, server_default="1")
    session_version: Mapped[int] = mapped_column(default=1)


class Binding(Base):
    __tablename__ = "member_uid_bindings"
    uid: Mapped[str] = mapped_column(String(20), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(255))
    member_id: Mapped[int] = mapped_column(ForeignKey("member_accounts.id"), index=True)
    address: Mapped[str | None] = mapped_column(Text)


class PurchaseLevel(Base):
    __tablename__ = "gift_purchase_levels"
    __table_args__ = (
        Index("ix_gift_purchase_pending", "room_id", "applied_level", "month"),
    )
    room_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    month: Mapped[str] = mapped_column(String(6), primary_key=True)
    uid: Mapped[str] = mapped_column(String(20), primary_key=True)
    level: Mapped[int] = mapped_column(Integer)
    paid_level: Mapped[int | None]
    applied_level: Mapped[int | None]
    nickname: Mapped[str | None] = mapped_column(String(255))


class GiftMonth(Base):
    __tablename__ = "gift_months"
    room_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    month: Mapped[str] = mapped_column(String(6), primary_key=True)
    settled: Mapped[bool] = mapped_column(Boolean, default=False)
    roster_finalized_at: Mapped[datetime | None] = mapped_column(DateTime)
    deadline: Mapped[datetime | None] = mapped_column(DateTime)
    reminded: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(default=0)


class GiftOrder(Base):
    __tablename__ = "member_gifts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["room_id", "month"], ["gift_months.room_id", "gift_months.month"]
        ),
    )
    room_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    month: Mapped[str] = mapped_column(String(6), primary_key=True)
    uid: Mapped[str] = mapped_column(String(20), primary_key=True, index=True)
    nickname: Mapped[str] = mapped_column(String(255))
    bound_username: Mapped[str | None] = mapped_column(String(255))
    roster_level: Mapped[int | None]
    level: Mapped[int]
    state: Mapped[str] = mapped_column(String(16), default="open", index=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("member_accounts.id"), index=True
    )
    waived_by: Mapped[int | None] = mapped_column(ForeignKey("member_accounts.id"))
    grace_until: Mapped[datetime | None] = mapped_column(DateTime)
    extension_until: Mapped[datetime | None] = mapped_column(DateTime)
    refill_requested: Mapped[bool] = mapped_column(default=False, server_default="0")
    returned_address: Mapped[str | None] = mapped_column(Text)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime)
    address: Mapped[str | None] = mapped_column(Text)
    tracking: Mapped[str | None] = mapped_column(String(100))
    needs_reship: Mapped[bool] = mapped_column(default=False, server_default="0")
    manual_level: Mapped[bool] = mapped_column(default=False)


class GiftAudit(Base):
    __tablename__ = "member_gift_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(40))
    subject: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[dict] = mapped_column(JSON)


class GiftNotice(Base):
    __tablename__ = "member_gift_mail"
    __table_args__ = (
        UniqueConstraint(
            "month", "member_id", "revision", name="uq_gift_mail_revision"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(6))
    member_id: Mapped[int] = mapped_column(ForeignKey("member_accounts.id"))
    revision: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    uids: Mapped[list] = mapped_column(JSON)
    deadline: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(default=0)
    next_try: Mapped[datetime] = mapped_column(DateTime, index=True)
    sent: Mapped[bool] = mapped_column(default=False)


class Appeal(Base):
    __tablename__ = "member_appeals"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket: Mapped[str] = mapped_column(String(64), unique=True)
    contact: Mapped[str] = mapped_column(String(254))
    uid: Mapped[str | None] = mapped_column(String(20))
    message: Mapped[str] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    closed: Mapped[bool] = mapped_column(default=False)
