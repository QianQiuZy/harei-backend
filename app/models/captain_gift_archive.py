from datetime import datetime

from sqlalchemy import Index, String, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base import Base


class CaptainGiftArchive(Base):
    __tablename__ = "captain_gift_archives"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    gift_month: Mapped[str] = mapped_column(String(6), nullable=False)
    image_path: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    __table_args__ = (Index("idx_captain_gift_archives_month", "gift_month"),)
