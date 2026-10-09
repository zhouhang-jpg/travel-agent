from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    amap_api_key: SecretStr | None = None
    qweather_api_key: SecretStr | None = None
    qweather_api_host: str | None = None
    bocha_api_key: SecretStr | None = None
    tool_timeout_seconds: float = Field(default=20, ge=1, le=60)
    max_concurrent_calls: int = Field(default=4, ge=1, le=16)


def has_secret(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())
