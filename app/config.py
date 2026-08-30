from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./data/fantasy_draft.db"
    database_pool_size: int = Field(default=10, ge=1, le=25)
    database_max_overflow: int = Field(default=15, ge=0, le=24)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=120)
    mfl_season: int = 2026
    mfl_keeper_league_id: str = ""
    mfl_auction_league_id: str = ""
    mfl_keeper_api_key: str = ""
    mfl_auction_api_key: str = ""
    mfl_username: str = ""
    mfl_password: str = ""
    mfl_user_agent: str = "MFLDraftManager/1.0 contact@example.com"
    fantasypros_api_key: str = ""
    auth_required: bool = True
    session_secret: str = ""
    session_max_age_days: int = 30
    allowed_hosts: str = "localhost,127.0.0.1,testserver,*.up.railway.app,healthcheck.railway.app"
    auto_sync_enabled: bool = True
    auto_sync_timezone: str = "America/New_York"
    auto_sync_hour: int = Field(default=1, ge=0, le=23)
    admin_usernames: str = "wilsonmw"
    openai_api_key: str = ""
    openai_model: str = "gpt-5.6"
    openai_base_url: str = "https://api.openai.com/v1"
    auction_default_budget: Decimal = Decimal("200")
    auction_min_bid: Decimal = Decimal("1")
    export_directory: Path = Field(default=Path("exports"))
    audit_directory: Path = Field(default=Path("data/audit"))
    mfl_enable_imports: bool = False

    @field_validator("mfl_keeper_league_id", "mfl_auction_league_id", mode="before")
    @classmethod
    def ids_stay_strings(cls, value: object) -> str:
        return "" if value is None else str(value)

    @model_validator(mode="after")
    def database_connection_limit(self) -> "Settings":
        if self.database_pool_size + self.database_max_overflow > 25:
            raise ValueError("database pool size plus overflow cannot exceed 25 connections")
        return self

    @property
    def commissioner_configured(self) -> bool:
        return bool(self.mfl_username and self.mfl_password and self.mfl_enable_imports)

    def api_key_for(self, league_id: str) -> str | None:
        if league_id == self.mfl_keeper_league_id:
            return self.mfl_keeper_api_key or None
        if league_id == self.mfl_auction_league_id:
            return self.mfl_auction_api_key or None
        return None

    @property
    def allowed_host_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]

    @property
    def admin_username_set(self) -> set[str]:
        return {item.strip().casefold() for item in self.admin_usernames.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
