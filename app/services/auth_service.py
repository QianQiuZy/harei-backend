import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from redis.asyncio import Redis

from app.core.config import get_settings

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

    async def issue_token(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        await self.redis.setex(
            self._token_key(token),
            self.settings.token_ttl_seconds,
            username,
        )
        return token

    async def revoke_token(self, token: str) -> bool:
        deleted = await self.redis.delete(self._token_key(token))
        return deleted > 0

    async def get_username_by_token(self, token: str) -> str | None:
        return await self.redis.get(self._token_key(token))

    @staticmethod
    def _token_key(token: str) -> str:
        return f"token:{token}"
