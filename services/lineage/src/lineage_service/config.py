import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lineage:lineage@localhost:5433/lineage"
    support_agent_url: str = "http://localhost:8001"
    service_token: SecretStr = Field(min_length=32)
    session_secret: SecretStr = Field(min_length=32)
    session_ttl_hours: int = 12
    cookie_secure: bool = False
    lineage_api_key: SecretStr | None = None
    lineage_retention_days: int = Field(default=30, ge=1)
    admin_email: str | None = None
    admin_password: SecretStr | None = None
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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
