from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base import Base


class Image(Base):
    __tablename__ = "images"

    image_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.message_id", ondelete="CASCADE"))
    image_path: Mapped[str] = mapped_column(String(255), nullable=False)
    thumb_path: Mapped[str | None] = mapped_column(String(255))
    jpg_path: Mapped[str | None] = mapped_column(String(255))
    uploaded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP,
        nullable=False,
        server_default=func.current_timestamp(),
    )

    message = relationship("Message", back_populates="images")

    __table_args__ = (Index("idx_images_message_id", "message_id"),)
