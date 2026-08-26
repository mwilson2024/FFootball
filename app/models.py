from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class LeagueType(StrEnum):
    KEEPER = "keeper"
    AUCTION = "auction"


class RosterStatus(StrEnum):
    ROSTER = "ROSTER"
    TAXI_SQUAD = "TAXI_SQUAD"
    INJURED_RESERVE = "INJURED_RESERVE"


class League(Base):
    __tablename__ = "leagues"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    season: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(200))
    roster_size: Mapped[int] = mapped_column(Integer)
    starting_budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    minimum_bid: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("1"))
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scoring_rules_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    lineup_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Franchise(Base):
    __tablename__ = "franchises"
    __table_args__ = (UniqueConstraint("league_id", "id"),)

    pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String, index=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String(200))
    abbreviation: Mapped[str | None] = mapped_column(String(20), nullable=True)
    starting_budget: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    roster_slots: Mapped[int] = mapped_column(Integer)


class Player(Base):
    __tablename__ = "players"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    position: Mapped[str] = mapped_column(String(20), index=True)
    nfl_team: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    birthdate: Mapped[date | None] = mapped_column(Date, nullable=True)
    rookie: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    fantasy_positions_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    injury_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    practice_participation: Mapped[str | None] = mapped_column(String(80), nullable=True)
    bye_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RosterAssignment(Base):
    __tablename__ = "roster_assignments"
    __table_args__ = (UniqueConstraint("league_id", "player_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    franchise_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=RosterStatus.ROSTER)
    salary: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    contract_info: Mapped[str | None] = mapped_column(String(200), nullable=True)


class RankingSnapshot(Base):
    __tablename__ = "ranking_snapshots"
    __table_args__ = (UniqueConstraint("league_id", "player_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    overall_rank: Mapped[int] = mapped_column(Integer)
    position_rank: Mapped[int] = mapped_column(Integer)
    tier: Mapped[int] = mapped_column(Integer)
    custom_score: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    projected_points: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    replacement_points: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    value_over_replacement: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    adp: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    mfl_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mfl_aav: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    baseline_auction_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    suggested_auction_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    source_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class KeeperSelection(Base):
    __tablename__ = "keeper_selections"
    __table_args__ = (UniqueConstraint("league_id", "player_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    franchise_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    keeper_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="local")
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionPurchase(Base):
    __tablename__ = "auction_purchases"
    __table_args__ = (UniqueConstraint("league_id", "player_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    league_id: Mapped[str] = mapped_column(String, index=True)
    franchise_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(32), default=RosterStatus.ROSTER)
    purchase_order: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(16), default="local")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionAuditEvent(Base):
    __tablename__ = "auction_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class MFLSnapshot(Base):
    __tablename__ = "mfl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    season: Mapped[int] = mapped_column(Integer)
    export_type: Mapped[str] = mapped_column(String(40), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ImportRecord(Base):
    __tablename__ = "import_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    payload_xml: Mapped[str] = mapped_column(Text)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PowerRankingSnapshot(Base):
    __tablename__ = "power_ranking_snapshots"

    league_id: Mapped[str] = mapped_column(String, primary_key=True)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    draft_round: Mapped[int] = mapped_column(Integer, default=0)
    auction_round: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[str] = mapped_column(String(80), default="startup")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserAccount(Base):
    __tablename__ = "user_accounts"

    username: Mapped[str] = mapped_column(String(100), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserPresence(Base):
    __tablename__ = "user_presence"

    username: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSourceSetting(Base):
    __tablename__ = "user_source_settings"
    __table_args__ = (UniqueConstraint("username", "source_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("1"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserAvoidedTeam(Base):
    __tablename__ = "user_avoided_teams"
    __table_args__ = (UniqueConstraint("username", "team"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    team: Mapped[str] = mapped_column(String(3), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserLeagueSetting(Base):
    __tablename__ = "user_league_settings"
    __table_args__ = (UniqueConstraint("username", "league_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    franchise_id: Mapped[str | None] = mapped_column(String, nullable=True)
    auction_strategy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserMFLMembership(Base):
    __tablename__ = "user_mfl_memberships"
    __table_args__ = (UniqueConstraint("username", "season", "league_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    league_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    franchise_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionLiveState(Base):
    __tablename__ = "auction_live_states"

    league_id: Mapped[str] = mapped_column(String, primary_key=True)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    current_player_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_franchise_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuctionNominationState(Base):
    __tablename__ = "auction_nomination_states"

    league_id: Mapped[str] = mapped_column(String, primary_key=True)
    order_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InteractiveAuctionState(Base):
    __tablename__ = "interactive_auction_states"

    league_id: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="idle")
    player_id: Mapped[str | None] = mapped_column(ForeignKey("players.id"), nullable=True)
    nominating_franchise_id: Mapped[str | None] = mapped_column(String, nullable=True)
    high_bid_franchise_id: Mapped[str | None] = mapped_column(String, nullable=True)
    current_bid: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    nominated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InteractiveAuctionBid(Base):
    __tablename__ = "interactive_auction_bids"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    league_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    franchise_id: Mapped[str] = mapped_column(String, index=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("1"))
    terms_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    license: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attribution: Mapped[str | None] = mapped_column(Text, nullable=True)
    cache_ttl_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncWarning(Base):
    __tablename__ = "sync_warnings"
    __table_args__ = (UniqueConstraint("league_id", "source", "message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    code: Mapped[str] = mapped_column(String(80), index=True)
    message: Mapped[str] = mapped_column(Text)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlayerIdentity(Base):
    __tablename__ = "player_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), unique=True, index=True)
    mfl_id: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    gsis_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    sleeper_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    espn_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    fantasypros_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    other_ids_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    match_method: Mapped[str] = mapped_column(String(40), default="exact_id")
    match_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1"))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourcePlayerValue(Base):
    __tablename__ = "source_player_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), index=True)
    league_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    value_type: Mapped[str] = mapped_column(String(40), index=True)
    raw_value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    normalized_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    snapshot_id: Mapped[str] = mapped_column(String(100), index=True)


class ConsensusSnapshot(Base):
    __tablename__ = "consensus_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    name: Mapped[str] = mapped_column(String(100), default="Generated consensus")
    source_weights_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    formula_version: Mapped[str] = mapped_column(String(30), default="consensus-v1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class PersonalPlayerPreference(Base):
    __tablename__ = "personal_player_preferences"
    __table_args__ = (UniqueConstraint("league_id", "player_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    manual_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_order: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target: Mapped[bool] = mapped_column(Boolean, default=False)
    fade: Mapped[bool] = mapped_column(Boolean, default=False)
    do_not_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserPlayerPreference(Base):
    __tablename__ = "user_player_preferences"
    __table_args__ = (UniqueConstraint("username", "league_id", "player_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    manual_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    manual_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_order: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    target: Mapped[bool] = mapped_column(Boolean, default=False)
    fade: Mapped[bool] = mapped_column(Boolean, default=False)
    do_not_draft: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DraftSession(Base):
    __tablename__ = "draft_sessions"
    __table_args__ = (UniqueConstraint("league_id", "season"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    league_id: Mapped[str] = mapped_column(String, index=True)
    season: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="pre_draft")
    current_round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_pick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="local")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MockDraftSession(Base):
    __tablename__ = "mock_draft_sessions"
    __table_args__ = (UniqueConstraint("league_id", "season"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    league_id: Mapped[str] = mapped_column(String, index=True)
    season: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MockDraftPick(Base):
    __tablename__ = "mock_draft_picks"
    __table_args__ = (
        UniqueConstraint("session_id", "player_id"),
        UniqueConstraint("session_id", "overall_pick"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("mock_draft_sessions.id"), index=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    franchise_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_pick: Mapped[int] = mapped_column(Integer, index=True)
    selected_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DraftPick(Base):
    __tablename__ = "draft_picks"
    __table_args__ = (
        UniqueConstraint("session_id", "player_id"),
        UniqueConstraint("session_id", "overall_pick"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    session_id: Mapped[str] = mapped_column(ForeignKey("draft_sessions.id"), index=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    player_id: Mapped[str] = mapped_column(ForeignKey("players.id"), index=True)
    franchise_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pick: Mapped[int | None] = mapped_column(Integer, nullable=True)
    overall_pick: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="local")
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    version: Mapped[int] = mapped_column(Integer, default=1)


class DraftAuditEvent(Base):
    __tablename__ = "draft_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    league_id: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    undone: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
