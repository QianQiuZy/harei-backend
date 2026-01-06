from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis

from app.core.redis import get_redis_client
from app.deps.auth import get_bearer_token
from app.schemas.auth import AuthResponse, LoginRequest, LoginResponse, UserInfo
from app.services.auth_service import AuthService

router = APIRouter()


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, redis: Redis = Depends(get_redis_client)) -> LoginResponse:
    service = AuthService(redis)
    if not service.verify_credentials(payload.username, payload.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    token = await service.issue_token(payload.username)
    return LoginResponse(token=token, user=UserInfo(username=payload.username), code=0)


@router.post("/logout")
async def logout(
    token: str = Depends(get_bearer_token),
    redis: Redis = Depends(get_redis_client),
) -> dict:
    service = AuthService(redis)
    revoked = await service.revoke_token(token)
    if not revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return {"code": 0, "success": True}


@router.get("/auth", response_model=AuthResponse)
async def auth(
    token: str = Depends(get_bearer_token),
    redis: Redis = Depends(get_redis_client),
) -> AuthResponse:
    service = AuthService(redis)
    username = await service.get_username_by_token(token)
    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return AuthResponse(authenticated=True, user=UserInfo(username=username), code=0)
