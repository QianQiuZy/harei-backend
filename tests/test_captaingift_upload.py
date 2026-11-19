"""Exercise the upload and download routes with isolated image storage."""

from importlib import import_module
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest


@pytest.fixture
def gift_client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "APP_SECRET_KEY": "test-secret",
        "MYSQL_HOST": "127.0.0.1",
        "MYSQL_USER": "test",
        "MYSQL_PASSWORD": "test",
        "MYSQL_DATABASE": "test",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "AUTH_USERNAME": "test",
        "AUTH_PASSWORD_HASH": "unused-in-this-test",
    }.items():
        monkeypatch.setenv(key, value)
    api = import_module("app.api.captaingift")

    result = Mock()
    result.scalar_one_or_none.return_value = None
    db = Mock()
    db.execute = AsyncMock(return_value=result)
    db.add.side_effect = lambda row: setattr(result.scalar_one_or_none, "return_value", row)
    db.commit = AsyncMock()

    async def offline_db():
        yield db

    async def offline_admin():
        return None

    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_db_session] = offline_db
    app.dependency_overrides[api.require_token] = offline_admin
    with TestClient(app) as client:
        yield client, result.scalar_one_or_none


def _upload(client, color):
    with BytesIO() as buffer:
        Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
        response = client.post(
            "/captaingift/add",
            data={"month": "202610"},
            files={"file": ("gift.png", buffer.getvalue(), "image/png")},
        )
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0


def test_reuploading_identical_captaingift_keeps_image_downloadable(gift_client):
    client, get_record = gift_client
    _upload(client, "red")
    path = get_record().image_path
    assert not Path(path).is_absolute()
    original = client.get("/captaingift/image", params={"path": path})
    assert original.status_code == 200

    _upload(client, "red")

    assert get_record().image_path == path
    repeated = client.get("/captaingift/image", params={"path": path})
    assert repeated.status_code == 200
    assert repeated.content == original.content


def test_uploading_different_captaingift_removes_old_image(gift_client):
    client, get_record = gift_client
    _upload(client, "red")
    old_path = get_record().image_path
    assert Path(old_path).is_file()

    _upload(client, "blue")

    new_path = get_record().image_path
    assert new_path != old_path
    assert not Path(old_path).exists()
    assert client.get("/captaingift/image", params={"path": new_path}).status_code == 200
