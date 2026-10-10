from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SupplierLimit(BaseModel):
    concurrency: int = Field(default=1, ge=1, le=16)
    requests_per_second: float = Field(default=1, ge=0, le=50)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    amap_api_key: SecretStr | None = None
    qweather_api_key: SecretStr | None = None
    qweather_api_host: str | None = None
    bocha_api_key: SecretStr | None = None
    juhe_train_api_key: SecretStr | None = None
    train_search_provider: Literal["12306", "flyai", "juhe"] = "12306"
    train_fallback_provider: Literal["flyai", "none"] = "flyai"
    coach_search_provider: Literal["bus365", "jisu"] = "bus365"
    browser_queries_enabled: bool = False
    browser_max_concurrent_queries: int = Field(default=2, ge=1, le=4)
    supplier_limits: dict[str, SupplierLimit] = Field(
        default_factory=lambda: {
            "amap": SupplierLimit(concurrency=2, requests_per_second=5),
            "qweather": SupplierLimit(concurrency=2, requests_per_second=3),
            "bocha": SupplierLimit(concurrency=1, requests_per_second=2),
            "public_web": SupplierLimit(concurrency=2, requests_per_second=2),
            "12306": SupplierLimit(),
            "bus365": SupplierLimit(),
            "ceair": SupplierLimit(),
            "flyai": SupplierLimit(),
            "juhe_train": SupplierLimit(),
            "jisu_coach": SupplierLimit(),
        }
    )
    cache_places_seconds: float = Field(default=600, ge=0, le=3600)
    cache_weather_seconds: float = Field(default=120, ge=0, le=600)
    cache_routes_seconds: float = Field(default=60, ge=0, le=300)
    cache_web_seconds: float = Field(default=120, ge=0, le=600)
    cache_quotes_seconds: float = Field(default=20, ge=0, le=60)
    browser_query_timeout_seconds: float = Field(default=18, ge=3, le=55)
    browser_query_cache_seconds: float = Field(default=0, ge=0, le=300)
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
    agent_engine: Literal["legacy", "langgraph"] = "langgraph"
    agent_max_steps: int = Field(default=12, ge=1, le=40)
    agent_max_run_seconds: float = Field(default=300, ge=10, le=600)
    tool_timeout_seconds: float = Field(default=20, ge=1, le=60)
    max_concurrent_calls: int = Field(default=4, ge=1, le=16)


def has_secret(secret: SecretStr | None) -> bool:
    return secret is not None and bool(secret.get_secret_value().strip())
