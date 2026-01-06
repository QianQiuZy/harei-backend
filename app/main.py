from fastapi import FastAPI

import asyncio
from contextlib import asynccontextmanager
from app.services import bili_captain_listener
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.box import router as box_router
from app.api.captaingift import router as captaingift_router
from app.api.captains import router as captains_router
from app.api.live import router as live_router
from app.api.huangdou import router as huangdou_router
from app.api.music import router as music_router
from app.api.tag import router as tag_router
from app.core.config import get_settings

settings = get_settings()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(box_router)
app.include_router(captaingift_router)
app.include_router(captains_router)
app.include_router(live_router)

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(bili_captain_listener.bootstrap())
    app.state.bili_listener_task = task
    try:
        yield
    finally:
        await bili_captain_listener.shutdown()

        t = getattr(app.state, "bili_listener_task", None)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

app = FastAPI(lifespan=lifespan)

app.include_router(huangdou_router)
app.include_router(music_router)
app.include_router(tag_router)
