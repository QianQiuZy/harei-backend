from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis

from app.core.redis import get_redis_client
from app.deps.auth import Principal, get_bearer_token, get_current_principal
from app.schemas.auth import AuthResponse, LoginRequest, LoginResponse, UserInfo
from app.services.auth_service import AuthService

router = APIRouter()


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, redis: Redis = Depends(get_redis_client)) -> LoginResponse:
    service = AuthService(redis)
    if not service.verify_credentials(payload.username, payload.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    token, principal = await service.issue_token(payload.username, ["admin", "music:manage"])
    return LoginResponse(token=token, user=UserInfo(username=payload.username), scopes=principal.scopes, expires_at=principal.expires_at, code=0)


@router.post("/logout")
async def logout(
    token: str = Depends(get_bearer_token),
    redis: Redis = Depends(get_redis_client),
) -> dict:
    service = AuthService(redis)
    await service.revoke_token(token)
    return {"code": 0, "success": True}


@router.get("/auth", response_model=AuthResponse)
async def auth(
    principal: Principal = Depends(get_current_principal),
) -> AuthResponse:
    return AuthResponse(authenticated=True, user=UserInfo(username=principal.subject), scopes=sorted(principal.scopes), expires_at=principal.expires_at, code=0)
