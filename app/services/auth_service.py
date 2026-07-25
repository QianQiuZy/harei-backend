import secrets
import json
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from redis.asyncio import Redis

from app.core.config import get_settings
from app.schemas.auth import SessionPrincipal

password_hasher = PasswordHasher()


class AuthService:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self.settings = get_settings()

    def verify_credentials(self, username: str, password: str) -> bool:
        if username != self.settings.auth_username:
            return False
        try:
            return password_hasher.verify(self.settings.auth_password_hash, password)
        except VerifyMismatchError:
            return False

    def verify_music_credentials(self, username: str, password: str) -> bool:
        if not self.settings.music_auth_username or username != self.settings.music_auth_username:
            return False
        try:
            return password_hasher.verify(self.settings.music_auth_password_hash, password)
        except VerifyMismatchError:
            return False

    async def issue_token(self, username: str, scopes: list[str], ttl_seconds: int | None = None) -> tuple[str, SessionPrincipal]:
        token = secrets.token_urlsafe(32)
        ttl = ttl_seconds or self.settings.token_ttl_seconds
        issued_at = datetime.now(UTC)
        expires_at = issued_at + timedelta(seconds=ttl)
        principal = SessionPrincipal(version=1, subject=username, scopes=scopes, issued_at=issued_at, expires_at=expires_at)
        await self.redis.set(
            self._token_key(token),
            principal.model_dump_json(),
            ex=ttl,
        )
        return token, principal

    async def revoke_token(self, token: str) -> bool:
        deleted = await self.redis.delete(self._token_key(token))
        return deleted > 0

    async def get_principal_by_token(self, token: str) -> SessionPrincipal | None:
        value = await self.redis.get(self._token_key(token))
        if value is None:
            return None
        try:
            principal = SessionPrincipal.model_validate_json(value)
        except (ValueError, json.JSONDecodeError):
            return None
        if principal.version != 1 or principal.expires_at <= datetime.now(UTC):
            return None
        return principal

    @staticmethod
    def _token_key(token: str) -> str:
        return f"token:{token}"
