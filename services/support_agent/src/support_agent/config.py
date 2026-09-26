import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://store:store@localhost:5433/store"

    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "anthropic/claude-sonnet-5"
    llm_fallback_model: str | None = None
    llm_mode: Literal["live", "recorded"] = "live"
    llm_timeout_s: float = 30.0
    llm_max_iterations: int = 8

    refund_approval_threshold: float = 200.0
    # Test-only: restores the original `ORDER BY updated_at` lookup defect (see SPEC §4.4).
    legacy_order_lookup: bool = False
    # Mock connector behaviour.
    connector_latency_ms: tuple[int, int] = (20, 150)
    connector_error_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    lineage_url: str | None = "http://localhost:8002"
    lineage_api_key: SecretStr | None = None
    lineage_redaction_key: SecretStr = Field(min_length=32)
    service_token: SecretStr = Field(min_length=32)
    # AES key material for stored connector credentials; derived from SERVICE_TOKEN when unset.
    connector_encryption_key: SecretStr | None = None
    # Shared with the lineage service so operator sessions work across both APIs.
    session_secret: SecretStr | None = None

    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173", "http://localhost:8080"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        # Accept "https://a,https://b" (and a JSON list) from the environment.
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    log_level: str = "INFO"

    @model_validator(mode="after")
    def _live_mode_needs_key(self) -> "Settings":
        if self.llm_mode == "live" and not self.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required when LLM_MODE=live (use LLM_MODE=recorded offline)")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
