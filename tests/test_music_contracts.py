from io import BytesIO
from pathlib import Path

from fastapi import HTTPException
from httpx import AsyncClient
from PIL import Image as PilImage
import pytest

from app.api.box import (
    MAX_DECODED_IMAGE_PIXELS,
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_REQUEST_BYTES,
    process_uploaded_image,
)
from app.schemas.auth import LoginResponse


async def login(client: AsyncClient, path: str, username: str, password: str) -> LoginResponse:
    response = await client.post(path, json={"username": username, "password": password})
    assert response.status_code == 200
    return LoginResponse.model_validate(response.json())


def bearer(payload: LoginResponse) -> dict[str, str]:
    return {"Authorization": f"Bearer {payload.token}"}


def prepare_upload_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for directory in ("uploads/original", "uploads/jpg", "uploads/thumbs"):
        Path(directory).mkdir(parents=True)


@pytest.mark.asyncio
async def test_public_upload_rejects_oversized_request_before_persisting(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/box/uploads",
        headers={
            "Content-Type": "multipart/form-data; boundary=upload-boundary",
            "Content-Length": str(MAX_UPLOAD_REQUEST_BYTES + 1),
        },
        content=b"",
    )
    assert response.status_code == 413


def test_image_processing_rejects_decoded_pixel_bomb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_upload_directories(tmp_path, monkeypatch)

    buffer = BytesIO()
    PilImage.new("1", (5200, 5200), color=1).save(buffer, format="PNG")

    with pytest.raises(HTTPException) as raised:
        _ = process_uploaded_image(
            buffer.getvalue(), ".png", "image/png", "pixel-bomb"
        )

    assert raised.value.status_code == 413
    assert raised.value.detail == {
        "error": "image_too_large",
        "max_pixels": MAX_DECODED_IMAGE_PIXELS,
    }
    assert not list(tmp_path.rglob("*.*"))


def test_image_processing_rejects_decoded_gif_pixel_bomb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_upload_directories(tmp_path, monkeypatch)
    buffer = BytesIO()
    PilImage.new("P", (5200, 5200), color=1).save(buffer, format="GIF")

    with pytest.raises(HTTPException) as raised:
        _ = process_uploaded_image(
            buffer.getvalue(), ".gif", "image/gif", "gif-pixel-bomb"
        )

    assert raised.value.status_code == 413
    assert not list(tmp_path.rglob("*.*"))


