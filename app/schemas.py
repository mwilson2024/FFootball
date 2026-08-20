from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import RosterStatus


class PurchaseCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    league_id: str
    franchise_id: str
    player_id: str
    amount: Decimal = Field(gt=0)
    status: RosterStatus = RosterStatus.ROSTER

    @field_validator("league_id", "franchise_id", "player_id", mode="before")
    @classmethod
    def preserve_id(cls, value: object) -> str:
        return str(value)


class PurchaseUpdate(BaseModel):
    franchise_id: str | None = None
    player_id: str | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    status: RosterStatus | None = None
    version: int

    @field_validator("franchise_id", "player_id", mode="before")
    @classmethod
    def preserve_update_ids(cls, value: object) -> str | None:
        return None if value is None else str(value)


class KeeperCreate(BaseModel):
    league_id: str
    franchise_id: str
    player_id: str
    keeper_cost: Decimal | None = None


class ImportConfirmation(BaseModel):
    league_id: str
    confirmation_token: str
    clear: bool = False
    overwrite: bool = False


class SetupUpdate(BaseModel):
    season: int = Field(ge=2020, le=2100)
    keeper_league_id: str = ""
    auction_league_id: str = ""
    keeper_api_key: str | None = None
    auction_api_key: str | None = None
    fantasypros_api_key: str | None = None
    user_agent: str = Field(min_length=10, max_length=250)

    @field_validator("keeper_league_id", "auction_league_id", mode="before")
    @classmethod
    def preserve_setup_ids(cls, value: object) -> str:
        return "" if value is None else str(value).strip()


class MFLConnectionTest(BaseModel):
    season: int = Field(ge=2020, le=2100)
    league_id: str
    api_key: str | None = None

    @field_validator("league_id", mode="before")
    @classmethod
    def preserve_test_id(cls, value: object) -> str:
        return str(value).strip()


class SourceUpdate(BaseModel):
    enabled: bool
    weight: Decimal = Field(ge=0, le=10)


class SourcePreview(BaseModel):
    league_id: str
    sources: dict[str, SourceUpdate]


class UserLeagueSettingUpdate(BaseModel):
    franchise_id: str | None = None
    auction_strategy: dict[str, object] = Field(default_factory=dict)


class LeagueConnect(BaseModel):
    league_id: str
    league_type: str = Field(pattern="^(keeper|auction)$")

    @field_validator("league_id", mode="before")
    @classmethod
    def preserve_connected_league_id(cls, value: object) -> str:
        return str(value).strip()


class LeagueFormatUpdate(BaseModel):
    league_type: str = Field(pattern="^(keeper|auction)$")


class AuctionLiveUpdate(BaseModel):
    is_live: bool


class DraftModeUpdate(BaseModel):
    mode: str = Field(pattern="^(companion|local)$")


class AuctionStageUpdate(BaseModel):
    enabled: bool


class InteractiveAuctionUpdate(BaseModel):
    enabled: bool


class InteractiveAuctionNominationCreate(BaseModel):
    league_id: str
    player_id: str

    @field_validator("league_id", "player_id", mode="before")
    @classmethod
    def preserve_interactive_nomination_ids(cls, value: object) -> str:
        return str(value).strip()


class InteractiveAuctionBidCreate(BaseModel):
    league_id: str
    amount: Decimal = Field(gt=0)

    @field_validator("league_id", mode="before")
    @classmethod
    def preserve_interactive_bid_league_id(cls, value: object) -> str:
        return str(value).strip()


class MockDraftUpdate(BaseModel):
    enabled: bool


class AuctionRobModeUpdate(BaseModel):
    enabled: bool


class CommissionerImportsUpdate(BaseModel):
    enabled: bool


class AdminRoleUpdate(BaseModel):
    is_admin: bool


class AuctionNominationOrderUpdate(BaseModel):
    franchise_ids: list[str] = Field(min_length=1)

    @field_validator("franchise_ids", mode="before")
    @classmethod
    def preserve_nomination_ids(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("Franchise order must be a list")
        return [str(item) for item in value]


class AssistantRequest(BaseModel):
    league_id: str
    message: str = Field(min_length=1, max_length=2000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=20)


class PlayerComparisonRequest(BaseModel):
    player_ids: list[str] = Field(min_length=2, max_length=5)

    @field_validator("player_ids", mode="before")
    @classmethod
    def preserve_comparison_ids(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("Choose two to five players")
        player_ids = [str(item).strip() for item in value]
        if len(set(player_ids)) != len(player_ids):
            raise ValueError("Choose each player only once")
        return player_ids


class WarningResolve(BaseModel):
    resolved: bool = True


class PreferenceUpdate(BaseModel):
    manual_rank: int | None = Field(default=None, ge=1)
    manual_tier: int | None = Field(default=None, ge=1)
    queue_order: int | None = Field(default=None, ge=1)
    target: bool = False
    fade: bool = False
    do_not_draft: bool = False
    notes: str | None = Field(default=None, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=20)


class IdentityUpdate(BaseModel):
    gsis_id: str | None = None
    sleeper_id: str | None = None
    espn_id: str | None = None
    verified: bool = True


class DraftPickCreate(BaseModel):
    league_id: str
    player_id: str
    franchise_id: str | None = None
    round: int | None = Field(default=None, ge=1)
    pick: int | None = Field(default=None, ge=1)
    overall_pick: int | None = Field(default=None, ge=1)
    is_mock: bool = False

    @field_validator("league_id", "player_id", "franchise_id", mode="before")
    @classmethod
    def preserve_draft_ids(cls, value: object) -> str | None:
        return None if value is None else str(value).strip()


class DraftPickUpdate(BaseModel):
    player_id: str | None = None
    franchise_id: str | None = None
    round: int | None = Field(default=None, ge=1)
    pick: int | None = Field(default=None, ge=1)
    overall_pick: int | None = Field(default=None, ge=1)
    version: int = Field(ge=1)

    @field_validator("player_id", "franchise_id", mode="before")
    @classmethod
    def preserve_updated_draft_ids(cls, value: object) -> str | None:
        return None if value is None else str(value).strip()
