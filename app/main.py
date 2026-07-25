from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.services import bili_captain_listener
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.auth import router as auth_router
from app.api.box import MAX_UPLOAD_REQUEST_BYTES, router as box_router
from app.api.captaingift import router as captaingift_router
from app.api.captains import router as captains_router
from app.api.download import router as download_router
from app.api.live import router as live_router
from app.api.huangdou import router as huangdou_router
from app.api.music import router as music_router
from app.api.music_manage import router as music_manage_router
from app.api.music_manage_extra import router as music_manage_extra_router
from app.api.music_import import router as music_import_router
from app.api.tag import router as tag_router
from app.core.config import get_settings
from app.core.redis import close_redis_client
from app.db.session import engine

settings = get_settings()


class UploadBodyTooLargeError(Exception):
    pass


class UploadBodyLimitMiddleware:
    app: ASGIApp
    max_body_bytes: int

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/box/uploads"
        ):
            await self.app(scope, receive, send)
            return

        content_length = self._content_length(scope)
        if content_length is not None and content_length > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self.max_body_bytes:
                    raise UploadBodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except UploadBodyTooLargeError:
            await self._reject(scope, receive, send)

    @staticmethod
    def _content_length(scope: Scope) -> int | None:
        for name, value in scope["headers"]:
            if name.lower() != b"content-length":
                continue
            try:
                return int(value)
            except ValueError:
                return None
        return None

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "error": "request_too_large",
                    "max_bytes": self.max_body_bytes,
                }
            },
        )
        await response(scope, receive, send)

@asynccontextmanager
async def lifespan(_: FastAPI):
    await bili_captain_listener.bootstrap()
    try:
        yield
    finally:
        await bili_captain_listener.shutdown()
        await close_redis_client()
        await engine.dispose()

app = FastAPI(lifespan=lifespan,docs_url=None, redoc_url=None, openapi_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=MAX_UPLOAD_REQUEST_BYTES)

app.include_router(auth_router)
app.include_router(box_router)
app.include_router(captaingift_router)
app.include_router(captains_router)
app.include_router(live_router)
app.include_router(download_router)

app.include_router(huangdou_router)
app.include_router(music_router)
app.include_router(music_manage_router)
app.include_router(music_manage_extra_router)
app.include_router(music_import_router)
app.include_router(tag_router)
