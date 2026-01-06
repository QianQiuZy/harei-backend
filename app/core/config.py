from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_secret_key: str

    mysql_host: str
    mysql_port: int = 3306
    mysql_user: str
    mysql_password: str
    mysql_database: str

    redis_url: str
    token_ttl_seconds: int = 60 * 60 * 24 * 7

    auth_username: str
    auth_password_hash: str

    cors_allow_origins: str = "https://harei.cn,https://api.harei.cn"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    @property
    def cors_allow_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