def test_image_processing_rejects_invalid_gif(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare_upload_directories(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as raised:
        _ = process_uploaded_image(b"not-a-gif", ".gif", "image/gif", "invalid-gif")

    assert raised.value.status_code == 400
    assert not list(tmp_path.rglob("*.*"))


@pytest.mark.asyncio
async def test_public_upload_rejects_oversized_file(client: AsyncClient) -> None:
    response = await client.post(
        "/box/uploads",
        data={"message": "test", "tag": "test"},
        files={"files": ("large.jpg", b"x" * (MAX_UPLOAD_BYTES + 1), "image/jpeg")},
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_scope_matrix_and_logout(client: AsyncClient) -> None:
    assert (await client.post("/login", json={"username": "admin", "password": "wrong"})).status_code == 401
    admin = await login(client, "/login", "admin", "admin-password")
    music = await login(client, "/music-manage/login", "music", "music-password")
    assert admin.scopes == ["admin", "music:manage"]
    assert music.scopes == ["music:manage"]
    assert (await client.get("/tag/all", headers=bearer(music))).status_code == 403
    assert (await client.get("/music-manage/stats", headers=bearer(admin))).status_code == 200
    assert (await client.post("/logout", headers=bearer(music))).status_code == 200
    assert (await client.get("/auth", headers=bearer(music))).status_code == 401


@pytest.mark.asyncio
async def test_song_version_archive_restore_and_audit(
    client: AsyncClient,
    song_payload: dict[str, str | list[str]],
) -> None:
    music = await login(client, "/music-manage/login", "music", "music-password")
    headers = bearer(music)
    created = await client.post("/music-manage/songs", headers=headers, json=song_payload)
    assert created.status_code == 201
    song_id = created.json()["song_id"]
    assert created.json()["source_key"].startswith("song_")
    updated = await client.put(
        f"/music-manage/songs/{song_id}",
        headers=headers,
        json={**song_payload, "title": "Updated Song", "version": 1},
    )
    assert updated.status_code == 200
    version = updated.json()["version"]
    conflict = await client.put(
        f"/music-manage/songs/{song_id}",
        headers=headers,
        json={**song_payload, "version": 1},
    )
    assert conflict.status_code == 409
    archived = await client.post(
        f"/music-manage/songs/{song_id}/archive",
        headers=headers,
        json={"version": version},
    )
    assert archived.status_code == 200
    detail = await client.get(f"/music-manage/songs/{song_id}", headers=headers)
    assert detail.json()["item"]["status"] == "archived"
    restored = await client.post(
        f"/music-manage/songs/{song_id}/restore",
        headers=headers,
        json={"version": detail.json()["item"]["version"]},
    )
    assert restored.status_code == 200
    audit = await client.get("/music-manage/audit", headers=headers)
    assert audit.json()["total"] == 4
    assert audit.json()["items"][0]["details"]
    assert audit.json()["items"][0]["created_at"].endswith("Z")
    assert audit.json()["items"][0]["details"] == {"before": "archived", "after": "active"}


@pytest.mark.asyncio
async def test_performance_crud_and_public_catalog(
    client: AsyncClient,
    song_payload: dict[str, str | list[str]],
) -> None:
    music = await login(client, "/music-manage/login", "music", "music-password")
    headers = bearer(music)
    created = await client.post("/music-manage/songs", headers=headers, json=song_payload)
    song_id = created.json()["song_id"]
    song_source_key = created.json()["source_key"]
    performance = {
        "version": 1,
        "date": "2026-07-25",
        "platform": "哔哩哔哩",
        "stream_title": "测试直播",
        "stream_url": None,
        "clip_url": "https://www.bilibili.com/video/BV1ABCDEF123",
    }
    invalid_url = await client.post(
        f"/music-manage/songs/{song_id}/performances",
        headers=headers,
        json={**performance, "clip_url": "javascript:alert(1)"},
    )
    assert invalid_url.status_code == 422
    added = await client.post(
        f"/music-manage/songs/{song_id}/performances",
        headers=headers,
        json=performance,
    )
    assert added.status_code == 201
    performance_id = added.json()["performance_id"]
    assert added.json()["source_key"].startswith("performance_")
    public = await client.get("/music", params={"q": "Test", "search_mode": "title", "sort": "count"})
    assert public.json()["stats"] == {"song_count": 1, "performance_count": 1}
    assert public.json()["items"][0]["performanceCount"] == 1
    detail = await client.get(f"/music/{song_source_key}")
    assert detail.json()["item"]["performances"][0]["performance_id"] == performance_id
    assert detail.json()["item"]["performances"][0]["stream"]["id"] == "BV1ABCDEF123"
    etag = public.headers["etag"]
    not_modified = await client.get("/music", headers={"If-None-Match": etag})
    assert not_modified.status_code == 304
    assert not not_modified.content
    changed = {**performance, "version": 2, "stream_title": "更新直播"}
    stale = await client.put(
        f"/music-manage/performances/{performance_id}",
        headers=headers,
        json={**performance, "stream_title": "过期修改"},
    )
    assert stale.status_code == 409
    assert (
        await client.put(
            f"/music-manage/performances/{performance_id}", headers=headers, json=changed
        )
    ).status_code == 200
    assert (
        await client.delete(
            f"/music-manage/performances/{performance_id}",
            headers=headers,
            params={"version": 3},
        )
    ).status_code == 200
    assert (await client.get("/music/export")).status_code == 200
