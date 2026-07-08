from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_host: str = "127.0.0.1"
    app_port: int = 8095
    app_base_url: str = "http://127.0.0.1:8095"

    xyranet_api_base_url: str = "https://xyranet.pro/api/wholesale"
    xyranet_api_key: str = ""
    xyranet_timeout_seconds: float = 30.0

    digiseller_seller_id: str = ""
    digiseller_api_key: str = ""
    ggsel_seller_id: str = ""
    ggsel_api_key: str = ""

    telegram_bot_token: str = ""
    admin_ids: list[int] = Field(default_factory=list)
    admin_username: str = "admin"
    admin_password: str = "change-me"

    database_path: str = "data/reseller.sqlite3"
    panel_language: str = "ru"
    enable_telegram: bool = True
    notify_new_purchases: bool = True
    notify_chat_messages: bool = True
    notify_errors: bool = True
    notify_pending: bool = True
    notify_daily_statistics: bool = False
    free_reissue_enabled: bool = True
    digiseller_unique_code_request_enabled: bool = True
    digiseller_unique_code_request_delay_minutes: float = 15.0
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: Any) -> list[int]:
        if value in (None, ""):
            return []
        if isinstance(value, list):
            return [int(item) for item in value if str(item).strip()]
        return [int(item.strip()) for item in str(value).replace(";", ",").split(",") if item.strip()]

    @property
    def database_file(self) -> Path:
        return Path(self.database_path)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
