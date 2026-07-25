import re
from urllib.parse import urlsplit
from uuid import uuid4


BV_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])(BV[0-9A-Za-z]{10})(?![0-9A-Za-z])")
LIVE_ROOM_PATTERN = re.compile(r"^/(?:blanc/)?(\d+)(?:/|$)")


def generate_music_source_key(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def derive_stream_id(stream_url: str | None, clip_url: str | None) -> str | None:
    for value in (clip_url, stream_url):
        if not value:
            continue
        match = BV_ID_PATTERN.search(value)
        if match:
            return match.group(1)

    for value in (stream_url, clip_url):
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.hostname not in {"live.bilibili.com", "www.live.bilibili.com"}:
            continue
        match = LIVE_ROOM_PATTERN.match(parsed.path)
        if match:
            return match.group(1)
    return None
