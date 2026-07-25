from datetime import datetime
from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    username: str

class SessionPrincipal(BaseModel):
    version: int
    subject: str
    scopes: list[str]
    issued_at: datetime
    expires_at: datetime


class LoginResponse(BaseModel):
    code: int = 0
    token: str
    user: UserInfo
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime


class AuthResponse(BaseModel):
    code: int = 0
    authenticated: bool
    user: UserInfo
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime
