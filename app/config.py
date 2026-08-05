from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./data/fantasy_draft.db"
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
    allowed_hosts: str = (
        "localhost,127.0.0.1,testserver,*.up.railway.app,healthcheck.railway.app"
    )
    auction_default_budget: Decimal = Decimal("200")
    auction_min_bid: Decimal = Decimal("1")
    export_directory: Path = Field(default=Path("exports"))
    mfl_enable_imports: bool = False

    @field_validator("mfl_keeper_league_id", "mfl_auction_league_id", mode="before")
    @classmethod
    def ids_stay_strings(cls, value: object) -> str:
        return "" if value is None else str(value)

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
