from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    amap_api_key: SecretStr | None = None
    qweather_api_key: SecretStr | None = None
    qweather_api_host: str | None = None
    bocha_api_key: SecretStr | None = None
    juhe_train_api_key: SecretStr | None = None
    train_search_provider: Literal["flyai", "juhe"] = "flyai"
    jisu_coach_api_key: SecretStr | None = None
    flyai_api_key: SecretStr | None = None
    flyai_enable_demo: bool = False
    flyai_node_path: str | None = None
    flyai_cli_path: str | None = None
    flyai_state_directory: str = "private/flyai"
    llm_provider: Literal["deepseek", "openai_compatible"] = "deepseek"
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_thinking: bool = True
    llm_reasoning_effort: Literal["low", "high", "max"] = "high"
    llm_timeout_seconds: float = Field(default=120, ge=5, le=180)
    database_url: str = "sqlite+aiosqlite:///private/travel-agent.db"
    agent_max_steps: int = Field(default=12, ge=1, le=40)
    agent_max_run_seconds: float = Field(default=300, ge=10, le=600)
    tool_timeout_seconds: float = Field(default=20, ge=1, le=60)
    max_concurrent_calls: int = Field(default=4, ge=1, le=16)


def has_secret(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())
