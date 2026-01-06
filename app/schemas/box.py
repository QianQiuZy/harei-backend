from datetime import datetime

from pydantic import BaseModel


class UploadResponse(BaseModel):
    code: int = 0
    message_id: int
    image_ids: list[int]


class DeleteRequest(BaseModel):
    id: int


class MessageItem(BaseModel):
    id: int
    created_at: datetime
    msg: str | None
    tag: str | None
    images: list[str]
    images_thumb: list[str]
    images_jpg: list[str]


class MessageListResponse(BaseModel):
    code: int = 0
    items: list[MessageItem]

    @staticmethod
    def from_messages(messages: list) -> "MessageListResponse":
        items = []
        for message in messages:
            items.append(
                MessageItem(
                    id=message.message_id,
                    created_at=message.created_at,
                    msg=message.message_text,
                    tag=message.tag,
                    images=[image.image_path for image in message.images],
                    images_thumb=[image.thumb_path for image in message.images],
                    images_jpg=[image.jpg_path for image in message.images],
                )
            )
        return MessageListResponse(items=items)
