from pydantic import BaseModel


class LoginRequest(BaseModel):
    username: str
    password: str


class UserInfo(BaseModel):
    username: str


class LoginResponse(BaseModel):
    code: int = 0
    token: str
    user: UserInfo


class AuthResponse(BaseModel):
    code: int = 0
    authenticated: bool
    user: UserInfo
