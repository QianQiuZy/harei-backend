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

    # -------------------------
    # B站直播监听（Captain only）
    # -------------------------
    bili_monitor_enabled: bool = False
    bili_room_ids: str = ""  # 逗号分隔，可选：覆盖默认 ROOM_IDS
    bili_sessdata: str = ""
    bili_bili_jct: str = ""
    bili_dedeuserid: str = ""
    bili_dedeuserid_ckmd5: str = ""
    bili_sid: str = ""
    bili_buvid3: str = ""
    bili_device_fingerprint: str = ""

    # -------------------------
    # SMTP（Cookies 失效告警）
    # -------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    email_from: str = ""
    email_to: str = ""

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
