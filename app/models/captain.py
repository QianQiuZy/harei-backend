from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Index, Integer, String, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class Captain(Base):
    __tablename__ = "captains"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_uid: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    joined_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    joined_month: Mapped[str] = mapped_column(String(6), nullable=False)
    level: Mapped[str] = mapped_column(
        Enum("舰长", "提督", "总督", name="captains_level"),
        nullable=False,
    )
    ship_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    is_red_packet: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    __table_args__ = (
        Index("idx_captains_uid", "user_uid"),
        Index("idx_captains_month_level", "joined_month", "level"),
        Index("idx_captains_joined_at", "joined_at"),
    )
