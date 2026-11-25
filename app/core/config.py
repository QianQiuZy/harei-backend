from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas.member import Email


class Settings(BaseSettings):
    app_env: str = "development"
    app_secret_key: str

    mysql_host: str
    mysql_port: int = 3306
    mysql_user: str
    mysql_password: str
    mysql_database: str
    mysql_pool_size: int = Field(5, ge=1, le=100)
    mysql_max_overflow: int = Field(3, ge=0, le=100)
    mysql_pool_timeout: float = Field(5, gt=0, le=60)
    mysql_pool_recycle: int = Field(1800, gt=0)

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
    smtp_starttls: bool = True
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    email_from: str = ""
    email_to: str = ""
    email_cc: str = ""

    gift_mail_rate_redis_url: SecretStr = SecretStr("")
    gift_smtp_host: str = "gz-smtp.qcloudmail.com"
    gift_smtp_port: int = 465
    gift_smtp_ca_file: str = ""
    gift_email_from: str = "noreply@harei.cn"
    gift_roster_recipient: Email = "harei0301@126.com"
    gift_smtp_pass: SecretStr = SecretStr("")

    gift_address_key: str = ""
    member_site_url: str = "https://harei.cn"

    token_ttl_seconds: int = 60 * 60 * 24 * 7

    auth_username: str
    auth_password_hash: str
    music_auth_username: str = ""
    music_auth_password_hash: str = ""
    music_token_ttl_seconds: int | None = None

    cors_allow_origins: str = "https://harei.cn,https://api.harei.cn"
    trusted_proxy_hosts: str = "127.0.0.1,::1"
    trusted_edgeone_proxy_hosts: str = ""

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, hide_input_in_errors=True)

    @property
    def local_mode(self) -> bool:
        return self.app_env.lower() in {"dev", "development", "local", "test"}

    @model_validator(mode="after")
    def check_production_secret(self):
        if not self.local_mode and len(self.app_secret_key.strip().encode()) < 32:
            raise ValueError("生产环境 APP_SECRET_KEY 至少需要 32 字节，请使用独立的随机密钥")
        return self

    @property
    def cors_allow_origins_list(self) -> list[str]:
        origins = [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]
        if self.local_mode:
            origins.extend(["http://localhost:3000", "http://127.0.0.1:3000"])
        return list(dict.fromkeys(origins))

    @property
    def email_cc_list(self) -> list[str]:
        return [email.strip() for email in self.email_cc.split(",") if email.strip()]

    @property
    def trusted_proxy_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.trusted_proxy_hosts.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
