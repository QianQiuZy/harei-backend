from datetime import datetime

from sqlalchemy import Enum, Index, Text, TIMESTAMP, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    message_text: Mapped[str | None] = mapped_column(Text)
    tag: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        Enum("pending", "approved", "archived", "delete", name="messages_status"),
        nullable=False,
        server_default="pending",
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    images = relationship("Image", back_populates="message", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_messages_ip_created", "ip_address", "created_at"),
        Index("idx_messages_status_created", "status", "created_at"),
        Index("idx_messages_tag", "tag"),
    )
