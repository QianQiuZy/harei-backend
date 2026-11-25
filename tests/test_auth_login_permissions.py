"""Exercise real login routes, Argon2 verification and token scope enforcement.

Only synthetic credentials and in-memory Redis are used; importing app.main
would load production settings and start unrelated services, so use a small app.
"""

import importlib
import sys
import types
from unittest.mock import AsyncMock

from argon2 import PasswordHasher
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
import pytest

from app.core.redis import get_rate_limit_redis


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def set(self, key, value, ex):
        self.values[key] = value
        self.ttls[key] = ex

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        return int(self.values.pop(key, None) is not None)


@pytest.fixture(scope="module")
def credential_hashes():
    hasher = PasswordHasher()
    return hasher.hash("test-admin-password"), hasher.hash("test-music-password")


@pytest.fixture
def login_app(monkeypatch, credential_hashes):
    settings = types.SimpleNamespace(
        auth_username="test-admin", auth_password_hash=credential_hashes[0],
        music_auth_username="test-music", music_auth_password_hash=credential_hashes[1],
        token_ttl_seconds=7200, music_token_ttl_seconds=1800,
    )
    monkeypatch.setattr("app.core.rate_limit.client_ip", lambda request: "127.0.0.1")
    db = types.ModuleType("app.db.session")

    async def offline_db():
        yield types.SimpleNamespace(scalar=AsyncMock(return_value=0))

    db.get_db_session = offline_db
    monkeypatch.setitem(sys.modules, "app.db.session", db)
    auth_service = importlib.import_module("app.services.auth_service")
    auth_api = importlib.import_module("app.api.auth")
    music_api = importlib.import_module("app.api.music_manage")
    auth_deps = importlib.import_module("app.deps.auth")
    monkeypatch.setattr(auth_service, "get_settings", lambda: settings)
    monkeypatch.setattr(music_api, "get_settings", lambda: settings)

    redis = MemoryRedis()

    async def offline_redis():
        return redis

    # This fixture exercises credential verification and scopes. Use a separate
    # in-memory quota dependency; the integration test runs the Lua script on Redis.
    limits = types.SimpleNamespace(eval=AsyncMock(return_value=0))
    app = FastAPI()
    app.dependency_overrides[get_rate_limit_redis] = lambda: limits
    app.include_router(auth_api.router)
    app.include_router(music_api.router)
    app.dependency_overrides[auth_api.get_redis_client] = offline_redis
    app.dependency_overrides[music_api.get_db_session] = offline_db

    @app.get("/admin-probe")
    async def admin_probe(principal=Depends(auth_deps.require_admin)):
        return {"subject": principal.subject}

    with TestClient(app) as client:
        yield client, redis, settings


def log_in(client, path, username, password):
    return client.post(path, json={"username": username, "password": password})


@pytest.mark.parametrize("path", ["/login", "/music-manage/login"])
def test_admin_credentials_log_in_on_both_sides(login_app, path):
    client, redis, settings = login_app
    response = log_in(client, path, "test-admin", "test-admin-password")
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["username"] == "test-admin"
    assert set(body["scopes"]) == {"admin", "music:manage"}
    assert redis.ttls[f"token:{body['token']}"] == settings.token_ttl_seconds
    headers = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/admin-probe", headers=headers).status_code == 200
    assert client.get("/music-manage/stats", headers=headers).status_code == 200
    principal = client.get("/auth", headers=headers)
    assert principal.status_code == 200
    assert set(principal.json()["scopes"]) == {"admin", "music:manage"}


def test_music_credentials_only_log_in_on_music_side(login_app):
    client, redis, settings = login_app
    assert log_in(client, "/login", "test-music", "test-music-password").status_code == 401
    assert not redis.values
    response = log_in(client, "/music-manage/login", "test-music", "test-music-password")
    assert response.status_code == 200
    body = response.json()
    assert body["scopes"] == ["music:manage"]
    assert redis.ttls[f"token:{body['token']}"] == settings.music_token_ttl_seconds
    headers = {"Authorization": f"Bearer {body['token']}"}
    assert client.get("/music-manage/stats", headers=headers).status_code == 200
    assert client.get("/admin-probe", headers=headers).status_code == 403


@pytest.mark.parametrize("path", ["/login", "/music-manage/login"])
@pytest.mark.parametrize("username,password", [
    ("test-admin", "wrong-password"), ("test-music", "wrong-password"),
    ("unknown-user", "test-admin-password"), ("test-admin", "test-music-password"),
    ("test-music", "test-admin-password"),
])
def test_invalid_credentials_never_issue_token(login_app, path, username, password):
    client, redis, _ = login_app
    assert log_in(client, path, username, password).status_code == 401
    assert not redis.values


def test_admin_can_use_music_login_without_music_account_configured(login_app):
    client, _, settings = login_app
    settings.music_auth_username = ""
    settings.music_auth_password_hash = ""
    assert log_in(client, "/music-manage/login", "test-admin", "test-admin-password").status_code == 200


def test_music_ttl_defaults_to_normal_ttl(login_app):
    client, redis, settings = login_app
    settings.music_token_ttl_seconds = None
    response = log_in(client, "/music-manage/login", "test-music", "test-music-password")
    assert response.status_code == 200
    assert redis.ttls[f"token:{response.json()['token']}"] == settings.token_ttl_seconds


def test_same_username_different_password_does_not_escalate_music_account(login_app):
    client, _, settings = login_app
    settings.music_auth_username = settings.auth_username
    response = log_in(client, "/music-manage/login", settings.auth_username, "test-music-password")
    assert response.status_code == 200
    assert response.json()["scopes"] == ["music:manage"]
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    assert client.get("/admin-probe", headers=headers).status_code == 403
    assert log_in(client, "/login", settings.auth_username, "test-music-password").status_code == 401


def test_logout_revokes_music_login_admin_token(login_app):
    client, _, _ = login_app
    response = log_in(client, "/music-manage/login", "test-admin", "test-admin-password")
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    assert client.post("/logout", headers=headers).status_code == 200
    assert client.get("/auth", headers=headers).status_code == 401
    assert client.get("/admin-probe", headers=headers).status_code == 401
    assert client.get("/music-manage/stats", headers=headers).status_code == 401
