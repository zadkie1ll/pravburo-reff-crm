from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_debug: bool = False
    bitrix_webhook_url: str = ""
    bitrix_webhook_secret: str = ""
    bitrix_agent_source_id: str = "RECOMMENDATION"
    bitrix_client_category_id: int = 2
    bounty_service_url: str = "http://127.0.0.1:8041"
    site_service_url: str = "http://127.0.0.1:8040"
    internal_service_token: str = "development-internal-token"

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.app_env == "production":
            if not self.bitrix_webhook_url or not self.bitrix_webhook_secret:
                raise ValueError("Bitrix URL and webhook secret are required in production")
            if self.internal_service_token == "development-internal-token":
                raise ValueError("INTERNAL_SERVICE_TOKEN must be configured in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
