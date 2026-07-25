from dataclasses import dataclass
from datetime import datetime
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from app.core.redis import get_redis_client
from app.services.auth_service import AuthService


security = HTTPBearer(auto_error=False)


async def get_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return credentials.credentials

@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    scopes: frozenset[str]
    expires_at: datetime

async def get_current_principal(token: str = Depends(get_bearer_token), redis: Redis = Depends(get_redis_client)) -> Principal:
    principal = await AuthService(redis).get_principal_by_token(token)
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return Principal(subject=principal.subject, scopes=frozenset(principal.scopes), expires_at=principal.expires_at)

def require_scope(scope: str):
    async def dependency(principal: Principal = Depends(get_current_principal)) -> Principal:
        if scope not in principal.scopes:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return principal
    return dependency

async def require_admin(principal: Principal = Depends(get_current_principal)) -> Principal:
    if "admin" not in principal.scopes:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return principal

require_music_manage = require_scope("music:manage")
