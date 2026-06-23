from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..currency import normalize_currency_code


class WhatsAppSettings(BaseSettings):
    WHATSAPP_DATABASE_URL: SecretStr = Field(..., min_length=1)
    WHATSAPP_ACCESS_TOKEN: SecretStr = Field(..., min_length=1)
    WHATSAPP_PHONE_NUMBER_ID: str = Field(..., min_length=1)
    WHATSAPP_BUSINESS_PHONE: str = Field(..., min_length=1)
    WHATSAPP_VERIFY_TOKEN: SecretStr = Field(..., min_length=1)
    WHATSAPP_API_VERSION: str = Field(default="v23.0", min_length=1)
    DEFAULT_CURRENCY: str = Field(default="ILS", min_length=3, max_length=3)

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("DEFAULT_CURRENCY")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        return normalize_currency_code(value)

    @field_validator("WHATSAPP_API_VERSION")
    @classmethod
    def _normalize_api_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("WHATSAPP_API_VERSION is required")
        return normalized if normalized.startswith("v") else f"v{normalized}"

    @field_validator("WHATSAPP_BUSINESS_PHONE")
    @classmethod
    def _normalize_business_phone(cls, value: str) -> str:
        normalized = "".join(char for char in value if char.isdigit())
        if not normalized:
            raise ValueError("WHATSAPP_BUSINESS_PHONE must contain digits")
        return normalized

    @property
    def database_url(self) -> str:
        return self.WHATSAPP_DATABASE_URL.get_secret_value()

    @property
    def access_token(self) -> str:
        return self.WHATSAPP_ACCESS_TOKEN.get_secret_value()

    @property
    def verify_token(self) -> str:
        return self.WHATSAPP_VERIFY_TOKEN.get_secret_value()

    @property
    def graph_base_url(self) -> str:
        return f"https://graph.facebook.com/{self.WHATSAPP_API_VERSION}"


@lru_cache(maxsize=1)
def get_whatsapp_settings() -> WhatsAppSettings:
    return WhatsAppSettings()

