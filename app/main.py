import asyncio
import logging
from contextlib import asynccontextmanager

from app.api.member import router as member_router
from app.api.gift_admin import router as gift_admin_router
from app.services import gift_worker

from fastapi import FastAPI, HTTPException
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from redis.exceptions import RedisError
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
from app.services import bili_captain_listener
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.auth import router as auth_router
from app.api.box import MAX_UPLOAD_REQUEST_BYTES, router as box_router
from app.api.captaingift import router as captaingift_router
from app.api.captains import router as captains_router
from app.api.download import router as download_router
from app.api.emoji import router as emoji_router
from app.api.live import router as live_router
from app.api.huangdou import router as huangdou_router
from app.api.music import router as music_router
from app.api.music_manage import router as music_manage_router
from app.api.music_manage_extra import router as music_manage_extra_router
from app.api.music_import import router as music_import_router
from app.api.tag import router as tag_router
from app.core.config import get_settings
from app.core.redis import close_redis_client, get_rate_limit_redis, get_session_redis
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from app.db.session import engine
from app.services.captain_gift_archive import migrate_legacy_captaingift_images

settings = get_settings()


def private_path(path: str) -> bool:
    return path.rstrip("/") in {"/me", "/auth", "/login", "/logout"} or path.startswith(
        ("/me/", "/user-auth/", "/admin/member-gifts", "/music-manage/")
    )


class UploadBodyLimitMiddleware:
    app: ASGIApp
    max_body_bytes: int

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return
        if path in ("/box/uploads", "/captaingift/add"):
            maximum = self.max_body_bytes
        elif path == "/music-manage/performances/import" or (
            path.startswith("/admin/member-gifts/months/") and path.endswith("/tracking-import")
        ):
            # Both workbooks allow a 5 MiB file plus multipart headers.
            maximum = 6 * 1024 * 1024
        elif private_path(path) or (
            path == "/download/add"
            and b"application/json" in dict(scope["headers"]).get(b"content-type", b"").lower()
        ):
            # Account and management JSON forms are small, including appeal text.
            maximum = 64 * 1024
        else:
            await self.app(scope, receive, send)
            return
        detail = {"error": "request_too_large", "max_bytes": maximum}
        content_length = self._content_length(scope)
        if content_length is not None and content_length > maximum:
            await JSONResponse({"detail": detail}, status_code=413)(scope, receive, send)
            return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > maximum:
                    raise HTTPException(status_code=413, detail=detail)
            return message

        await self.app(scope, limited_receive, send)

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

@asynccontextmanager
async def lifespan(_: FastAPI):
    await migrate_legacy_captaingift_images()
    try:
        await get_rate_limit_redis()
        await get_session_redis()
    except (RedisConnectionError, RedisTimeoutError):
        # Public routes can still serve; the next limited request retries initialization.
        logging.getLogger(__name__).warning("限速 Redis 暂不可用，将在下次限速请求时重试")
    await bili_captain_listener.bootstrap()
    restored = await gift_worker.try_restore_captains()
    gifts = asyncio.create_task(gift_worker.run(restore_pending=not restored), name="member-gifts")
    try:
        yield
    finally:
        gifts.cancel()
        await asyncio.gather(gifts, return_exceptions=True)
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
    expose_headers=["Retry-After"],
)
app.add_middleware(UploadBodyLimitMiddleware, max_body_bytes=MAX_UPLOAD_REQUEST_BYTES)


@app.exception_handler(PoolTimeoutError)
@app.exception_handler(RedisError)
async def redis_unavailable(request, exc):
    return JSONResponse({"detail": "服务暂时不可用，请稍后重试"}, status_code=503, headers={"Retry-After": "5"})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if private_path(request.url.path) or "authorization" in request.headers:
        # Omit submitted credentials, addresses and verification codes from validation errors.
        errors = [{k: e[k] for k in ("loc", "type", "msg")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)
    return await request_validation_exception_handler(request, exc)

@app.middleware("http")
async def private_responses(request, call_next):
    response = await call_next(request)
    if private_path(request.url.path) or "authorization" in request.headers or response.status_code in (401, 403):
        response.headers["Cache-Control"] = "private, no-store"
    return response


app.include_router(member_router)
app.include_router(gift_admin_router)
app.include_router(auth_router)
app.include_router(box_router)
app.include_router(captaingift_router)
app.include_router(captains_router)
app.include_router(live_router)
app.include_router(download_router)
app.include_router(emoji_router)

app.include_router(huangdou_router)
app.include_router(music_router)
app.include_router(music_manage_router)
app.include_router(music_manage_extra_router)
app.include_router(music_import_router)
app.include_router(tag_router)
