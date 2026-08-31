from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote, urlparse

import httpx
from fastapi import (
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.assistant import ask_assistant
from app.auction import (
    AuctionValidationError,
    add_purchase,
    advance_nomination,
    delete_purchase,
    franchise_budget,
    nomination_state,
    redo,
    reset_auction,
    reset_nomination_cursor,
    set_nomination_order,
    shuffle_nomination_order,
    undo,
    update_purchase,
)
from app.audit_backup import append_audit_event
from app.auth import (
    SESSION_COOKIE,
    clear_login_failures,
    login_allowed,
    login_rate_key,
    make_session_token,
    mfl_memberships,
    read_session_token,
    record_login_failure,
    resolve_session_secret,
)
from app.automation import daily_sync_loop, live_draft_sync_loop, stop_daily_sync
from app.bye_advisor import bye_week_advice
from app.catalog import (
    draftable_consensus,
    player_detail,
    player_filters,
    query_players,
    roster_overview,
)
from app.config import get_settings
from app.consensus import create_consensus_snapshot, parse_ranking_csv
from app.db import SessionLocal, get_db, init_db
from app.depth_charts import depth_chart_overview
from app.draft import (
    DraftValidationError,
    add_mock_pick,
    add_pick,
    apply_reconciliation,
    draft_intelligence,
    draft_state,
    export_draft_csv,
    franchise_position_needs,
    mock_draft_state,
    mock_draft_status,
    mock_pick_json,
    pick_json,
    recommendations,
    reconcile_preview,
    remove_pick,
    reset_mock_draft,
    set_draft_live,
    set_mock_draft_enabled,
    undo_draft,
    update_pick,
)
from app.draft_links import draft_link_groups
from app.exports import build_xml, export_csv, export_xml
from app.mfl import MFLAuthenticationError, MFLClient, MFLError
from app.mfl_draft_import import prepare_draft_results_import
from app.models import (
    AuctionLiveState,
    AuctionPurchase,
    DataSource,
    DraftPick,
    DraftSession,
    Franchise,
    ImportRecord,
    InteractiveAuctionBid,
    InteractiveAuctionState,
    KeeperSelection,
    League,
    LeagueType,
    MFLSnapshot,
    Player,
    PlayerIdentity,
    RosterAssignment,
    SourcePlayerValue,
    SyncWarning,
    UserAccount,
    UserMFLMembership,
    UserPlayerPreference,
    UserPresence,
    UserSourceSetting,
)
from app.power_cache import (
    cached_draft_analysis,
    cached_power_rankings,
    refresh_all_power_snapshots_job,
    refresh_power_snapshot_job,
    round_refresh_due,
    stored_team_power,
)
from app.power_rankings import chatgpt_power_rankings
from app.realtime import league_events
from app.schemas import (
    AdminRoleUpdate,
    AssistantRequest,
    AuctionLiveUpdate,
    AuctionNominationOrderUpdate,
    AuctionRobModeUpdate,
    AuctionStageUpdate,
    AvoidedTeamsUpdate,
    CommissionerImportsUpdate,
    DraftModeUpdate,
    DraftPickCreate,
    DraftPickUpdate,
    DraftResultsImportConfirmation,
    IdentityUpdate,
    ImportConfirmation,
    InteractiveAuctionBidCreate,
    InteractiveAuctionNominationCreate,
    InteractiveAuctionUpdate,
    KeeperCreate,
    LeagueConnect,
    LeagueFormatUpdate,
    MFLConnectionTest,
    MockDraftUpdate,
    PlayerComparisonRequest,
    PreferenceUpdate,
    PurchaseCreate,
    PurchaseUpdate,
    SetupUpdate,
    SourcePreview,
    SourceUpdate,
    UserLeagueSettingUpdate,
    WarningResolve,
)
from app.settings_store import (
    CredentialStoreError,
    runtime_settings,
    save_commissioner_imports,
    save_setup,
    setup_status,
)
from app.sources import (
    LOCAL_FILE_SOURCE_SPECS,
    LOCAL_PROJECTION_SOURCE_IDS,
    LOCAL_RANKING_SOURCE_IDS,
    initialize_sources,
    source_json,
    sync_gng,
    sync_local_projection_source,
    sync_local_ranking_source,
    sync_nflverse,
    sync_sleeper,
)
from app.sync import RULE_DESCRIPTIONS, record_sync_warnings, sync_configured, sync_league
from app.user_context import (
    active_username,
    is_personal_source_id,
    normalize_username,
    reset_active_league_ids,
    reset_active_username,
    set_active_league_ids,
    set_active_username,
    source_visible_to_user,
)
from app.users import (
    AUCTION_STRATEGIES,
    DRAFT_POLL_INTERVALS,
    NFL_TEAMS,
    auction_rob_mode,
    auction_stage_enabled,
    authorized_league_ids,
    avoided_teams,
    bootstrap_user,
    current_account,
    draft_mode,
    draft_poll_interval,
    effective_source_settings,
    is_current_admin,
    league_setting,
    mfl_memberships_for_user,
    record_login,
    reset_source_settings,
    save_auction_rob_mode,
    save_auction_stage,
    save_avoided_teams,
    save_draft_mode,
    save_draft_poll_interval,
    save_mfl_memberships,
    save_source_setting,
    strategy_json,
)
from scripts.import_auction_csv_as_draft import (
    ImportValidationError,
    MFLImportError,
    send_plan,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.globals["asset_version"] = "20260831.16"
SESSION_SIGNING_SECRET = ""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global SESSION_SIGNING_SECRET
    settings = get_settings()
    if settings.auth_required:
        SESSION_SIGNING_SECRET = resolve_session_secret(settings)
    init_db()
    with SessionLocal() as db:
        initialize_sources(db)
        for source_id in LOCAL_RANKING_SOURCE_IDS:
            try:
                sync_local_ranking_source(db, source_id)
            except (OSError, UnicodeError, ValueError):
                # The source card retains the exact error while the site remains usable.
                pass
        for source_id in LOCAL_PROJECTION_SOURCE_IDS:
            try:
                sync_local_projection_source(db, source_id)
            except (OSError, UnicodeError, ValueError):
                # Projection imports are optional and report their error on the source card.
                pass
        for username in settings.admin_username_set or {"wilsonmw"}:
            bootstrap_user(db, username, admin_usernames=settings.admin_username_set)
        for league_item in db.scalars(select(League)):
            if league_item.warnings_json and not db.scalar(
                select(SyncWarning.id).where(SyncWarning.league_id == league_item.id).limit(1)
            ):
                record_sync_warnings(db, league_item.id, league_item.warnings_json)
        db.commit()
        stored_settings = runtime_settings(db)
        stored_settings.export_directory.mkdir(parents=True, exist_ok=True)
        stored_settings.audit_directory.mkdir(parents=True, exist_ok=True)
    auto_sync_task: asyncio.Task[None] | None = None
    if settings.auto_sync_enabled:
        auto_sync_task = asyncio.create_task(daily_sync_loop(settings), name="daily-mfl-sync")
    live_draft_sync_task = asyncio.create_task(live_draft_sync_loop(), name="live-mfl-draft-sync")
    power_cache_task: asyncio.Task[None] | None = None
    if get_db not in app.dependency_overrides:
        power_cache_task = asyncio.create_task(
            asyncio.to_thread(refresh_all_power_snapshots_job, "startup"),
            name="power-rankings-cache-warmup",
        )
    try:
        yield
    finally:
        await stop_daily_sync(live_draft_sync_task)
        if auto_sync_task is not None:
            await stop_daily_sync(auto_sync_task)
        if power_cache_task is not None:
            await stop_daily_sync(power_cache_task)


app = FastAPI(title="MFL Fantasy Draft Manager", version="1.0.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_settings().allowed_host_list)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
Db = Annotated[Session, Depends(get_db)]


@app.middleware("http")
async def secure_session(request: Request, call_next: Any) -> Response:
    settings = get_settings()
    public_path = (
        request.url.path == "/login"
        or request.url.path == "/health"
        or request.url.path.startswith("/static/")
    )
    session = None
    if settings.auth_required:
        session = read_session_token(SESSION_SIGNING_SECRET, request.cookies.get(SESSION_COOKIE))
        if session is None and not public_path:
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": {"code": "authentication_required", "message": "Sign in required"}
                    },
                )
            target = request.url.path
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
        if (
            session is not None
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path != "/login"
            and not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session.csrf_token)
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "code": "csrf_failed",
                        "message": "Security token expired; refresh and try again",
                    }
                },
            )
    request.state.user = session
    context_token = set_active_username(session.username if session else "wilsonmw")
    league_context_token = set_active_league_ids(session.league_ids if session else None)
    try:
        response = cast(Response, await call_next(request))
    finally:
        reset_active_league_ids(league_context_token)
        reset_active_username(context_token)
    quick_player_embed = (
        request.url.path.startswith("/player/")
        and request.query_params.get("quick") == "1"
    )
    frame_ancestors = "'self'" if quick_player_embed else "'none'"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
        f"base-uri 'self'; form-action 'self'; frame-ancestors {frame_ancestors}"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN" if quick_player_embed else "DENY"
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
    if settings.app_env.lower() in {"production", "prod"}:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def _league_or_404(db: Session, league_id: str) -> League:
    league = db.scalar(select(League).where(League.id == league_id))
    allowed = authorized_league_ids(db)
    if not league or (allowed is not None and league_id not in allowed):
        raise HTTPException(404, detail={"code": "league_not_found", "message": "League not found"})
    return league


def _league_json(league: League) -> dict[str, Any]:
    return {
        "id": league.id,
        "season": league.season,
        "league_type": league.league_type,
        "name": league.name,
        "roster_size": league.roster_size,
        "starting_budget": str(league.starting_budget)
        if league.starting_budget is not None
        else None,
        "minimum_bid": str(league.minimum_bid),
        "settings": league.settings_json,
        "scoring_rules": league.scoring_rules_json,
        "lineup": league.lineup_json,
        "warnings": league.warnings_json,
        "synced_at": league.synced_at.isoformat() if league.synced_at else None,
    }


def _purchase_json(db: Session, purchase: AuctionPurchase) -> dict[str, Any]:
    player = db.get(Player, purchase.player_id)
    franchise = db.scalar(
        select(Franchise).where(
            Franchise.league_id == purchase.league_id,
            Franchise.id == purchase.franchise_id,
        )
    )
    return {
        "id": purchase.id,
        "league_id": purchase.league_id,
        "franchise_id": purchase.franchise_id,
        "player_id": purchase.player_id,
        "player_name": player.name if player else purchase.player_id,
        "player_team": player.nfl_team if player else None,
        "player_position": player.position if player else None,
        "franchise_name": franchise.name if franchise else purchase.franchise_id,
        "amount": str(purchase.amount),
        "status": purchase.status,
        "purchase_order": purchase.purchase_order,
        "version": purchase.version,
    }


def _keeper_json(keeper: KeeperSelection) -> dict[str, Any]:
    return {
        "id": keeper.id,
        "league_id": keeper.league_id,
        "franchise_id": keeper.franchise_id,
        "player_id": keeper.player_id,
        "keeper_cost": str(keeper.keeper_cost) if keeper.keeper_cost is not None else None,
        "source": keeper.source,
        "selected_at": keeper.selected_at.isoformat(),
    }


def _page_context(db: Session, title: str, league_id: str | None = None) -> dict[str, Any]:
    settings = runtime_settings(db)
    leagues = list(db.scalars(select(League).order_by(League.league_type, League.name)))
    allowed = authorized_league_ids(db)
    if allowed is not None:
        leagues = [item for item in leagues if item.id in allowed]
    selected = league_id or settings.mfl_keeper_league_id or settings.mfl_auction_league_id
    selected_league = next((item for item in leagues if item.id == selected), None)
    account = current_account(db)
    return {
        "title": title,
        "settings": settings,
        "leagues": leagues,
        "selected_league_id": selected,
        "selected_league": selected_league,
        "setup": setup_status(db),
        "account": account,
        "is_admin": account.is_admin,
        "assistant_enabled": bool(get_settings().openai_api_key),
        "chat_context": {"username": account.username, "league_id": selected},
    }


def _require_admin(db: Session) -> None:
    if not is_current_admin(db):
        raise HTTPException(
            403,
            detail={"code": "admin_required", "message": "An administrator must do that"},
        )


def _queue_round_power_refresh(
    background_tasks: BackgroundTasks,
    db: Session,
    league_id: str,
    mode: Literal["draft", "auction"],
) -> None:
    if mode == "draft":
        if not draft_state(db, league_id, include_intelligence=False)["live"]["is_live"]:
            return
    elif not _auction_live(db, league_id).is_live:
        return
    if round_refresh_due(db, league_id, mode):
        background_tasks.add_task(
            refresh_power_snapshot_job,
            league_id,
            f"{mode}-round-complete",
        )


def _admin_user_json(db: Session, account: UserAccount) -> dict[str, Any]:
    configured_admins = get_settings().admin_username_set
    presence = db.get(UserPresence, account.username)
    last_seen = presence.last_seen_at if presence else None
    comparable_seen = (
        last_seen.replace(tzinfo=UTC) if last_seen and last_seen.tzinfo is None else last_seen
    )
    is_online = bool(
        comparable_seen and comparable_seen >= datetime.now(UTC) - timedelta(seconds=90)
    )
    return {
        "username": account.username,
        "display_name": account.display_name,
        "is_admin": account.is_admin,
        "is_current_user": account.username == normalize_username(active_username()),
        "is_protected": account.username in configured_admins,
        "created_at": account.created_at.isoformat(),
        "last_login_at": account.last_login_at.isoformat() if account.last_login_at else None,
        "last_seen_at": last_seen.isoformat() if last_seen else None,
        "is_online": is_online,
    }


def _audit_mutation(
    db: Session,
    *,
    stream: str,
    action: str,
    league_id: str,
    entity_id: str | None = None,
    before: Any = None,
    after: Any = None,
    details: Any = None,
) -> dict[str, Any]:
    result = append_audit_event(
        runtime_settings(db).audit_directory,
        stream=stream,
        action=action,
        league_id=league_id,
        actor=active_username(),
        entity_id=entity_id,
        before=before,
        after=after,
        details=details,
    )
    league_events.publish(league_id, f"{stream}:{action}", {"entity_id": entity_id})
    return result


def _auction_live(db: Session, league_id: str) -> AuctionLiveState:
    state = db.get(AuctionLiveState, league_id)
    if state is None:
        state = AuctionLiveState(league_id=league_id)
        db.add(state)
        db.commit()
    return state


def _bump_auction(db: Session, league_id: str) -> AuctionLiveState:
    state = _auction_live(db, league_id)
    state.revision += 1
    state.updated_by = active_username()
    state.updated_at = datetime.now(UTC)
    db.commit()
    league_events.publish(league_id, "auction-state", {"revision": state.revision})
    return state


def _interactive_auction(db: Session, league_id: str) -> InteractiveAuctionState:
    state = db.get(InteractiveAuctionState, league_id)
    if state is None:
        state = InteractiveAuctionState(league_id=league_id)
        db.add(state)
        db.commit()
    return state


def _current_user_franchise_id(db: Session, league: League) -> str | None:
    membership = db.scalar(
        select(UserMFLMembership)
        .where(
            UserMFLMembership.username == normalize_username(active_username()),
            UserMFLMembership.league_id == league.id,
            UserMFLMembership.season == league.season,
        )
        .order_by(UserMFLMembership.discovered_at.desc())
    )
    if membership and membership.franchise_id:
        return membership.franchise_id
    return league_setting(db, league.id).franchise_id


def _player_headshot(player: Player | None) -> str | None:
    if player is None:
        return None
    metadata = player.metadata_json or {}
    nflverse = metadata.get("nflverse") if isinstance(metadata.get("nflverse"), dict) else {}
    headshot = nflverse.get("headshot") if isinstance(nflverse, dict) else None
    if isinstance(headshot, str) and headshot.startswith("https://"):
        return headshot
    sleeper = metadata.get("sleeper") if isinstance(metadata.get("sleeper"), dict) else {}
    sleeper_id = sleeper.get("player_id") if isinstance(sleeper, dict) else None
    if sleeper_id:
        return f"https://sleepercdn.com/content/nfl/players/{quote(str(sleeper_id))}.jpg"
    return None


def _money_string(value: Decimal | None) -> str | None:
    return f"{Decimal(value):.2f}" if value is not None else None


def _interactive_auction_json(db: Session, league_id: str) -> dict[str, Any]:
    league = _league_or_404(db, league_id)
    state = _interactive_auction(db, league_id)
    live = _auction_live(db, league_id)
    nomination = nomination_state(db, league_id)
    user_franchise_id = _current_user_franchise_id(db, league)
    franchises = {
        item.id: item.name
        for item in db.scalars(select(Franchise).where(Franchise.league_id == league_id))
    }
    player = db.get(Player, state.player_id) if state.player_id else None
    bids: list[InteractiveAuctionBid] = []
    if state.player_id and state.opened_at:
        bids = list(
            db.scalars(
                select(InteractiveAuctionBid)
                .where(
                    InteractiveAuctionBid.league_id == league_id,
                    InteractiveAuctionBid.player_id == state.player_id,
                    InteractiveAuctionBid.created_at >= state.opened_at,
                )
                .order_by(InteractiveAuctionBid.created_at.desc())
                .limit(20)
            )
        )
    active = state.status == "open" and player is not None
    can_nominate = bool(
        state.enabled
        and live.is_live
        and not active
        and user_franchise_id
        and user_franchise_id == nomination.get("current_franchise_id")
    )
    can_bid = bool(state.enabled and live.is_live and active and user_franchise_id)
    reason = None
    if state.enabled and not live.is_live:
        reason = "The admin must start the live auction"
    elif state.enabled and not user_franchise_id:
        reason = "Select your MFL franchise under My Account"
    elif (
        state.enabled and not active and user_franchise_id != nomination.get("current_franchise_id")
    ):
        reason = (
            f"Waiting for {nomination.get('current_franchise_name') or 'the next team'} to nominate"
        )
    minimum_next = Decimal(state.current_bid or 0) + Decimal(league.minimum_bid) if active else None
    return {
        "enabled": state.enabled,
        "status": state.status,
        "active": active,
        "revision": state.revision,
        "is_live": live.is_live,
        "player": {
            "player_id": player.id,
            "player_name": player.name,
            "position": player.position,
            "nfl_team": player.nfl_team,
            "headshot_url": _player_headshot(player),
        }
        if player
        else None,
        "nominating_franchise_id": state.nominating_franchise_id,
        "nominating_franchise_name": franchises.get(state.nominating_franchise_id or ""),
        "high_bid_franchise_id": state.high_bid_franchise_id,
        "high_bid_franchise_name": franchises.get(state.high_bid_franchise_id or ""),
        "current_bid": _money_string(state.current_bid),
        "minimum_next_bid": _money_string(minimum_next),
        "minimum_bid": _money_string(Decimal(league.minimum_bid)),
        "current_user_franchise_id": user_franchise_id,
        "current_user_franchise_name": franchises.get(user_franchise_id or ""),
        "current_nominator_id": nomination.get("current_franchise_id"),
        "current_nominator_name": nomination.get("current_franchise_name"),
        "can_nominate": can_nominate,
        "can_bid": can_bid,
        "permission_reason": reason,
        "bids": [
            {
                "id": bid.id,
                "franchise_id": bid.franchise_id,
                "franchise_name": franchises.get(bid.franchise_id, bid.franchise_id),
                "username": bid.username,
                "amount": _money_string(bid.amount),
                "created_at": bid.created_at.isoformat(),
            }
            for bid in bids
        ],
    }


def _normalized_roster_position(value: Any) -> str:
    position = str(value or "").upper()
    return {"DST": "DEF", "D/ST": "DEF", "K": "PK"}.get(position, position)


def _auction_room_intelligence(
    db: Session,
    league: League,
    budgets: list[dict[str, Any]],
    purchases: list[dict[str, Any]],
) -> dict[str, Any]:
    current_franchise_id = _current_user_franchise_id(db, league)
    budget_by_id = {str(item["franchise_id"]): item for item in budgets}
    owned_by_franchise: dict[str, set[str]] = {franchise_id: set() for franchise_id in budget_by_id}
    assignments_by_franchise: dict[str, dict[str, RosterAssignment]] = {}
    for assignment in db.scalars(
        select(RosterAssignment).where(RosterAssignment.league_id == league.id)
    ):
        franchise_id = str(assignment.franchise_id)
        player_id = str(assignment.player_id)
        owned_by_franchise.setdefault(franchise_id, set()).add(player_id)
        assignments_by_franchise.setdefault(franchise_id, {})[player_id] = assignment
    for purchase in purchases:
        owned_by_franchise.setdefault(str(purchase["franchise_id"]), set()).add(
            str(purchase["player_id"])
        )
    all_player_ids = set().union(*owned_by_franchise.values()) if owned_by_franchise else set()
    players_by_id = (
        {
            player.id: player
            for player in db.scalars(select(Player).where(Player.id.in_(all_player_ids)))
        }
        if all_player_ids
        else {}
    )
    lineup_requirements = {
        _normalized_roster_position(position): int(required or 0)
        for position, required in league.lineup_json.items()
        if _normalized_roster_position(position) not in {"FLEX", "SUPERFLEX"}
    }
    baseline_cost = Decimal(league.starting_budget or 0) / Decimal(max(league.roster_size, 1))
    owner_insights: list[dict[str, Any]] = []
    team_details: dict[str, dict[str, Any]] = {}
    for franchise_id, budget in budget_by_id.items():
        player_ids = owned_by_franchise.get(franchise_id, set())
        position_counts: Counter[str] = Counter()
        bye_groups: dict[int, list[str]] = {}
        for player_id in player_ids:
            player = players_by_id.get(player_id)
            if player is None:
                continue
            position_counts[_normalized_roster_position(player.position)] += 1
            if player.bye_week:
                bye_groups.setdefault(int(player.bye_week), []).append(player.name)
        needs = {
            position: max(0, required - position_counts.get(position, 0))
            for position, required in lineup_requirements.items()
        }
        team_purchases = [
            purchase for purchase in purchases if purchase["franchise_id"] == franchise_id
        ]
        purchase_by_player = {str(purchase["player_id"]): purchase for purchase in team_purchases}
        roster = []
        for player_id in player_ids:
            player = players_by_id.get(player_id)
            roster_purchase = purchase_by_player.get(player_id)
            roster_assignment = assignments_by_franchise.get(franchise_id, {}).get(player_id)
            paid = (
                roster_purchase.get("amount")
                if roster_purchase
                else roster_assignment.salary
                if roster_assignment
                else None
            )
            roster.append(
                {
                    "player_id": player_id,
                    "player_name": player.name if player else player_id,
                    "position": player.position if player else None,
                    "nfl_team": player.nfl_team if player else None,
                    "bye_week": player.bye_week if player else None,
                    "amount": (_money_string(Decimal(str(paid))) if paid is not None else None),
                    "status": (
                        roster_purchase.get("status")
                        if roster_purchase
                        else roster_assignment.status
                        if roster_assignment
                        else "ROSTER"
                    ),
                    "source": "local" if roster_purchase else "mfl",
                }
            )
        position_order = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "PK": 4, "DEF": 5}
        roster.sort(
            key=lambda item: (
                position_order.get(
                    _normalized_roster_position(cast(str | None, item["position"])), 99
                ),
                str(item["player_name"]),
            )
        )
        purchase_total = sum(
            (Decimal(str(purchase["amount"])) for purchase in team_purchases), Decimal("0")
        )
        average_purchase = (
            purchase_total / Decimal(len(team_purchases)) if team_purchases else Decimal("0")
        )
        bye_warnings = [
            {"week": week, "count": len(names), "players": sorted(names)}
            for week, names in bye_groups.items()
            if len(names) >= 2
        ]
        bye_warnings.sort(key=lambda item: (-cast(int, item["count"]), cast(int, item["week"])))
        detail = {
            "franchise_id": franchise_id,
            "franchise_name": budget["name"],
            "roster_count": int(budget["slots_used"]),
            "roster_size": int(budget["roster_slots"]),
            "open_roster_slots": int(budget["slots_remaining"]),
            "spent": _money_string(Decimal(str(budget["spent"]))),
            "remaining": _money_string(Decimal(str(budget["remaining"]))),
            "maximum_bid": _money_string(Decimal(str(budget["maximum_bid"]))),
            "position_counts": dict(position_counts),
            "needs": needs,
            "open_starter_slots": sum(needs.values()),
            "purchase_count": len(team_purchases),
            "average_purchase": _money_string(average_purchase),
            "spending_style": (
                "Aggressive"
                if team_purchases and average_purchase > baseline_cost * Decimal("1.2")
                else "Patient"
                if team_purchases and average_purchase < baseline_cost * Decimal("0.8")
                else "Balanced"
            ),
            "last_purchase": team_purchases[0] if team_purchases else None,
            "bye_warnings": bye_warnings,
            "roster": roster,
        }
        team_details[franchise_id] = detail
        if franchise_id != current_franchise_id:
            owner_insights.append(detail)
    owner_insights.sort(
        key=lambda item: (
            -Decimal(str(item["maximum_bid"] or 0)),
            str(item["franchise_name"]),
        )
    )
    selected = team_details.get(current_franchise_id or "")
    total_remaining = sum((Decimal(str(item["remaining"])) for item in budgets), Decimal("0"))
    open_slots = sum(int(item["slots_remaining"]) for item in budgets)
    free_money = max(
        Decimal("0"), total_remaining - Decimal(league.minimum_bid) * Decimal(open_slots)
    )
    recent_positions = [
        _normalized_roster_position(purchase.get("player_position"))
        for purchase in purchases[:6]
        if purchase.get("player_position")
    ]
    recent_position_counts = Counter(recent_positions)
    return {
        "war_room": {
            "configured": selected is not None,
            **(selected or {}),
            "lineup_requirements": lineup_requirements,
        },
        "intelligence": {
            "recent_position_counts": dict(recent_position_counts),
            "position_runs": [
                {
                    "position": position,
                    "count": count,
                    "window": len(recent_positions),
                    "label": f"{count} {position}s in the last {len(recent_positions)} purchases",
                }
                for position, count in recent_position_counts.most_common()
                if count >= 3
            ],
            "latest_purchase": purchases[0] if purchases else None,
            "opponent_insights": owner_insights,
            "market": {
                "total_remaining": _money_string(total_remaining),
                "open_slots": open_slots,
                "free_money": _money_string(free_money),
                "dollars_per_open_slot": _money_string(
                    total_remaining / Decimal(open_slots) if open_slots else Decimal("0")
                ),
                "top_maximum_bid": _money_string(
                    max(
                        (Decimal(str(item["maximum_bid"])) for item in budgets),
                        default=Decimal("0"),
                    )
                ),
            },
        },
    }


@app.exception_handler(AuctionValidationError)
@app.exception_handler(DraftValidationError)
async def domain_error(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "detail": {"code": "conflict", "message": str(exc)},
            "corrective_action": "Refresh the room and correct the highlighted action.",
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None) -> Any:
    if request.state.user is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"title": "Sign in", "next": _safe_next(next), "error": None},
    )


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    db: Db,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Any:
    client_ip = request.client.host if request.client else "unknown"
    rate_key = login_rate_key(client_ip, username)
    error = "MFL sign-in failed or this account does not belong to the configured leagues."
    if not login_allowed(rate_key):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "title": "Sign in",
                "next": _safe_next(next),
                "error": "Too many attempts. Wait ten minutes and try again.",
            },
            status_code=429,
        )

    settings = runtime_settings(db)
    configured = {
        value for value in (settings.mfl_keeper_league_id, settings.mfl_auction_league_id) if value
    }
    try:
        if not configured:
            raise MFLAuthenticationError("No leagues are configured")
        async with MFLClient(settings) as client:
            await client.authenticate(username.strip(), password)
            leagues = await client.export("myleagues", force=True)
        membership_rows = mfl_memberships(leagues.payload)
        membership_ids = {str(item["league_id"]) for item in membership_rows}
        if not configured.issubset(membership_ids):
            raise MFLAuthenticationError("Account is not in every configured league")
    except (MFLAuthenticationError, MFLError):
        record_login_failure(rate_key)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"title": "Sign in", "next": _safe_next(next), "error": error},
            status_code=401,
        )

    clear_login_failures(rate_key)
    app_settings = get_settings()
    record_login(db, username.strip(), app_settings.admin_username_set)
    save_mfl_memberships(db, username.strip(), settings.mfl_season, membership_rows)
    max_age = app_settings.session_max_age_days * 24 * 60 * 60
    token, _ = make_session_token(
        SESSION_SIGNING_SECRET,
        username.strip(),
        membership_ids,
        max_age_seconds=max_age,
    )
    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=app_settings.app_env.lower() in {"production", "prod"},
        samesite="lax",
        path="/",
    )
    return response


@app.post("/logout", status_code=204)
def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Db) -> Any:
    context = _page_context(db, "League dashboard")
    context["sources"] = list(db.scalars(select(DataSource).order_by(DataSource.name)))
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/players", response_class=HTMLResponse)
def players_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    return templates.TemplateResponse(
        request, "players.html", _page_context(db, "All players", league_id)
    )


@app.get("/player/{player_id}", response_class=HTMLResponse)
def player_profile_page(
    request: Request, player_id: str, db: Db, league_id: str | None = None
) -> Any:
    context = _page_context(db, "Player profile", league_id)
    if db.get(Player, player_id) is None:
        raise HTTPException(404, detail={"code": "player_not_found", "message": "Player not found"})
    context["player_id"] = player_id
    return templates.TemplateResponse(request, "player_profile.html", context)


@app.get("/rosters", response_class=HTMLResponse)
def rosters_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    return templates.TemplateResponse(
        request, "rosters.html", _page_context(db, "League rosters", league_id)
    )


@app.get("/franchises/{franchise_id}", response_class=HTMLResponse)
def franchise_page(
    request: Request, franchise_id: str, db: Db, league_id: str | None = None
) -> Any:
    context = _page_context(db, "Franchise detail", league_id)
    context["franchise_id"] = franchise_id
    return templates.TemplateResponse(request, "franchise.html", context)


@app.get("/cheat-sheet", response_class=HTMLResponse)
def cheat_sheet_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    return templates.TemplateResponse(
        request, "cheat_sheet.html", _page_context(db, "Consensus cheat sheet", league_id)
    )


@app.get("/draft", response_class=HTMLResponse)
def draft_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_keeper_league_id
    return templates.TemplateResponse(
        request, "draft.html", _page_context(db, "Live draft room", selected)
    )


@app.get("/draft-board", response_class=HTMLResponse)
def draft_board_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_keeper_league_id
    return templates.TemplateResponse(
        request,
        "draft_board.html",
        _page_context(db, "Live draft board", selected),
    )


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, db: Db) -> Any:
    return templates.TemplateResponse(request, "sources.html", _page_context(db, "Data sources"))


@app.get("/links", response_class=HTMLResponse)
def links_page(request: Request, db: Db) -> Any:
    context = _page_context(db, "Draft links")
    context["link_groups"] = draft_link_groups()
    return templates.TemplateResponse(request, "links.html", context)


@app.get("/bye-advisor", response_class=HTMLResponse)
def bye_advisor_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    return templates.TemplateResponse(
        request,
        "bye_advisor.html",
        _page_context(db, "Bye Week Advisor", league_id),
    )


@app.get("/power-rankings", response_class=HTMLResponse)
def power_rankings_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    return templates.TemplateResponse(
        request,
        "power_rankings.html",
        _page_context(db, "League power rankings", league_id),
    )


@app.get("/depth-charts", response_class=HTMLResponse)
def depth_charts_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    return templates.TemplateResponse(
        request,
        "depth_charts.html",
        _page_context(db, "NFL depth charts", league_id),
    )


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    return templates.TemplateResponse(
        request, "account.html", _page_context(db, "My account", league_id)
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Db) -> Any:
    return templates.TemplateResponse(request, "settings.html", _page_context(db, "MFL connection"))


@app.get("/scoring", response_class=HTMLResponse)
def scoring_page(request: Request, db: Db, league_id: str | None = None) -> Any:
    return templates.TemplateResponse(
        request, "scoring.html", _page_context(db, "Imported scoring rules", league_id)
    )


def _auction_page_context(db: Session, league_id: str | None = None) -> dict[str, Any]:
    settings = runtime_settings(db)
    auction_leagues = list(
        db.scalars(
            select(League).where(League.league_type == LeagueType.AUCTION).order_by(League.name)
        )
    )
    allowed = authorized_league_ids(db)
    if allowed is not None:
        auction_leagues = [item for item in auction_leagues if item.id in allowed]
    selected_league: League | None
    if league_id:
        selected_league = _league_or_404(db, league_id)
        if selected_league.league_type != LeagueType.AUCTION:
            raise HTTPException(
                409,
                detail={
                    "code": "auction_league_required",
                    "message": "An administrator must mark this league as an auction first",
                },
            )
    else:
        selected_league = next(
            (item for item in auction_leagues if item.id == settings.mfl_auction_league_id),
            auction_leagues[0] if auction_leagues else None,
        )
    selected = selected_league.id if selected_league else None
    context = _page_context(db, "Auction room", selected)
    context["league"] = selected_league
    context["auction_leagues"] = auction_leagues
    context["rob_mode"] = auction_rob_mode(db)
    live = _auction_live(db, selected) if selected else None
    staged = auction_stage_enabled(db, selected) if selected else False
    interactive = _interactive_auction(db, selected).enabled if selected else False
    context["interactive_bidding"] = interactive
    current_user_franchise_id = (
        _current_user_franchise_id(db, selected_league) if selected_league else None
    )
    context["current_user_franchise_id"] = current_user_franchise_id
    context["can_record_purchase"] = (
        bool(live and live.is_live) and (context["is_admin"] or not context["rob_mode"])
    ) or (staged and context["is_admin"])
    if interactive:
        context["can_record_purchase"] = False
    context["can_record_own_purchase"] = bool(
        context["can_record_purchase"] and current_user_franchise_id
    )
    return context


@app.get("/auction", response_class=HTMLResponse)
def auction_room(request: Request, db: Db, league_id: str | None = None) -> Any:
    context = _auction_page_context(db, league_id)
    context["auctioneer_view"] = False
    return templates.TemplateResponse(request, "auction.html", context)


@app.get("/auction/auctioneer", response_class=HTMLResponse)
def auctioneer_room(request: Request, db: Db, league_id: str | None = None) -> Any:
    _require_admin(db)
    context = _auction_page_context(db, league_id)
    context["auctioneer_view"] = True
    context["title"] = "Auctioneer view"
    return templates.TemplateResponse(request, "auctioneer.html", context)


@app.get("/auction/history", response_class=HTMLResponse)
def auction_history(request: Request, db: Db, league_id: str | None = None) -> Any:
    context = _auction_page_context(db, league_id)
    league = context.get("league")
    context["title"] = "Auction history"
    context["purchases"] = (
        [
            _purchase_json(db, purchase)
            for purchase in db.scalars(
                select(AuctionPurchase)
                .where(AuctionPurchase.league_id == league.id)
                .order_by(AuctionPurchase.purchase_order.desc())
            ).all()
        ]
        if league
        else []
    )
    return templates.TemplateResponse(request, "auction_history.html", context)


@app.get("/keepers", response_class=HTMLResponse)
def keeper_room(request: Request, db: Db) -> Any:
    settings = runtime_settings(db)
    context = _page_context(db, "Keeper room", settings.mfl_keeper_league_id)
    context["league"] = db.scalar(select(League).where(League.id == settings.mfl_keeper_league_id))
    return templates.TemplateResponse(request, "keepers.html", context)


@app.get("/api/setup/status")
def api_setup_status(db: Db) -> dict[str, object]:
    return setup_status(db)


@app.get("/api/admin/auction-mode")
def admin_auction_mode(db: Db) -> dict[str, bool]:
    _require_admin(db)
    return {"rob_mode": auction_rob_mode(db)}


@app.put("/api/admin/auction-mode")
def update_admin_auction_mode(payload: AuctionRobModeUpdate, db: Db) -> dict[str, bool]:
    _require_admin(db)
    return {"rob_mode": save_auction_rob_mode(db, payload.enabled)}


def _commissioner_import_status(db: Session) -> dict[str, bool]:
    settings = runtime_settings(db)
    credentials_configured = bool(settings.mfl_username and settings.mfl_password)
    return {
        "enabled": settings.mfl_enable_imports,
        "credentials_configured": credentials_configured,
        "reauthentication_required": True,
        "ready": settings.mfl_enable_imports,
    }


@app.get("/api/admin/commissioner-imports")
def admin_commissioner_imports(db: Db) -> dict[str, bool]:
    _require_admin(db)
    return _commissioner_import_status(db)


@app.put("/api/admin/commissioner-imports")
def update_admin_commissioner_imports(
    payload: CommissionerImportsUpdate, db: Db
) -> dict[str, bool]:
    _require_admin(db)
    save_commissioner_imports(db, payload.enabled)
    return _commissioner_import_status(db)


@app.get("/api/admin/users")
def admin_users(db: Db) -> list[dict[str, Any]]:
    _require_admin(db)
    accounts = db.scalars(
        select(UserAccount).order_by(UserAccount.display_name, UserAccount.username)
    )
    return [_admin_user_json(db, account) for account in accounts]


@app.get("/api/admin/draft-connection")
def admin_draft_connection(league_id: str, db: Db) -> dict[str, Any]:
    _require_admin(db)
    league = _league_or_404(db, league_id)
    session = db.scalar(
        select(DraftSession)
        .where(
            DraftSession.league_id == league_id,
            DraftSession.season == league.season,
        )
        .limit(1)
    )
    snapshot = db.scalar(
        select(MFLSnapshot)
        .where(
            MFLSnapshot.league_id == league_id,
            MFLSnapshot.export_type == "draftResults",
        )
        .order_by(MFLSnapshot.fetched_at.desc(), MFLSnapshot.id.desc())
        .limit(1)
    )
    preview: dict[str, Any] = (
        reconcile_preview(db, league_id, snapshot.payload_json)
        if snapshot and isinstance(snapshot.payload_json, dict)
        else {"additions": [], "conflicts": [], "remote_count": 0}
    )
    method = draft_mode(db, league_id)
    is_live = bool(session and session.status == "live")
    companion_running = bool(is_live and method == "companion")
    now = datetime.now(UTC)
    fetched_at = snapshot.fetched_at if snapshot else None
    comparable_fetched = (
        fetched_at.replace(tzinfo=UTC) if fetched_at and fetched_at.tzinfo is None else fetched_at
    )
    age_seconds = (
        max(0, int((now - comparable_fetched).total_seconds())) if comparable_fetched else None
    )
    interval_seconds = draft_poll_interval(db, league_id)
    stale_after_seconds = max(75, interval_seconds * 2 + 15)
    conflicts = list(preview.get("conflicts") or [])
    additions = list(preview.get("additions") or [])
    remote_count = int(preview.get("remote_count") or 0)
    if method == "local":
        connection_state = "local"
        connection_label = "Local mode"
    elif not is_live:
        connection_state = "paused"
        connection_label = "Companion paused"
    elif conflicts:
        connection_state = "conflict"
        connection_label = "Sync conflict"
    elif age_seconds is None or age_seconds > stale_after_seconds:
        connection_state = "stale"
        connection_label = "MFL check overdue"
    else:
        connection_state = "connected"
        connection_label = "MFL connected"
    warning = None
    if conflicts:
        warning = (
            f"Automatic importing is paused by {len(conflicts)} MFL conflict(s). "
            "Open the Draft Room and correct or reconcile the conflicting pick."
        )
    elif companion_running and (age_seconds is None or age_seconds > stale_after_seconds):
        warning = (
            "No recent MFL draftResults response has arrived. Keep the MFL draft room open "
            "and be ready to pause Companion mode if this does not recover."
        )
    next_check_at = (
        comparable_fetched + timedelta(seconds=interval_seconds)
        if companion_running and comparable_fetched
        else None
    )
    imported_count = int(
        db.scalar(
            select(func.count(DraftPick.id)).where(
                DraftPick.league_id == league_id,
                DraftPick.source == "mfl",
            )
        )
        or 0
    )
    recorded_count = int(
        db.scalar(select(func.count(DraftPick.id)).where(DraftPick.league_id == league_id)) or 0
    )
    return {
        "league_id": league.id,
        "league_name": league.name,
        "draft_mode": method,
        "is_live": is_live,
        "companion_sync_running": companion_running,
        "connection_state": connection_state,
        "connection_label": connection_label,
        "last_successful_check_at": comparable_fetched.isoformat() if comparable_fetched else None,
        "next_check_at": next_check_at.isoformat() if next_check_at else None,
        "server_time": now.isoformat(),
        "poll_interval_seconds": interval_seconds,
        "poll_interval_options": list(DRAFT_POLL_INTERVALS),
        "stale_after_seconds": stale_after_seconds,
        "age_seconds": age_seconds,
        "mfl_pick_count": remote_count,
        "imported_pick_count": imported_count,
        "recorded_pick_count": recorded_count,
        "pending_pick_count": len(additions),
        "conflict_count": len(conflicts),
        "warning": warning,
    }


@app.put("/api/admin/draft-polling")
def admin_draft_polling(league_id: str, seconds: int, db: Db) -> dict[str, Any]:
    _require_admin(db)
    _league_or_404(db, league_id)
    try:
        saved = save_draft_poll_interval(db, league_id, seconds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"league_id": league_id, "poll_interval_seconds": saved, "options": list(DRAFT_POLL_INTERVALS)}


@app.post("/api/presence", status_code=204)
def record_user_presence(db: Db) -> None:
    username = normalize_username(active_username())
    presence = db.get(UserPresence, username)
    if presence is None:
        presence = UserPresence(username=username)
        db.add(presence)
    presence.last_seen_at = datetime.now(UTC)
    db.commit()


@app.put("/api/admin/users/{username}/role")
def update_admin_user_role(username: str, payload: AdminRoleUpdate, db: Db) -> dict[str, Any]:
    _require_admin(db)
    normalized = normalize_username(username)
    account = db.get(UserAccount, normalized)
    if account is None:
        raise HTTPException(
            404, detail={"code": "user_not_found", "message": "That user has not signed in yet"}
        )
    if not payload.is_admin:
        if normalized == normalize_username(active_username()):
            raise HTTPException(
                409,
                detail={
                    "code": "self_demotion_blocked",
                    "message": "Another administrator must remove your admin access",
                },
            )
        if normalized in get_settings().admin_username_set:
            raise HTTPException(
                409,
                detail={
                    "code": "configured_admin",
                    "message": "This administrator is protected by ADMIN_USERNAMES",
                },
            )
        admin_count = db.scalar(
            select(func.count()).select_from(UserAccount).where(UserAccount.is_admin.is_(True))
        )
        if account.is_admin and int(admin_count or 0) <= 1:
            raise HTTPException(
                409,
                detail={"code": "last_admin", "message": "At least one administrator is required"},
            )
    account.is_admin = payload.is_admin
    db.commit()
    return _admin_user_json(db, account)


@app.put("/api/setup/leagues")
def update_setup(payload: SetupUpdate, db: Db) -> dict[str, object]:
    try:
        save_setup(db, payload)
    except CredentialStoreError as exc:
        raise HTTPException(
            503, detail={"code": "credential_store_unavailable", "message": str(exc)}
        ) from exc
    return setup_status(db)


@app.post("/api/setup/test-mfl")
async def test_mfl(payload: MFLConnectionTest, db: Db) -> dict[str, Any]:
    base = runtime_settings(db)
    public_settings = base.model_copy(
        update={
            "mfl_season": payload.season,
            "mfl_keeper_league_id": payload.league_id,
            "mfl_keeper_api_key": "",
        }
    )
    async with MFLClient(public_settings) as client:
        try:
            public = await client.export("league", league_id=payload.league_id, force=True)
            league_data = public.payload.get("league", public.payload)
            if not isinstance(league_data, dict):
                league_data = {}
            public_ok = True
            public_error = None
        except MFLError as exc:
            public_ok = False
            public_error = str(exc)
            league_data = {}
            public = None
    protected_ok = False
    protected_error: str | None = "No league API key supplied"
    if payload.api_key:
        protected_settings = public_settings.model_copy(
            update={"mfl_keeper_api_key": payload.api_key}
        )
        async with MFLClient(protected_settings) as client:
            try:
                await client.export("rosters", league_id=payload.league_id, force=True)
                protected_ok = True
                protected_error = None
            except MFLError as exc:
                protected_error = str(exc)
    return {
        "league_id": payload.league_id,
        "season": payload.season,
        "name": league_data.get("name"),
        "host": urlparse(public.source_url).hostname if public else None,
        "public_access": {"ok": public_ok, "error": public_error},
        "protected_access": {"ok": protected_ok, "error": protected_error},
        "api_key_returned": False,
    }


@app.post("/api/sync")
async def sync(db: Db) -> dict[str, Any]:
    settings = runtime_settings(db)
    async with MFLClient(settings) as client:
        try:
            return {"leagues": await sync_configured(db, client, settings)}
        except (MFLError, ValueError) as exc:
            raise HTTPException(
                502, detail={"code": "mfl_sync_failed", "message": str(exc)}
            ) from exc


@app.get("/api/sources")
def sources(db: Db) -> list[dict[str, Any]]:
    initialize_sources(db)
    effective = effective_source_settings(db)
    username = active_username()
    visible_sources = [
        item
        for item in db.scalars(select(DataSource).order_by(DataSource.name))
        if source_visible_to_user(item.id, username)
    ]
    visible_source_ids = [item.id for item in visible_sources]
    allowed = authorized_league_ids(db)
    count_query = select(
        SourcePlayerValue.source_id,
        SourcePlayerValue.league_id,
        func.count(SourcePlayerValue.id),
    ).group_by(SourcePlayerValue.source_id, SourcePlayerValue.league_id)
    count_query = count_query.where(SourcePlayerValue.source_id.in_(visible_source_ids))
    if allowed is not None:
        count_query = count_query.where(
            or_(
                SourcePlayerValue.league_id.in_(allowed),
                SourcePlayerValue.league_id.is_(None),
            )
        )
    counts: dict[str, dict[str, int]] = {}
    for source_id, league_id, count in db.execute(count_query):
        counts.setdefault(str(source_id), {})[str(league_id or "global")] = int(count)
    saved_ids = set(
        db.scalars(
            select(UserSourceSetting.source_id).where(
                UserSourceSetting.username == active_username()
            )
        )
    )
    return [
        {
            **source_json(item),
            "enabled": effective[item.id]["enabled"],
            "weight": str(effective[item.id]["weight"]),
            "default_weight": str(item.weight),
            "personalized": item.id in saved_ids,
            "visibility": "personal" if is_personal_source_id(item.id) else "shared",
            "received_count": sum(counts.get(item.id, {}).values()),
            "league_counts": counts.get(item.id, {}),
        }
        for item in visible_sources
    ]


SOURCE_RAW_PRIVATE_KEYS = {
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
SOURCE_RAW_COLUMN_PRIORITY = (
    "overall_rank",
    "position_rank",
    "tier",
    "season_projection",
    "projected_average",
    "projection_floor",
    "projection_ceiling",
    "expert_analysis",
    "projection",
    "projected_points",
    "adp",
    "aav",
    "auction_value",
    "bye_week",
    "week",
    "opponent",
    "depth_order",
    "source_file",
    "source_label",
)


def _safe_source_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _safe_source_raw(value)
    if isinstance(value, list):
        return [_safe_source_value(item) for item in value]
    return value


def _safe_source_raw(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _safe_source_value(value)
        for key, value in raw.items()
        if "".join(character for character in str(key).casefold() if character.isalnum())
        not in SOURCE_RAW_PRIVATE_KEYS
    }


def _source_spreadsheet_payload(
    db: Session, source_id: str, *, limit: int | None
) -> dict[str, Any]:
    source = db.get(DataSource, source_id)
    if source is None or not source_visible_to_user(source_id, active_username()):
        raise HTTPException(404, detail={"code": "source_not_found", "message": "Source not found"})
    filters = [SourcePlayerValue.source_id == source_id]
    allowed = authorized_league_ids(db)
    if allowed is not None:
        filters.append(
            or_(
                SourcePlayerValue.league_id.in_(allowed),
                SourcePlayerValue.league_id.is_(None),
            )
        )
    total = int(db.scalar(select(func.count(SourcePlayerValue.id)).where(*filters)) or 0)
    query = (
        select(SourcePlayerValue, Player)
        .join(Player, Player.id == SourcePlayerValue.player_id)
        .where(*filters)
        .order_by(
            SourcePlayerValue.value_type,
            SourcePlayerValue.league_id,
            SourcePlayerValue.normalized_value,
            Player.name,
        )
    )
    if limit is not None:
        query = query.limit(limit)
    records = list(db.execute(query))
    league_names = {
        item.id: item.name
        for item in db.scalars(select(League).order_by(League.name, League.id))
        if allowed is None or item.id in allowed
    }
    raw_columns: set[str] = set()
    rows = []
    for value, player in records:
        raw = _safe_source_raw(value.raw_value_json or {})
        raw_columns.update(raw)
        rows.append(
            {
                "league_id": value.league_id,
                "league_name": league_names.get(str(value.league_id), "All leagues"),
                "player_id": player.id,
                "player_name": player.name,
                "nfl_team": player.nfl_team,
                "position": player.position,
                "value_type": value.value_type,
                "normalized_value": str(value.normalized_value)
                if value.normalized_value is not None
                else None,
                "raw": raw,
                "snapshot_id": value.snapshot_id,
                "source_updated_at": value.source_updated_at,
                "fetched_at": value.fetched_at,
            }
        )
    ordered_raw_columns = [key for key in SOURCE_RAW_COLUMN_PRIORITY if key in raw_columns]
    ordered_raw_columns.extend(sorted(raw_columns.difference(ordered_raw_columns)))
    return {
        "source_id": source.id,
        "source_name": source.name,
        "total_count": total,
        "returned_count": len(rows),
        "raw_columns": ordered_raw_columns,
        "rows": rows,
    }


@app.get("/api/sources/{source_id}/data")
def source_received_data(
    source_id: str,
    db: Db,
    limit: int = Query(default=100, ge=1, le=250),
) -> dict[str, Any]:
    return _source_spreadsheet_payload(db, source_id, limit=limit)


@app.get("/api/sources/{source_id}/download.csv")
def download_source_csv(source_id: str, db: Db) -> Response:
    spec = LOCAL_FILE_SOURCE_SPECS.get(source_id)
    source = db.get(DataSource, source_id)
    if source is None or not source_visible_to_user(source_id, active_username()):
        raise HTTPException(404, detail={"code": "source_not_found", "message": "Source not found"})
    if spec is not None:
        path = Path(spec["path"])
        if path.is_file():
            return FileResponse(path, media_type="text/csv", filename=path.name)

    spreadsheet = _source_spreadsheet_payload(db, source_id, limit=None)
    raw_columns = spreadsheet["raw_columns"]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "league",
            "player_name",
            "player_id",
            "nfl_team",
            "position",
            "data_type",
            "normalized_value",
            "snapshot_id",
            "source_updated_at",
            "loaded_at",
            *raw_columns,
        ]
    )
    for row in spreadsheet["rows"]:
        raw = row["raw"]
        writer.writerow(
            [
                row["league_name"],
                row["player_name"],
                row["player_id"],
                row["nfl_team"],
                row["position"],
                row["value_type"],
                row["normalized_value"],
                row["snapshot_id"],
                row["source_updated_at"],
                row["fetched_at"],
                *[
                    json.dumps(raw.get(column), ensure_ascii=False)
                    if isinstance(raw.get(column), (dict, list))
                    else raw.get(column)
                    for column in raw_columns
                ],
            ]
        )
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{source.id}_spreadsheet.csv"'},
    )


@app.put("/api/setup/sources/{source_id}")
def update_source(source_id: str, payload: SourceUpdate, db: Db) -> dict[str, Any]:
    source = db.get(DataSource, source_id)
    if source is None or not source_visible_to_user(source_id, active_username()):
        raise HTTPException(404, detail={"code": "source_not_found", "message": "Source not found"})
    setting = save_source_setting(db, source_id, enabled=payload.enabled, weight=payload.weight)
    return {**source_json(source), "enabled": setting.enabled, "weight": str(setting.weight)}


@app.post("/api/sources/reset")
def reset_sources(db: Db) -> dict[str, int]:
    return {"reset_count": reset_source_settings(db)}


def _avoided_teams_json(db: Session) -> dict[str, Any]:
    selected = avoided_teams(db)
    return {
        "teams": sorted(selected),
        "options": [
            {"code": code, "name": name, "avoided": code in selected}
            for code, name in NFL_TEAMS.items()
        ],
    }


@app.get("/api/sources/avoided-teams")
def get_avoided_teams(db: Db) -> dict[str, Any]:
    return _avoided_teams_json(db)


@app.put("/api/sources/avoided-teams")
def update_avoided_teams(payload: AvoidedTeamsUpdate, db: Db) -> dict[str, Any]:
    try:
        save_avoided_teams(db, payload.teams)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "invalid_nfl_team", "message": str(exc)}) from exc
    return _avoided_teams_json(db)


@app.post("/api/sources/preview")
def preview_sources(payload: SourcePreview, db: Db) -> dict[str, Any]:
    _league_or_404(db, payload.league_id)
    baseline = draftable_consensus(db, payload.league_id)
    preview = draftable_consensus(
        db,
        payload.league_id,
        {key: value.model_dump() for key, value in payload.sources.items()},
    )
    old = {row["player_id"]: row["consensus_rank"] for row in baseline}
    movers = [
        {
            "player_name": row["player_name"],
            "position": row["position"],
            "old_rank": old.get(row["player_id"]),
            "new_rank": row["consensus_rank"],
            "change": old.get(row["player_id"], row["consensus_rank"]) - row["consensus_rank"],
        }
        for row in preview
    ]
    movers.sort(key=lambda item: abs(item["change"]), reverse=True)
    return {"top": preview[:100], "movers": movers[:12]}


@app.post("/api/sources/sync")
async def sync_source(db: Db, source_id: str = Query(...)) -> dict[str, Any]:
    source = db.get(DataSource, source_id)
    if source is None or not source_visible_to_user(source_id, active_username()):
        raise HTTPException(404, detail={"code": "source_not_found", "message": "Source not found"})
    if not source.enabled:
        raise HTTPException(
            409, detail={"code": "source_disabled", "message": "Enable this source first"}
        )
    try:
        if source_id == "sleeper":
            return {"source_id": source_id, **await sync_sleeper(db)}
        if source_id == "nflverse":
            return {"source_id": source_id, **await sync_nflverse(db)}
        if source_id in LOCAL_RANKING_SOURCE_IDS:
            return {"source_id": source_id, **sync_local_ranking_source(db, source_id)}
        if source_id in LOCAL_PROJECTION_SOURCE_IDS:
            return {"source_id": source_id, **sync_local_projection_source(db, source_id)}
        if source_id == "gng":
            leagues_to_sync = list(
                db.scalars(select(League).order_by(League.league_type, League.id))
            )
            results: list[dict[str, Any]] = []
            for league_item in leagues_to_sync:
                synced = await sync_gng(db, league_item.id, league_item.scoring_rules_json or {})
                results.append({"league_id": league_item.id, **synced})
            return {
                "source_id": source_id,
                "leagues": results,
                "matched": sum(int(item["matched"]) for item in results),
                "unresolved": sum(int(item["unresolved"]) for item in results),
            }
        if source_id.startswith("mfl_"):
            result = await sync(db)
            return {"source_id": source_id, **result}
    except Exception as exc:
        raise HTTPException(
            502, detail={"code": "source_sync_failed", "message": str(exc)}
        ) from exc
    raise HTTPException(
        400,
        detail={"code": "manual_source", "message": "This source is updated through CSV import"},
    )


@app.get("/api/leagues")
def leagues(db: Db) -> list[dict[str, Any]]:
    items = list(db.scalars(select(League).order_by(League.name)))
    allowed = authorized_league_ids(db)
    if allowed is not None:
        items = [item for item in items if item.id in allowed]
    return [_league_json(item) for item in items]


@app.get("/api/leagues/{league_id}")
def league(league_id: str, db: Db) -> dict[str, Any]:
    return _league_json(_league_or_404(db, league_id))


@app.get("/api/warnings")
def warning_log(
    db: Db,
    league_id: str | None = None,
    resolved: bool | None = False,
    category: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    query = select(SyncWarning)
    if league_id:
        query = query.where(SyncWarning.league_id == league_id)
    if resolved is not None:
        query = query.where(SyncWarning.resolved.is_(resolved))
    if category:
        query = query.where(SyncWarning.category == category)
    rows = list(db.scalars(query.order_by(SyncWarning.last_seen_at.desc(), SyncWarning.id.desc())))
    start = (page - 1) * per_page
    return {
        "items": [
            {
                "id": item.id,
                "league_id": item.league_id,
                "source": item.source,
                "category": item.category,
                "code": item.code,
                "message": item.message,
                "details": item.details_json,
                "occurrences": item.occurrences,
                "resolved": item.resolved,
                "first_seen_at": item.first_seen_at,
                "last_seen_at": item.last_seen_at,
            }
            for item in rows[start : start + per_page]
        ],
        "pagination": {"page": page, "per_page": per_page, "total": len(rows)},
        "categories": sorted({item.category for item in rows}),
    }


@app.patch("/api/warnings/{warning_id}")
def resolve_warning(warning_id: int, payload: WarningResolve, db: Db) -> dict[str, Any]:
    warning = db.get(SyncWarning, warning_id)
    if warning is None:
        raise HTTPException(
            404, detail={"code": "warning_not_found", "message": "Warning not found"}
        )
    warning.resolved = payload.resolved
    db.commit()
    return {"id": warning.id, "resolved": warning.resolved}


@app.get("/api/leagues/{league_id}/scoring-rules")
def scoring_rules(league_id: str, db: Db) -> dict[str, Any]:
    league_item = _league_or_404(db, league_id)
    snapshot = db.scalar(
        select(MFLSnapshot)
        .where(
            MFLSnapshot.league_id == league_id,
            MFLSnapshot.export_type == "rules",
        )
        .order_by(MFLSnapshot.fetched_at.desc())
    )
    normalized = []
    for key, value in (league_item.scoring_rules_json or {}).items():
        if isinstance(value, dict):
            normalized.append(
                {
                    "key": key,
                    "positions": value.get("positions"),
                    "event": value.get("event"),
                    "description": value.get("description")
                    or RULE_DESCRIPTIONS.get(str(value.get("event", ""))),
                    "points": value.get("points"),
                    "range": value.get("range"),
                    "mapped": bool(
                        value.get("description")
                        or RULE_DESCRIPTIONS.get(str(value.get("event", "")))
                    ),
                }
            )
    return {
        "league": _league_json(league_item),
        "normalized_rules": normalized,
        "derived_settings": {
            key: value
            for key, value in (league_item.scoring_rules_json or {}).items()
            if not isinstance(value, dict)
        },
        "raw_mfl_response": snapshot.payload_json if snapshot else None,
        "source_url": snapshot.source_url if snapshot else None,
        "fetched_at": snapshot.fetched_at if snapshot else None,
    }


@app.get("/api/players/filters")
def filters(db: Db, league_id: str) -> dict[str, Any]:
    _league_or_404(db, league_id)
    return player_filters(db, league_id)


@app.get("/api/players")
def all_players(
    db: Db,
    league_id: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(100, ge=1, le=500),
    search: str | None = None,
    position: str | None = None,
    nfl_team: str | None = None,
    availability: str = Query("all", pattern="^(all|available|rostered|keeper|drafted)$"),
    owner: str | None = None,
    rookie: bool | None = None,
    injury_status: str | None = None,
    status: str | None = None,
    tier: int | None = None,
    bye_week: int | None = None,
    min_source_count: int | None = Query(None, ge=0),
    min_adp: float | None = Query(None, ge=0),
    max_adp: float | None = Query(None, ge=0),
    tag: str | None = Query(None, pattern="^(target|fade|queued|do_not_draft)$"),
    sort: str = "consensus_rank",
    direction: str = Query("asc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    _league_or_404(db, league_id)
    return query_players(
        db,
        league_id,
        page=page,
        per_page=per_page,
        search=search,
        position=position,
        nfl_team=nfl_team,
        availability=availability,
        owner=owner,
        rookie=rookie,
        injury_status=injury_status,
        status=status,
        tier=tier,
        bye_week=bye_week,
        min_source_count=min_source_count,
        min_adp=min_adp,
        max_adp=max_adp,
        tag=tag,
        sort=sort,
        direction=direction,
    )


@app.get("/api/players/{player_id}")
def get_player(player_id: str, db: Db, league_id: str) -> dict[str, Any]:
    result = player_detail(db, league_id, player_id)
    if result is None:
        raise HTTPException(404, detail={"code": "player_not_found", "message": "Player not found"})
    return result


@app.get("/api/player-identities/unresolved")
def unresolved_identities(
    db: Db, page: int = Query(1, ge=1), per_page: int = Query(100, ge=1, le=500)
) -> dict[str, Any]:
    query = (
        select(PlayerIdentity, Player)
        .join(Player, Player.id == PlayerIdentity.player_id)
        .where((PlayerIdentity.verified.is_(False)) | (PlayerIdentity.match_confidence < 0.9))
        .order_by(Player.name, Player.id)
    )
    rows = list(db.execute(query))
    start = (page - 1) * per_page
    return {
        "items": [
            {
                "player_id": player.id,
                "player_name": player.name,
                "position": player.position,
                "nfl_team": player.nfl_team,
                "gsis_id": identity.gsis_id,
                "sleeper_id": identity.sleeper_id,
                "espn_id": identity.espn_id,
                "match_method": identity.match_method,
                "match_confidence": str(identity.match_confidence),
                "verified": identity.verified,
            }
            for identity, player in rows[start : start + per_page]
        ],
        "pagination": {"page": page, "per_page": per_page, "total": len(rows)},
    }


@app.patch("/api/player-identities/{player_id}")
def update_identity(player_id: str, payload: IdentityUpdate, db: Db) -> dict[str, Any]:
    if db.get(Player, player_id) is None:
        raise HTTPException(404, detail={"code": "player_not_found", "message": "Player not found"})
    identity = db.scalar(select(PlayerIdentity).where(PlayerIdentity.player_id == player_id))
    if identity is None:
        identity = PlayerIdentity(player_id=player_id, mfl_id=player_id)
        db.add(identity)
    identity.gsis_id = payload.gsis_id
    identity.sleeper_id = payload.sleeper_id
    identity.espn_id = payload.espn_id
    identity.verified = payload.verified
    identity.match_method = "manual"
    identity.match_confidence = Decimal("1")
    identity.updated_at = datetime.now(UTC)
    db.commit()
    return {
        "player_id": player_id,
        "verified": identity.verified,
        "match_method": identity.match_method,
    }


@app.get("/api/leagues/{league_id}/rankings")
def rankings(
    league_id: str,
    db: Db,
    position: str | None = None,
    nfl_team: str | None = None,
    tier: int | None = None,
    available: bool = True,
    search: str | None = None,
    sort: str = "consensus_rank",
) -> list[dict[str, Any]]:
    result = query_players(
        db,
        league_id,
        per_page=500,
        position=position,
        nfl_team=nfl_team,
        tier=tier,
        availability="available" if available else "all",
        search=search,
        sort="league_adjusted_rank"
        if sort in {"weekly_model_rank", "league_adjusted_rank"}
        else sort,
        direction="desc" if sort in {"custom_score", "suggested_auction_value"} else "asc",
    )
    return [
        {
            **row,
            "overall_rank": row["consensus_rank"],
            "weekly_model_rank": row["league_adjusted_rank"],
        }
        for row in result["items"]
        if row["consensus_rank"] is not None
    ]


@app.get("/api/leagues/{league_id}/bye-advisor")
def get_bye_week_advice(
    league_id: str,
    db: Db,
    week: int = Query(1, ge=1, le=18),
    player_id: str | None = None,
) -> dict[str, Any]:
    _league_or_404(db, league_id)
    return bye_week_advice(db, league_id, week, player_id)


@app.get("/api/leagues/{league_id}/power-rankings")
def get_power_rankings(league_id: str, db: Db) -> dict[str, Any]:
    _league_or_404(db, league_id)
    return cached_power_rankings(db, league_id)


@app.get("/api/depth-charts")
def get_depth_charts(db: Db, team: str | None = None) -> dict[str, Any]:
    return depth_chart_overview(db, team)


@app.post("/api/leagues/{league_id}/power-rankings/chatgpt")
async def judge_power_rankings(league_id: str, db: Db) -> dict[str, Any]:
    _league_or_404(db, league_id)
    try:
        return await chatgpt_power_rankings(db, get_settings(), league_id)
    except RuntimeError as exc:
        raise HTTPException(
            503,
            detail={"code": "power_judge_unavailable", "message": str(exc)},
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            502,
            detail={
                "code": "power_judge_failed",
                "message": "ChatGPT could not judge the rankings right now",
            },
        ) from exc


@app.get("/api/leagues/{league_id}/franchises")
def franchises(league_id: str, db: Db) -> list[dict[str, Any]]:
    league = _league_or_404(db, league_id)
    return [
        franchise_budget(db, league, franchise)
        for franchise in db.scalars(
            select(Franchise).where(Franchise.league_id == league_id).order_by(Franchise.name)
        )
    ]


@app.get("/api/leagues/{league_id}/rosters")
def rosters(league_id: str, db: Db) -> dict[str, Any]:
    _league_or_404(db, league_id)
    return roster_overview(db, league_id)


@app.get("/api/leagues/{league_id}/franchises/{franchise_id}")
def franchise_detail(league_id: str, franchise_id: str, db: Db) -> dict[str, Any]:
    result = roster_overview(db, league_id)
    team = next((item for item in result["teams"] if item["franchise_id"] == franchise_id), None)
    if team is None:
        raise HTTPException(
            404, detail={"code": "franchise_not_found", "message": "Franchise not found"}
        )
    detail = cast(dict[str, Any], team)
    detail["league_type"] = result["league_type"]
    detail["stored_power"] = stored_team_power(db, league_id, franchise_id)
    return detail


@app.get("/api/leagues/{league_id}/keepers")
def keepers(league_id: str, db: Db) -> list[dict[str, Any]]:
    _league_or_404(db, league_id)
    query = (
        select(KeeperSelection, Player)
        .join(Player, Player.id == KeeperSelection.player_id)
        .where(KeeperSelection.league_id == league_id)
        .order_by(KeeperSelection.franchise_id, Player.name)
    )
    return [
        {
            "id": keeper.id,
            "franchise_id": keeper.franchise_id,
            "player_id": player.id,
            "player_name": player.name,
            "keeper_cost": str(keeper.keeper_cost) if keeper.keeper_cost is not None else None,
            "source": keeper.source,
        }
        for keeper, player in db.execute(query)
    ]


@app.post("/api/leagues/{league_id}/keepers", status_code=201)
def create_keeper(league_id: str, payload: KeeperCreate, db: Db) -> dict[str, Any]:
    if payload.league_id != league_id:
        raise HTTPException(
            400, detail={"code": "league_mismatch", "message": "League ID mismatch"}
        )
    if not db.get(Player, payload.player_id):
        raise HTTPException(404, detail={"code": "player_not_found", "message": "Player not found"})
    keeper = KeeperSelection(**payload.model_dump(), source="local")
    db.add(keeper)
    db.commit()
    db.refresh(keeper)
    return _keeper_json(keeper)


@app.delete("/api/leagues/{league_id}/keepers/{keeper_id}", status_code=204)
def remove_keeper(league_id: str, keeper_id: int, db: Db) -> None:
    keeper = db.get(KeeperSelection, keeper_id)
    if not keeper or keeper.league_id != league_id or keeper.source != "local":
        raise HTTPException(
            404, detail={"code": "keeper_not_found", "message": "Local keeper not found"}
        )
    db.delete(keeper)
    db.commit()


@app.get("/api/leagues/{league_id}/cheat-sheet")
def cheat_sheet(
    league_id: str,
    db: Db,
    page: int = Query(1, ge=1),
    per_page: int = Query(150, ge=1, le=500),
    position: str | None = None,
    availability: str = "all",
    search: str | None = None,
    rookie: bool | None = None,
    tag: str | None = Query(None, pattern="^(target|fade|queued|do_not_draft|sleeper)$"),
) -> dict[str, Any]:
    return query_players(
        db,
        league_id,
        page=page,
        per_page=per_page,
        position=position,
        availability=availability,
        search=search,
        rookie=rookie,
        tag=tag,
    )


@app.post("/api/leagues/{league_id}/cheat-sheet/recalculate")
def recalculate_cheat_sheet(
    league_id: str, db: Db, name: str = Body("Generated consensus", embed=True)
) -> dict[str, Any]:
    _league_or_404(db, league_id)
    snapshot = create_consensus_snapshot(db, league_id, name)
    return {
        "snapshot_id": snapshot.id,
        "created_at": snapshot.created_at,
        "player_count": len(draftable_consensus(db, league_id)),
    }


@app.post("/api/leagues/{league_id}/cheat-sheet/import")
async def import_cheat_sheet(
    league_id: str,
    db: Db,
    file: Annotated[UploadFile, File()],
    source_name: Annotated[str, Form()],
    confirm: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    _league_or_404(db, league_id)
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(
            413,
            detail={"code": "file_too_large", "message": "Ranking CSV must be smaller than 10 MB"},
        )
    try:
        return parse_ranking_csv(
            db,
            league_id,
            content,
            source_name,
            confirm=confirm,
            import_directory=Path("data/imports"),
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(
            422, detail={"code": "invalid_ranking_csv", "message": str(exc)}
        ) from exc


@app.get("/api/leagues/{league_id}/cheat-sheet/export.csv")
def export_cheat_sheet(league_id: str, db: Db) -> Response:
    _league_or_404(db, league_id)
    output = io.StringIO(newline="")
    fields = [
        "consensus_rank",
        "league_adjusted_rank",
        "player_id",
        "player_name",
        "position",
        "nfl_team",
        "tier",
        "source_count",
        "average_rank",
        "median_rank",
        "best_rank",
        "worst_rank",
        "rank_range",
        "adp",
        "mfl_aav",
        "suggested_auction_value",
        "max_recommended_bid",
        "dynamic_bid",
        "value_over_replacement",
        "available",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(draftable_consensus(db, league_id))
    return Response(
        output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="mfl_cheat_sheet_{league_id}.csv"'},
    )


@app.patch("/api/leagues/{league_id}/preferences/{player_id}")
def update_preference(
    league_id: str, player_id: str, payload: PreferenceUpdate, db: Db
) -> dict[str, Any]:
    _league_or_404(db, league_id)
    if db.get(Player, player_id) is None:
        raise HTTPException(404, detail={"code": "player_not_found", "message": "Player not found"})
    preference = db.scalar(
        select(UserPlayerPreference).where(
            UserPlayerPreference.username == active_username(),
            UserPlayerPreference.league_id == league_id,
            UserPlayerPreference.player_id == player_id,
        )
    )
    if preference is None:
        preference = UserPlayerPreference(
            username=active_username(), league_id=league_id, player_id=player_id
        )
        db.add(preference)
    for key, value in payload.model_dump(exclude={"tags"}).items():
        setattr(preference, key, value)
    preference.tags_json = payload.tags
    preference.updated_at = datetime.now(UTC)
    db.commit()
    return {"league_id": league_id, "player_id": player_id, **payload.model_dump()}


def _draft_state_response(
    db: Session,
    league_id: str,
    franchise_id: str | None,
    *,
    include_intelligence: bool,
    board: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    personal_franchise = league_setting(db, league_id).franchise_id
    selected_franchise = franchise_id or personal_franchise
    mock = mock_draft_status(db, league_id)
    if mock["enabled"]:
        state = mock_draft_state(
            db,
            league_id,
            selected_franchise,
            include_intelligence=include_intelligence,
            board=board,
        )
        state["draft_mode"] = draft_mode(db, league_id)
        state["selected_franchise_id"] = selected_franchise
        state["personal_franchise_id"] = personal_franchise
        return state
    state = draft_state(
        db,
        league_id,
        selected_franchise,
        include_intelligence=include_intelligence,
        board=board,
    )
    is_live = bool(state["live"]["is_live"])
    method = draft_mode(db, league_id)
    league = _league_or_404(db, league_id)
    user_franchise_id = _current_user_franchise_id(db, league)
    current_drafter = state.get("current_drafter")
    is_admin = is_current_admin(db)
    is_user_turn = bool(
        current_drafter
        and user_franchise_id
        and current_drafter.get("franchise_id") == user_franchise_id
    )
    can_make_pick = bool(is_live and method == "local" and (is_admin or is_user_turn))
    if not is_live:
        locked_reason = "The admin has not started the live draft"
    elif method == "companion":
        locked_reason = "Companion mode: make picks on MFL; DraftDesk imports them every 30 seconds"
    elif current_drafter is None:
        locked_reason = "The draft is complete"
    elif not user_franchise_id and not is_admin:
        locked_reason = "Connect your MFL team in My Account to make local picks"
    else:
        locked_reason = (
            f"Waiting for {current_drafter.get('franchise_name') or 'the team on the clock'}"
        )
    state.update(
        {
            "mode": "real",
            "draft_mode": method,
            "mock": mock,
            "permissions": {
                "can_make_pick": can_make_pick,
                "locked_reason": None if can_make_pick else locked_reason,
            },
            "selected_franchise_id": selected_franchise,
            "personal_franchise_id": personal_franchise,
        }
    )
    return state


@app.get("/api/draft/state")
def api_draft_state(
    league_id: str,
    db: Db,
    franchise_id: str | None = None,
    include_intelligence: bool = True,
) -> dict[str, Any]:
    return _draft_state_response(
        db,
        league_id,
        franchise_id,
        include_intelligence=include_intelligence,
    )


@app.get("/api/draft/bootstrap")
def api_draft_bootstrap(
    league_id: str,
    db: Db,
    franchise_id: str | None = None,
) -> dict[str, Any]:
    """Return the initial Draft Room board from one shared consensus calculation."""
    board = draftable_consensus(db, league_id)
    return {
        "state": _draft_state_response(
            db,
            league_id,
            franchise_id,
            include_intelligence=False,
            board=board,
        ),
        "players": query_players(
            db,
            league_id,
            availability="available",
            per_page=500,
            board=board,
        ),
        "franchises": franchises(league_id, db),
    }


@app.get("/api/draft/intelligence")
def api_draft_intelligence(
    league_id: str, db: Db, franchise_id: str | None = None
) -> dict[str, Any]:
    selected_franchise = franchise_id or league_setting(db, league_id).franchise_id
    return draft_intelligence(db, league_id, selected_franchise)


@app.get("/api/draft/analysis")
def api_draft_analysis(
    league_id: str,
    db: Db,
    franchise_id: str | None = None,
    what_if_overall_pick: int | None = Query(None, ge=1),
    alternative_player_id: str | None = None,
) -> dict[str, Any]:
    selected_franchise = franchise_id or league_setting(db, league_id).franchise_id
    return cached_draft_analysis(
        db,
        league_id,
        selected_franchise,
        what_if_overall_pick=what_if_overall_pick,
        alternative_player_id=alternative_player_id,
    )


@app.get("/api/leagues/{league_id}/events")
def league_event_stream(league_id: str) -> StreamingResponse:
    # Validate access with a short-lived session. A FastAPI yield dependency would remain open
    # until this never-ending SSE response closes, eventually exhausting the connection pool.
    with SessionLocal() as db:
        _league_or_404(db, league_id)
    return StreamingResponse(
        league_events.stream(league_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.put("/api/draft/live")
def api_set_draft_live(payload: AuctionLiveUpdate, league_id: str, db: Db) -> dict[str, Any]:
    _require_admin(db)
    result = set_draft_live(db, league_id, payload.is_live)
    league_events.publish(league_id, "draft-live", result)
    return result


@app.put("/api/admin/draft-mode")
def update_admin_draft_mode(payload: DraftModeUpdate, league_id: str, db: Db) -> dict[str, Any]:
    _require_admin(db)
    _league_or_404(db, league_id)
    previous = draft_mode(db, league_id)
    was_live = bool(draft_state(db, league_id, include_intelligence=False)["live"]["is_live"])
    paused = previous != payload.mode and was_live
    if paused:
        set_draft_live(db, league_id, False)
    mode = save_draft_mode(db, league_id, payload.mode)
    return {"mode": mode, "is_live": was_live and not paused, "paused": paused}


@app.get("/api/admin/mock-draft")
def admin_mock_draft(league_id: str, db: Db) -> dict[str, Any]:
    _require_admin(db)
    return mock_draft_status(db, league_id)


@app.put("/api/admin/mock-draft")
def update_admin_mock_draft(payload: MockDraftUpdate, league_id: str, db: Db) -> dict[str, Any]:
    _require_admin(db)
    return set_mock_draft_enabled(
        db,
        league_id,
        payload.enabled,
        actor=active_username(),
    )


@app.post("/api/admin/mock-draft/reset")
def reset_admin_mock_draft(league_id: str, db: Db) -> dict[str, Any]:
    _require_admin(db)
    return reset_mock_draft(db, league_id, actor=active_username())


@app.post("/api/draft/picks", status_code=201)
def create_draft_pick(
    payload: DraftPickCreate, background_tasks: BackgroundTasks, db: Db
) -> dict[str, Any]:
    if payload.is_mock:
        state = mock_draft_state(db, payload.league_id, include_intelligence=False)
        current = state.get("current_drafter")
        if not state["mock"]["enabled"]:
            raise HTTPException(
                409,
                detail={"code": "mock_locked", "message": "Shared mock draft is not enabled"},
            )
        if current is None:
            raise HTTPException(
                409, detail={"code": "mock_complete", "message": "The mock draft is complete"}
            )
        if payload.overall_pick not in (None, current["overall_pick"]):
            raise HTTPException(
                409,
                detail={
                    "code": "mock_pick_moved",
                    "message": "Another participant already made that mock pick; refresh the board",
                },
            )
        mock_payload = payload.model_copy(
            update={
                "franchise_id": current["franchise_id"],
                "round": current["round"],
                "pick": current["pick"],
                "overall_pick": current["overall_pick"],
            }
        )
        result = mock_pick_json(db, add_mock_pick(db, mock_payload, actor=active_username()))
        stream = "mock-draft-picks"
    else:
        state = draft_state(db, payload.league_id, include_intelligence=False)
        if not state["live"]["is_live"]:
            raise HTTPException(
                409,
                detail={
                    "code": "draft_locked",
                    "message": "Players are locked until an admin starts the live draft",
                },
            )
        if draft_mode(db, payload.league_id) == "companion":
            raise HTTPException(
                409,
                detail={
                    "code": "mfl_companion_only",
                    "message": "Make real draft picks on MFL; DraftDesk imports them automatically",
                },
            )
        current = state.get("current_drafter")
        if current is None:
            raise HTTPException(
                409,
                detail={"code": "draft_complete", "message": "The draft is complete"},
            )
        league = _league_or_404(db, payload.league_id)
        user_franchise_id = _current_user_franchise_id(db, league)
        if not is_current_admin(db) and user_franchise_id != current.get("franchise_id"):
            raise HTTPException(
                403,
                detail={
                    "code": "not_on_clock",
                    "message": "Only the team currently on the clock can make this pick",
                },
            )
        if payload.overall_pick not in (None, current["overall_pick"]):
            raise HTTPException(
                409,
                detail={
                    "code": "draft_pick_moved",
                    "message": "Another participant already made that pick; refresh the board",
                },
            )
        local_payload = payload.model_copy(
            update={
                "franchise_id": current["franchise_id"],
                "round": current["round"],
                "pick": current["pick"],
                "overall_pick": current["overall_pick"],
            }
        )
        result = pick_json(db, add_pick(db, local_payload))
        stream = "draft-picks"
    _audit_mutation(
        db,
        stream=stream,
        action="create",
        league_id=payload.league_id,
        entity_id=result["id"],
        after=result,
    )
    if not payload.is_mock:
        _queue_round_power_refresh(background_tasks, db, payload.league_id, "draft")
    return result


@app.patch("/api/draft/picks/{pick_id}")
def patch_draft_pick(pick_id: str, payload: DraftPickUpdate, db: Db) -> dict[str, Any]:
    _require_admin(db)
    current = db.get(DraftPick, pick_id)
    if (
        current is not None
        and not draft_state(db, current.league_id, include_intelligence=False)["live"]["is_live"]
    ):
        raise HTTPException(
            409,
            detail={"code": "draft_locked", "message": "The live draft is not active"},
        )
    before = pick_json(db, current) if current else None
    result = pick_json(db, update_pick(db, pick_id, payload))
    _audit_mutation(
        db,
        stream="draft-picks",
        action="update",
        league_id=result["league_id"],
        entity_id=pick_id,
        before=before,
        after=result,
    )
    return result


@app.delete("/api/draft/picks/{pick_id}", status_code=204)
def delete_draft_pick(pick_id: str, db: Db) -> None:
    _require_admin(db)
    current = db.get(DraftPick, pick_id)
    if current is None:
        raise DraftValidationError("Draft pick does not exist")
    before = pick_json(db, current)
    league_id = current.league_id
    if not draft_state(db, league_id, include_intelligence=False)["live"]["is_live"]:
        raise HTTPException(
            409,
            detail={"code": "draft_locked", "message": "The live draft is not active"},
        )
    remove_pick(db, pick_id)
    _audit_mutation(
        db,
        stream="draft-picks",
        action="delete",
        league_id=league_id,
        entity_id=pick_id,
        before=before,
    )


@app.post("/api/draft/undo", status_code=204)
def api_draft_undo(league_id: str, db: Db) -> None:
    _require_admin(db)
    if not draft_state(db, league_id, include_intelligence=False)["live"]["is_live"]:
        raise HTTPException(
            409,
            detail={"code": "draft_locked", "message": "The live draft is not active"},
        )
    undo_draft(db, league_id)
    _audit_mutation(db, stream="draft-picks", action="undo", league_id=league_id)


@app.get("/api/draft/recommendations")
def api_recommendations(
    league_id: str, db: Db, franchise_id: str | None = None, limit: int = Query(12, ge=1, le=50)
) -> list[dict[str, Any]]:
    return recommendations(db, league_id, franchise_id, limit)


@app.post("/api/draft/reconcile")
async def reconcile_draft(
    league_id: str,
    db: Db,
    background_tasks: BackgroundTasks,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
    apply: bool = False,
) -> dict[str, Any]:
    remote = payload
    if remote is None:
        settings = runtime_settings(db)
        async with MFLClient(settings) as client:
            result = await client.export("draftResults", league_id=league_id, db=db, force=True)
            remote = result.payload
    preview = reconcile_preview(db, league_id, remote)
    if not apply:
        return {"applied": False, **preview}
    count = apply_reconciliation(db, league_id, preview)
    _audit_mutation(
        db,
        stream="draft-picks",
        action="mfl_reconcile",
        league_id=league_id,
        details={"applied_count": count, "preview": preview},
    )
    if count:
        _queue_round_power_refresh(background_tasks, db, league_id, "draft")
    return {"applied": True, "applied_count": count, **preview}


@app.get("/api/draft/export.csv")
def download_draft_csv(league_id: str, db: Db) -> FileResponse:
    path = export_draft_csv(db, league_id, runtime_settings(db).export_directory)
    return FileResponse(path, media_type="text/csv", filename=path.name)


@app.get("/api/account")
def account_settings(db: Db) -> dict[str, Any]:
    account = current_account(db)
    leagues = list(db.scalars(select(League).order_by(League.name)))
    allowed = authorized_league_ids(db)
    if allowed is not None:
        leagues = [item for item in leagues if item.id in allowed]
    memberships = mfl_memberships_for_user(db, runtime_settings(db).mfl_season)
    connected_ids = {item.id for item in leagues}
    return {
        "username": account.username,
        "display_name": account.display_name,
        "is_admin": account.is_admin,
        "available_leagues": [
            {
                "id": item.league_id,
                "name": item.league_name or f"MFL League {item.league_id}",
                "mfl_franchise_id": item.franchise_id,
                "connected": item.league_id in connected_ids,
            }
            for item in memberships
        ],
        "leagues": [
            {
                "id": league.id,
                "name": league.name,
                "type": league.league_type,
                "franchise_id": league_setting(db, league.id).franchise_id,
                "auction_strategy": league_setting(db, league.id).auction_strategy_json,
                "franchises": [
                    {"id": item.id, "name": item.name}
                    for item in db.scalars(
                        select(Franchise)
                        .where(Franchise.league_id == league.id)
                        .order_by(Franchise.name)
                    )
                ],
            }
            for league in leagues
        ],
        "auction_strategies": {
            key: strategy_json(value) for key, value in AUCTION_STRATEGIES.items()
        },
    }


@app.post("/api/account/leagues")
async def connect_account_league(payload: LeagueConnect, db: Db) -> dict[str, Any]:
    settings = runtime_settings(db)
    membership = db.scalar(
        select(UserMFLMembership).where(
            UserMFLMembership.username == active_username(),
            UserMFLMembership.season == settings.mfl_season,
            UserMFLMembership.league_id == payload.league_id,
        )
    )
    if membership is None:
        raise HTTPException(
            403,
            detail={
                "code": "league_not_authorized",
                "message": "This league was not returned by your MFL sign-in",
            },
        )
    try:
        async with MFLClient(settings) as client:
            await sync_league(
                db,
                client,
                settings,
                payload.league_id,
                LeagueType(payload.league_type),
            )
    except (MFLError, ValueError) as exc:
        raise HTTPException(
            502,
            detail={
                "code": "league_connect_failed",
                "message": f"MFL could not sync this league: {exc}",
            },
        ) from exc
    setting = league_setting(db, payload.league_id)
    if membership.franchise_id:
        matching = db.scalar(
            select(Franchise).where(
                Franchise.league_id == payload.league_id,
                Franchise.id == membership.franchise_id,
            )
        )
        if matching is not None:
            setting.franchise_id = matching.id
            db.commit()
    return {
        "league_id": payload.league_id,
        "league_type": payload.league_type,
        "franchise_id": setting.franchise_id,
    }


@app.put("/api/admin/leagues/{league_id}/format")
def update_league_format(league_id: str, payload: LeagueFormatUpdate, db: Db) -> dict[str, Any]:
    """Change the shared draft format for every user of a connected league."""
    _require_admin(db)
    league = _league_or_404(db, league_id)
    before = {"league_type": league.league_type, "starting_budget": str(league.starting_budget)}
    league.league_type = payload.league_type
    if payload.league_type == LeagueType.AUCTION:
        settings = runtime_settings(db)
        raw_budget = next(
            (
                league.settings_json.get(key)
                for key in ("auctionStartingFunds", "auctionFunds", "salaryCapAmount")
                if league.settings_json.get(key) not in (None, "")
            ),
            None,
        )
        try:
            starting_budget = Decimal(str(raw_budget)) if raw_budget is not None else None
        except (ArithmeticError, ValueError):
            starting_budget = None
        starting_budget = (
            league.starting_budget or starting_budget or settings.auction_default_budget
        )
        league.starting_budget = starting_budget
        for franchise in db.scalars(select(Franchise).where(Franchise.league_id == league_id)):
            if Decimal(franchise.starting_budget or 0) <= 0:
                franchise.starting_budget = starting_budget
    db.commit()
    after = {"league_type": league.league_type, "starting_budget": str(league.starting_budget)}
    _audit_mutation(
        db,
        stream="league-format",
        action="update",
        league_id=league_id,
        entity_id=league_id,
        before=before,
        after=after,
    )
    return {"league_id": league.id, **after}


@app.put("/api/account/leagues/{league_id}")
def save_account_league(league_id: str, payload: UserLeagueSettingUpdate, db: Db) -> dict[str, Any]:
    _league_or_404(db, league_id)
    if payload.franchise_id:
        franchise = db.scalar(
            select(Franchise).where(
                Franchise.league_id == league_id,
                Franchise.id == payload.franchise_id,
            )
        )
        if franchise is None:
            raise HTTPException(
                422,
                detail={
                    "code": "franchise_mismatch",
                    "message": "Choose a franchise in this league",
                },
            )
    strategy = dict(payload.auction_strategy)
    template = str(strategy.get("template") or "balanced")
    if template not in AUCTION_STRATEGIES:
        raise HTTPException(422, detail={"code": "invalid_strategy", "message": "Unknown strategy"})
    if "priority_order" in strategy:
        allowed = {"QB", "RB", "WR", "TE", "DEF"}
        raw_priority = strategy["priority_order"]
        if not isinstance(raw_priority, list):
            raise HTTPException(
                422,
                detail={"code": "invalid_priority", "message": "Position order must be a list"},
            )
        priority = [str(item).upper() for item in raw_priority]
        if set(priority) != allowed or len(priority) != len(allowed):
            raise HTTPException(
                422,
                detail={"code": "invalid_priority", "message": "Use each position once"},
            )
        strategy["priority_order"] = priority
    setting = league_setting(db, league_id)
    setting.franchise_id = payload.franchise_id
    setting.auction_strategy_json = strategy
    setting.updated_at = datetime.now(UTC)
    db.commit()
    return {
        "league_id": league_id,
        "franchise_id": setting.franchise_id,
        "auction_strategy": strategy,
    }


@app.get("/api/assistant/status")
def assistant_status(db: Db, league_id: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    result: dict[str, Any] = {
        "enabled": bool(settings.openai_api_key),
        "model": settings.openai_model,
        "league_id": None,
        "league_name": None,
        "league_type": None,
        "franchise_id": None,
        "franchise_name": None,
    }
    if not league_id:
        return result
    league = _league_or_404(db, league_id)
    setting = league_setting(db, league_id)
    franchise = (
        db.scalar(
            select(Franchise).where(
                Franchise.league_id == league_id,
                Franchise.id == setting.franchise_id,
            )
        )
        if setting.franchise_id
        else None
    )
    result.update(
        {
            "league_id": league.id,
            "league_name": league.name,
            "league_type": league.league_type,
            "franchise_id": franchise.id if franchise else None,
            "franchise_name": franchise.name if franchise else None,
        }
    )
    return result


@app.post("/api/assistant")
async def league_assistant(payload: AssistantRequest, db: Db) -> dict[str, str]:
    _league_or_404(db, payload.league_id)
    try:
        answer = await ask_assistant(
            db, get_settings(), payload.league_id, payload.message, payload.history
        )
    except RuntimeError as exc:
        raise HTTPException(
            503, detail={"code": "assistant_unavailable", "message": str(exc)}
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            502,
            detail={"code": "assistant_failed", "message": "The assistant service is unavailable"},
        ) from exc
    return {"answer": answer}


@app.post("/api/leagues/{league_id}/compare/chatgpt")
async def compare_players_with_chatgpt(
    league_id: str, payload: PlayerComparisonRequest, db: Db
) -> dict[str, Any]:
    league = _league_or_404(db, league_id)
    board = {row["player_id"]: row for row in draftable_consensus(db, league_id)}
    missing = [player_id for player_id in payload.player_ids if player_id not in board]
    if missing:
        raise HTTPException(
            404,
            detail={
                "code": "comparison_player_not_found",
                "message": "One or more comparison players are not on this league board",
            },
        )
    players = []
    for player_id in payload.player_ids:
        row = board[player_id]
        players.append(
            {
                "player_id": player_id,
                "name": row["player_name"],
                "position": row["position"],
                "team": row["nfl_team"],
                "available": row["available"],
                "consensus_rank": row["consensus_rank"],
                "league_adjusted_rank": row.get("league_adjusted_rank"),
                "tier": row.get("tier"),
                "source_count": row.get("source_count"),
                "average_rank": row.get("average_rank"),
                "median_rank": row.get("median_rank"),
                "best_rank": row.get("best_rank"),
                "worst_rank": row.get("worst_rank"),
                "adp": row.get("adp"),
                "projected_points": row.get("projected_points"),
                "value_over_replacement": row.get("value_over_replacement"),
                "suggested_auction_value": row.get("suggested_auction_value"),
                "dynamic_bid": row.get("dynamic_bid"),
                "max_recommended_bid": row.get("max_recommended_bid"),
                "injury_status": row.get("injury_status"),
                "bye_week": row.get("bye_week"),
            }
        )
    message = (
        f"Break a draft-day tie in {league.name}. Compare only the supplied candidates and pick "
        "one. Explain the choice using league fit, tier, value over replacement, market cost, "
        "availability, and injury risk. Be decisive but mention the strongest reason to choose "
        f"each alternative. Candidate data: {json.dumps(players, default=str)}"
    )
    try:
        answer = await ask_assistant(db, get_settings(), league_id, message, [])
    except RuntimeError as exc:
        raise HTTPException(
            503, detail={"code": "assistant_unavailable", "message": str(exc)}
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            502,
            detail={"code": "assistant_failed", "message": "The assistant service is unavailable"},
        ) from exc
    return {"answer": answer, "players": players}


@app.post("/api/leagues/{league_id}/compare/recommendation")
def compare_players_recommendation(
    league_id: str, payload: PlayerComparisonRequest, db: Db
) -> dict[str, Any]:
    _league_or_404(db, league_id)
    board = {row["player_id"]: row for row in draftable_consensus(db, league_id)}
    missing = [player_id for player_id in payload.player_ids if player_id not in board]
    if missing:
        raise HTTPException(
            404,
            detail={
                "code": "comparison_player_not_found",
                "message": "One or more comparison players are not on this league board",
            },
        )

    setting = league_setting(db, league_id)
    franchise = None
    needs: dict[str, int] = {}
    if setting.franchise_id:
        franchise = db.scalar(
            select(Franchise).where(
                Franchise.league_id == league_id,
                Franchise.id == setting.franchise_id,
            )
        )
        if franchise is not None:
            needs = franchise_position_needs(db, league_id, franchise.id)

    candidates = []
    for player_id in payload.player_ids:
        row = board[player_id]
        position = str(row["position"] or "").upper()
        overall_rank = int(row.get("consensus_rank") or 99999)
        need_slots = needs.get(position, 0)
        need_bonus = min(need_slots, 2) * 12
        candidates.append(
            {
                "player_id": player_id,
                "player_name": row["player_name"],
                "position": position,
                "nfl_team": row.get("nfl_team"),
                "available": bool(row.get("available")),
                "overall_rank": overall_rank,
                "need_slots": need_slots,
                "need_adjusted_rank": max(1, overall_rank - need_bonus),
            }
        )
    candidates.sort(
        key=lambda item: (
            not item["available"],
            item["need_adjusted_rank"],
            -item["need_slots"],
            item["overall_rank"],
            item["player_name"],
        )
    )
    for index, candidate in enumerate(candidates, 1):
        candidate["recommendation_rank"] = index

    available = [candidate for candidate in candidates if candidate["available"]]
    recommended = available[0] if available else None
    if recommended is None:
        reason = "None of these players is currently available in this league."
    elif franchise is None:
        reason = (
            f"Draft {recommended['player_name']} based on the live #{recommended['overall_rank']} "
            "overall rank. Select your franchise in My Account to add team needs."
        )
    elif recommended["need_slots"]:
        slots = recommended["need_slots"]
        reason = (
            f"Draft {recommended['player_name']}: #{recommended['overall_rank']} overall and "
            f"fills {slots} open {recommended['position']} starter "
            f"slot{'s' if slots != 1 else ''} for {franchise.name}."
        )
    else:
        reason = (
            f"Draft {recommended['player_name']}: none of these candidates fills an open primary "
            f"starter slot for {franchise.name}, so the live #{recommended['overall_rank']} "
            "overall rank leads the decision."
        )
    return {
        "franchise_id": franchise.id if franchise else None,
        "franchise_name": franchise.name if franchise else None,
        "needs": needs,
        "recommended_player_id": recommended["player_id"] if recommended else None,
        "reason": reason,
        "candidates": candidates,
    }


@app.get("/api/auction/state")
def auction_state(db: Db, league_id: str | None = None) -> dict[str, Any]:
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_auction_league_id
    if not selected:
        raise HTTPException(
            400,
            detail={
                "code": "auction_not_configured",
                "message": "Configure the auction league in Settings",
            },
        )
    league = _league_or_404(db, selected)
    live = _auction_live(db, selected)
    staged = auction_stage_enabled(db, selected)
    is_admin = is_current_admin(db)
    rob_mode = auction_rob_mode(db)
    interactive = _interactive_auction_json(db, selected)
    current_user_franchise_id = _current_user_franchise_id(db, league)
    can_record = (
        (live.is_live and (is_admin or not rob_mode)) or (staged and is_admin)
    ) and not interactive["enabled"]
    phase = "live" if live.is_live else "staging" if staged else "closed"
    budgets = [
        franchise_budget(db, league, franchise)
        for franchise in db.scalars(
            select(Franchise).where(Franchise.league_id == selected).order_by(Franchise.name)
        )
    ]
    purchases = [
        _purchase_json(db, item)
        for item in db.scalars(
            select(AuctionPurchase)
            .where(AuctionPurchase.league_id == selected, AuctionPurchase.active.is_(True))
            .order_by(AuctionPurchase.purchase_order.desc())
        )
    ]
    room_intelligence = _auction_room_intelligence(db, league, budgets, purchases)
    return {
        "league": _league_json(league),
        "franchises": budgets,
        "purchases": purchases,
        "synced_at": league.synced_at,
        "stale": not league.synced_at,
        "live": {
            "is_live": live.is_live,
            "stage_enabled": staged,
            "phase": phase,
            "revision": live.revision,
            "updated_at": live.updated_at,
            "updated_by": live.updated_by,
        },
        "nomination": nomination_state(db, selected),
        "is_admin": is_admin,
        "rob_mode": rob_mode,
        "phase": phase,
        "can_record_purchase": can_record,
        "can_record_own_purchase": bool(can_record and current_user_franchise_id),
        "current_user_franchise_id": current_user_franchise_id,
        "current_user_franchise_name": interactive["current_user_franchise_name"],
        "interactive_bidding": interactive,
        **room_intelligence,
    }


@app.get("/api/admin/auction-stage")
def admin_auction_stage(db: Db, league_id: str | None = None) -> dict[str, Any]:
    _require_admin(db)
    selected = league_id or runtime_settings(db).mfl_auction_league_id
    _league_or_404(db, selected)
    live = _auction_live(db, selected)
    staged = auction_stage_enabled(db, selected)
    return {
        "league_id": selected,
        "stage_enabled": staged,
        "is_live": live.is_live,
        "phase": "live" if live.is_live else "staging" if staged else "closed",
    }


@app.put("/api/admin/auction-stage")
def update_admin_auction_stage(
    payload: AuctionStageUpdate, db: Db, league_id: str | None = None
) -> dict[str, Any]:
    _require_admin(db)
    selected = league_id or runtime_settings(db).mfl_auction_league_id
    _league_or_404(db, selected)
    save_auction_stage(db, selected, payload.enabled)
    live = _auction_live(db, selected)
    if not payload.enabled and live.is_live:
        live.is_live = False
        live.revision += 1
        live.updated_by = active_username()
        live.updated_at = datetime.now(UTC)
        db.commit()
    result = admin_auction_stage(db, selected)
    league_events.publish(selected, "auction-stage", result)
    return result


@app.get("/api/admin/interactive-auction")
def admin_interactive_auction(league_id: str, db: Db) -> dict[str, Any]:
    _require_admin(db)
    return _interactive_auction_json(db, league_id)


@app.put("/api/admin/interactive-auction")
def update_admin_interactive_auction(
    payload: InteractiveAuctionUpdate, league_id: str, db: Db
) -> dict[str, Any]:
    _require_admin(db)
    _league_or_404(db, league_id)
    state = _interactive_auction(db, league_id)
    state.enabled = payload.enabled
    state.revision += 1
    state.updated_at = datetime.now(UTC)
    if not payload.enabled:
        state.status = "idle"
        state.player_id = None
        state.nominating_franchise_id = None
        state.high_bid_franchise_id = None
        state.current_bid = None
        state.nominated_by = None
        state.opened_at = None
    db.commit()
    _bump_auction(db, league_id)
    return _interactive_auction_json(db, league_id)


@app.post("/api/admin/interactive-auction/handoff")
def handoff_auction_to_live_bidding(league_id: str, db: Db) -> dict[str, Any]:
    """Atomically move a manual auction into the shared owner bidding room."""
    _require_admin(db)
    _league_or_404(db, league_id)
    nomination_before = nomination_state(db, league_id)
    purchase_count = int(
        db.scalar(
            select(func.count(AuctionPurchase.id)).where(
                AuctionPurchase.league_id == league_id,
                AuctionPurchase.active.is_(True),
            )
        )
        or 0
    )
    live = _auction_live(db, league_id)
    interactive = _interactive_auction(db, league_id)
    before = {
        "live": live.is_live,
        "interactive": interactive.enabled,
        "purchase_count": purchase_count,
        "nomination": nomination_before,
    }
    save_auction_stage(db, league_id, True)
    live.is_live = True
    interactive.enabled = True
    interactive.revision += 1
    interactive.updated_at = datetime.now(UTC)
    db.commit()
    live = _bump_auction(db, league_id)
    room = _interactive_auction_json(db, league_id)
    result = {
        "mode": "live_owner_bidding",
        "preserved_purchase_count": purchase_count,
        "live": {"is_live": live.is_live, "revision": live.revision},
        "nomination": nomination_state(db, league_id),
        "interactive_bidding": room,
    }
    _audit_mutation(
        db,
        stream="auction-mode",
        action="handoff_to_live_bidding",
        league_id=league_id,
        before=before,
        after=result,
    )
    return result


@app.post("/api/auction/interactive/nominate", status_code=201)
def nominate_interactive_auction_player(
    payload: InteractiveAuctionNominationCreate, db: Db
) -> dict[str, Any]:
    league = _league_or_404(db, payload.league_id)
    state = db.scalar(
        select(InteractiveAuctionState)
        .where(InteractiveAuctionState.league_id == payload.league_id)
        .with_for_update()
    ) or _interactive_auction(db, payload.league_id)
    if not state.enabled or not _auction_live(db, payload.league_id).is_live:
        raise HTTPException(
            409,
            detail={"code": "interactive_auction_locked", "message": "Live bidding is not active"},
        )
    if state.status == "open":
        raise HTTPException(
            409,
            detail={
                "code": "nomination_in_progress",
                "message": "A player is already being bid on",
            },
        )
    user_franchise_id = _current_user_franchise_id(db, league)
    nomination = nomination_state(db, payload.league_id)
    if not user_franchise_id or user_franchise_id != nomination.get("current_franchise_id"):
        raise HTTPException(
            403,
            detail={
                "code": "not_current_nominator",
                "message": "Only the team currently up may nominate a player",
            },
        )
    board = {row["player_id"]: row for row in draftable_consensus(db, payload.league_id)}
    player_row = board.get(payload.player_id)
    if not player_row or not player_row.get("available"):
        raise HTTPException(
            409,
            detail={"code": "player_unavailable", "message": "That player is not available"},
        )
    franchise = db.scalar(
        select(Franchise).where(
            Franchise.league_id == payload.league_id,
            Franchise.id == user_franchise_id,
        )
    )
    if franchise is None:
        raise HTTPException(409, detail={"code": "franchise_missing", "message": "Team not found"})
    budget = franchise_budget(db, league, franchise)
    opening_bid = Decimal(league.minimum_bid)
    if budget["slots_remaining"] <= 0 or Decimal(budget["maximum_bid"]) < opening_bid:
        raise HTTPException(
            409,
            detail={"code": "cannot_nominate", "message": "Your team cannot make the opening bid"},
        )
    now = datetime.now(UTC)
    state.status = "open"
    state.player_id = payload.player_id
    state.nominating_franchise_id = user_franchise_id
    state.high_bid_franchise_id = user_franchise_id
    state.current_bid = opening_bid
    state.revision += 1
    state.nominated_by = active_username()
    state.opened_at = now
    state.updated_at = now
    db.add(
        InteractiveAuctionBid(
            league_id=payload.league_id,
            player_id=payload.player_id,
            franchise_id=user_franchise_id,
            username=active_username(),
            amount=opening_bid,
        )
    )
    db.commit()
    _bump_auction(db, payload.league_id)
    return _interactive_auction_json(db, payload.league_id)


@app.post("/api/auction/interactive/bids", status_code=201)
def place_interactive_auction_bid(payload: InteractiveAuctionBidCreate, db: Db) -> dict[str, Any]:
    league = _league_or_404(db, payload.league_id)
    state = db.scalar(
        select(InteractiveAuctionState)
        .where(InteractiveAuctionState.league_id == payload.league_id)
        .with_for_update()
    )
    if (
        state is None
        or not state.enabled
        or state.status != "open"
        or not state.player_id
        or not _auction_live(db, payload.league_id).is_live
    ):
        raise HTTPException(
            409,
            detail={"code": "no_active_nomination", "message": "No player is open for bidding"},
        )
    user_franchise_id = _current_user_franchise_id(db, league)
    if not user_franchise_id:
        raise HTTPException(
            403,
            detail={"code": "franchise_required", "message": "Select your MFL franchise first"},
        )
    if user_franchise_id == state.high_bid_franchise_id:
        raise HTTPException(
            409,
            detail={"code": "already_high_bidder", "message": "Your team already has the high bid"},
        )
    minimum_next = Decimal(state.current_bid or 0) + Decimal(league.minimum_bid)
    amount = Decimal(payload.amount)
    if amount < minimum_next:
        raise HTTPException(
            409,
            detail={"code": "bid_too_low", "message": f"The next bid is at least {minimum_next}"},
        )
    configured_precision = league.settings_json.get("precision")
    normalized_exponent = amount.normalize().as_tuple().exponent
    minimum_exponent = Decimal(league.minimum_bid).normalize().as_tuple().exponent
    try:
        allowed_precision = (
            int(str(configured_precision))
            if configured_precision not in (None, "")
            else max(0, -int(minimum_exponent))
        )
    except (TypeError, ValueError):
        allowed_precision = max(0, -int(minimum_exponent))
    if max(0, -int(normalized_exponent)) > allowed_precision:
        raise HTTPException(
            409,
            detail={
                "code": "invalid_bid_precision",
                "message": "That bid uses more decimal places than this league permits",
            },
        )
    franchise = db.scalar(
        select(Franchise).where(
            Franchise.league_id == payload.league_id,
            Franchise.id == user_franchise_id,
        )
    )
    if franchise is None:
        raise HTTPException(409, detail={"code": "franchise_missing", "message": "Team not found"})
    budget = franchise_budget(db, league, franchise)
    if budget["slots_remaining"] <= 0 or amount > Decimal(budget["maximum_bid"]):
        raise HTTPException(
            409,
            detail={"code": "bid_over_budget", "message": "That bid is above your legal maximum"},
        )
    state.high_bid_franchise_id = user_franchise_id
    state.current_bid = amount
    state.revision += 1
    state.updated_at = datetime.now(UTC)
    db.add(
        InteractiveAuctionBid(
            league_id=payload.league_id,
            player_id=state.player_id,
            franchise_id=user_franchise_id,
            username=active_username(),
            amount=amount,
        )
    )
    db.commit()
    _bump_auction(db, payload.league_id)
    return _interactive_auction_json(db, payload.league_id)


@app.post("/api/admin/interactive-auction/award", status_code=201)
def award_interactive_auction(
    league_id: str, background_tasks: BackgroundTasks, db: Db
) -> dict[str, Any]:
    _require_admin(db)
    state = _interactive_auction(db, league_id)
    if (
        not state.enabled
        or state.status != "open"
        or not state.player_id
        or not state.high_bid_franchise_id
        or state.current_bid is None
    ):
        raise HTTPException(
            409, detail={"code": "no_active_nomination", "message": "No winning bid is ready"}
        )
    purchase = add_purchase(
        db,
        PurchaseCreate(
            league_id=league_id,
            player_id=state.player_id,
            franchise_id=state.high_bid_franchise_id,
            amount=state.current_bid,
            status="ROSTER",
        ),
    )
    created = _purchase_json(db, purchase)
    advance_nomination(db, league_id, actor=active_username())
    state.status = "idle"
    state.player_id = None
    state.nominating_franchise_id = None
    state.high_bid_franchise_id = None
    state.current_bid = None
    state.revision += 1
    state.nominated_by = None
    state.opened_at = None
    state.updated_at = datetime.now(UTC)
    db.commit()
    _bump_auction(db, league_id)
    _audit_mutation(
        db,
        stream="auction-purchases",
        action="interactive_award",
        league_id=league_id,
        entity_id=purchase.id,
        after=created,
    )
    _queue_round_power_refresh(background_tasks, db, league_id, "auction")
    return created


@app.post("/api/admin/interactive-auction/cancel")
def cancel_interactive_auction_nomination(league_id: str, db: Db) -> dict[str, Any]:
    _require_admin(db)
    state = _interactive_auction(db, league_id)
    state.status = "idle"
    state.player_id = None
    state.nominating_franchise_id = None
    state.high_bid_franchise_id = None
    state.current_bid = None
    state.revision += 1
    state.nominated_by = None
    state.opened_at = None
    state.updated_at = datetime.now(UTC)
    db.commit()
    _bump_auction(db, league_id)
    return _interactive_auction_json(db, league_id)


@app.put("/api/auction/live")
def set_auction_live(
    payload: AuctionLiveUpdate, db: Db, league_id: str | None = None
) -> dict[str, Any]:
    _require_admin(db)
    selected = league_id or runtime_settings(db).mfl_auction_league_id
    _league_or_404(db, selected)
    state = _auction_live(db, selected)
    if payload.is_live:
        save_auction_stage(db, selected, True)
    state.is_live = payload.is_live
    state.revision += 1
    state.updated_by = active_username()
    state.updated_at = datetime.now(UTC)
    db.commit()
    result = {"is_live": state.is_live, "revision": state.revision}
    league_events.publish(selected, "auction-live", result)
    return result


@app.put("/api/auction/nomination-order")
def update_auction_nomination_order(
    payload: AuctionNominationOrderUpdate, db: Db, league_id: str | None = None
) -> dict[str, Any]:
    _require_admin(db)
    selected = league_id or runtime_settings(db).mfl_auction_league_id
    before = nomination_state(db, selected)
    result = set_nomination_order(db, selected, payload.franchise_ids, actor=active_username())
    _bump_auction(db, selected)
    _audit_mutation(
        db,
        stream="auction-nominations",
        action="reorder",
        league_id=selected,
        before=before,
        after=result,
    )
    return result


@app.post("/api/auction/nomination-order/randomize")
def randomize_auction_nomination_order(db: Db, league_id: str | None = None) -> dict[str, Any]:
    _require_admin(db)
    selected = league_id or runtime_settings(db).mfl_auction_league_id
    before = nomination_state(db, selected)
    result = shuffle_nomination_order(db, selected, actor=active_username())
    _bump_auction(db, selected)
    _audit_mutation(
        db,
        stream="auction-nominations",
        action="randomize",
        league_id=selected,
        before=before,
        after=result,
    )
    return result


@app.post("/api/auction/reset")
def reset_local_auction(db: Db, league_id: str | None = None) -> dict[str, Any]:
    _require_admin(db)
    selected = league_id or runtime_settings(db).mfl_auction_league_id
    _league_or_404(db, selected)
    before = [
        _purchase_json(db, item)
        for item in db.scalars(select(AuctionPurchase).where(AuctionPurchase.league_id == selected))
    ]
    reset_count = reset_auction(db, selected)
    nomination = reset_nomination_cursor(db, selected, actor=active_username())
    live = _auction_live(db, selected)
    live.is_live = False
    live.revision += 1
    live.updated_by = active_username()
    live.updated_at = datetime.now(UTC)
    interactive = _interactive_auction(db, selected)
    interactive.status = "idle"
    interactive.player_id = None
    interactive.nominating_franchise_id = None
    interactive.high_bid_franchise_id = None
    interactive.current_bid = None
    interactive.revision += 1
    interactive.nominated_by = None
    interactive.opened_at = None
    interactive.updated_at = datetime.now(UTC)
    db.commit()
    _audit_mutation(
        db,
        stream="auction-purchases",
        action="reset",
        league_id=selected,
        before=before,
        after=[],
        details={"reset_count": reset_count},
    )
    return {
        "reset_count": reset_count,
        "nomination": nomination,
        "live": {"is_live": False, "revision": live.revision},
    }


@app.post("/api/auction/purchases", status_code=201)
def purchase(payload: PurchaseCreate, background_tasks: BackgroundTasks, db: Db) -> dict[str, Any]:
    live = _auction_live(db, payload.league_id)
    staged = auction_stage_enabled(db, payload.league_id)
    if _interactive_auction(db, payload.league_id).enabled:
        raise HTTPException(
            409,
            detail={
                "code": "interactive_auction_required",
                "message": (
                    "Use the live nomination and bidding room while interactive auction is enabled"
                ),
            },
        )
    if not live.is_live and not staged:
        raise HTTPException(
            409,
            detail={
                "code": "auction_closed",
                "message": "The auction is closed until an admin enables staging or goes live",
            },
        )
    is_admin = is_current_admin(db)
    if not live.is_live or auction_rob_mode(db):
        _require_admin(db)
    elif not is_admin:
        league = _league_or_404(db, payload.league_id)
        current_user_franchise_id = _current_user_franchise_id(db, league)
        if not current_user_franchise_id:
            raise HTTPException(
                409,
                detail={
                    "code": "auction_franchise_required",
                    "message": "Link your MFL franchise under My Account before recording a win",
                },
            )
        if payload.franchise_id != current_user_franchise_id:
            raise HTTPException(
                403,
                detail={
                    "code": "auction_franchise_mismatch",
                    "message": "You can only record winning bids for your linked MFL franchise",
                },
            )
    result = add_purchase(db, payload)
    advance_nomination(db, payload.league_id, actor=active_username())
    _bump_auction(db, payload.league_id)
    created = _purchase_json(db, result)
    _audit_mutation(
        db,
        stream="auction-purchases",
        action="create",
        league_id=payload.league_id,
        entity_id=result.id,
        after=created,
    )
    _queue_round_power_refresh(background_tasks, db, payload.league_id, "auction")
    return created


@app.patch("/api/auction/purchases/{purchase_id}")
def patch_purchase(purchase_id: str, payload: PurchaseUpdate, db: Db) -> dict[str, Any]:
    _require_admin(db)
    current = db.get(AuctionPurchase, purchase_id)
    before = _purchase_json(db, current) if current else None
    result = update_purchase(db, purchase_id, payload)
    _bump_auction(db, result.league_id)
    updated = _purchase_json(db, result)
    _audit_mutation(
        db,
        stream="auction-purchases",
        action="update",
        league_id=result.league_id,
        entity_id=purchase_id,
        before=before,
        after=updated,
    )
    return updated


@app.delete("/api/auction/purchases/{purchase_id}", status_code=204)
def remove_purchase(purchase_id: str, db: Db) -> None:
    _require_admin(db)
    current = db.get(AuctionPurchase, purchase_id)
    before = _purchase_json(db, current) if current else None
    league_id = current.league_id if current else runtime_settings(db).mfl_auction_league_id
    delete_purchase(db, purchase_id)
    _bump_auction(db, league_id)
    _audit_mutation(
        db,
        stream="auction-purchases",
        action="delete",
        league_id=league_id,
        entity_id=purchase_id,
        before=before,
    )


@app.post("/api/auction/undo", status_code=204)
def undo_purchase(db: Db, league_id: str | None = None) -> None:
    _require_admin(db)
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_auction_league_id
    undo(db, selected)
    _bump_auction(db, selected)
    _audit_mutation(db, stream="auction-purchases", action="undo", league_id=selected)


@app.post("/api/auction/redo", status_code=204)
def redo_purchase(db: Db, league_id: str | None = None) -> None:
    _require_admin(db)
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_auction_league_id
    redo(db, selected)
    _bump_auction(db, selected)
    _audit_mutation(db, stream="auction-purchases", action="redo", league_id=selected)


@app.get("/api/auction/export.csv")
def download_csv(db: Db, league_id: str | None = None) -> FileResponse:
    settings = runtime_settings(db)
    path = export_csv(db, league_id or settings.mfl_auction_league_id, settings.export_directory)
    return FileResponse(path, media_type="text/csv", filename=path.name)


@app.get("/api/auction/export.xml")
def download_xml(db: Db, league_id: str | None = None) -> FileResponse:
    settings = runtime_settings(db)
    path = export_xml(db, league_id or settings.mfl_auction_league_id, settings.export_directory)
    return FileResponse(path, media_type="application/xml", filename=path.name)


@app.get("/api/auction/import-preview")
def import_preview(db: Db, league_id: str | None = None) -> dict[str, Any]:
    _require_admin(db)
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_auction_league_id
    _league_or_404(db, selected)
    _, xml, count = build_xml(db, selected)
    return {
        "league_id": selected,
        "purchase_count": count,
        "xml": xml.decode(),
        "confirmation_token": hashlib.sha256(xml).hexdigest(),
        "imports_enabled": settings.mfl_enable_imports,
        "reauthentication_username": active_username(),
        "warning": "OVERWRITE without CLEAR can create inconsistent franchise funds.",
    }


@app.post("/api/auction/push-to-mfl")
async def push_to_mfl(
    request: Request,
    payload: ImportConfirmation,
    db: Db,
) -> dict[str, str]:
    _require_admin(db)
    _league_or_404(db, payload.league_id)
    settings = runtime_settings(db)
    username = active_username()
    if not settings.mfl_enable_imports:
        _audit_mutation(
            db,
            stream="mfl-imports",
            action="auction_results_import_failed",
            league_id=payload.league_id,
            details={"username": username, "reason": "imports_disabled"},
        )
        raise HTTPException(
            403,
            detail={
                "code": "commissioner_disabled",
                "message": "Commissioner imports are disabled in Admin settings",
            },
        )
    _, xml, _ = build_xml(db, payload.league_id)
    if payload.confirmation_token != hashlib.sha256(xml).hexdigest():
        raise HTTPException(
            409,
            detail={
                "code": "preview_stale",
                "message": "Auction changed after preview; preview again before confirming",
            },
        )
    client_ip = request.client.host if request.client else "unknown"
    rate_key = login_rate_key(client_ip, username)
    if not login_allowed(rate_key):
        raise HTTPException(
            429,
            detail={
                "code": "mfl_reauthentication_rate_limited",
                "message": "Too many MFL sign-in attempts. Wait ten minutes and try again.",
            },
        )
    password = payload.password.get_secret_value()
    try:
        async with MFLClient(settings) as client:
            await client.authenticate(username, password)
            await client.export("auctionResults", league_id=payload.league_id, db=db, force=True)
            response = await client.import_auction_results(
                payload.league_id, xml.decode(), clear=payload.clear, overwrite=payload.overwrite
            )
    except MFLAuthenticationError as exc:
        record_login_failure(rate_key)
        _audit_mutation(
            db,
            stream="mfl-imports",
            action="auction_results_import_failed",
            league_id=payload.league_id,
            details={"username": username, "error_type": type(exc).__name__, "message": str(exc)},
        )
        raise HTTPException(
            403,
            detail={
                "code": "mfl_import_authentication_failed",
                "message": str(exc),
            },
        ) from exc
    except MFLError as exc:
        _audit_mutation(
            db,
            stream="mfl-imports",
            action="auction_results_import_failed",
            league_id=payload.league_id,
            details={"username": username, "error_type": type(exc).__name__, "message": str(exc)},
        )
        raise HTTPException(
            502,
            detail={
                "code": "mfl_import_failed",
                "message": str(exc),
            },
        ) from exc
    finally:
        password = ""
    clear_login_failures(rate_key)
    db.add(
        ImportRecord(league_id=payload.league_id, payload_xml=xml.decode(), response_text=response)
    )
    db.commit()
    _audit_mutation(
        db,
        stream="mfl-imports",
        action="auction_results_import",
        league_id=payload.league_id,
        details={
            "username": username,
            "clear": payload.clear,
            "overwrite": payload.overwrite,
            "confirmation_token": payload.confirmation_token,
            "response": response,
        },
    )
    return {"response": response}


@app.get("/api/auction/draft-results-import-preview")
def draft_results_import_preview(db: Db, league_id: str | None = None) -> dict[str, Any]:
    _require_admin(db)
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_auction_league_id
    league = _league_or_404(db, selected)
    try:
        prepared = prepare_draft_results_import(
            db,
            league.id,
            export_directory=settings.export_directory,
            audit_directory=settings.audit_directory,
        )
    except (OSError, ValueError, ImportValidationError) as exc:
        _audit_mutation(
            db,
            stream="mfl-imports",
            action="draft_results_preview_failed",
            league_id=league.id,
            details={
                "username": active_username(),
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
        )
        raise HTTPException(
            409,
            detail={"code": "draft_results_preview_failed", "message": str(exc)},
        ) from exc
    confirmation_text = f"IMPORT DRAFT RESULTS {league.id}"
    _audit_mutation(
        db,
        stream="mfl-imports",
        action="draft_results_preview",
        league_id=league.id,
        details={
            "username": active_username(),
            "purchase_count": len(prepared.plan.picks),
            "expected_capacity": prepared.expected_capacity,
            "ready": prepared.ready,
            "confirmation_token": prepared.confirmation_token,
        },
    )
    return {
        "league_id": league.id,
        "purchase_count": len(prepared.plan.picks),
        "round_count": max((pick.round for pick in prepared.plan.picks), default=0),
        "franchise_count": len(prepared.franchise_counts),
        "expected_capacity": prepared.expected_capacity,
        "franchise_counts": prepared.franchise_counts,
        "warnings": prepared.plan.warnings,
        "readiness_errors": list(prepared.readiness_errors),
        "ready": prepared.ready,
        "xml": prepared.artifacts["xml"].read_text(encoding="utf-8"),
        "confirmation_token": prepared.confirmation_token,
        "confirmation_text": confirmation_text,
        "imports_enabled": settings.mfl_enable_imports,
        "reauthentication_username": active_username(),
    }


@app.post("/api/auction/push-as-draft-results")
async def push_as_draft_results(
    request: Request,
    payload: DraftResultsImportConfirmation,
    db: Db,
) -> dict[str, Any]:
    _require_admin(db)
    league = _league_or_404(db, payload.league_id)
    settings = runtime_settings(db)
    username = active_username()

    def audit_failure(reason: str, exc: Exception | None = None) -> None:
        details: dict[str, Any] = {"username": username, "reason": reason}
        if exc is not None:
            details.update({"error_type": type(exc).__name__, "message": str(exc)})
        _audit_mutation(
            db,
            stream="mfl-imports",
            action="draft_results_import_failed",
            league_id=league.id,
            details=details,
        )

    if not settings.mfl_enable_imports:
        audit_failure("imports_disabled")
        raise HTTPException(
            403,
            detail={
                "code": "commissioner_disabled",
                "message": "Commissioner imports are disabled in Admin settings",
            },
        )
    expected_confirmation = f"IMPORT DRAFT RESULTS {league.id}"
    if not hmac.compare_digest(payload.confirmation_text.strip(), expected_confirmation):
        audit_failure("confirmation_mismatch")
        raise HTTPException(
            409,
            detail={
                "code": "confirmation_mismatch",
                "message": f"Type {expected_confirmation} exactly before importing",
            },
        )
    try:
        prepared = prepare_draft_results_import(
            db,
            league.id,
            export_directory=settings.export_directory,
            audit_directory=settings.audit_directory,
        )
    except (OSError, ValueError, ImportValidationError) as exc:
        audit_failure("validation_failed", exc)
        raise HTTPException(
            409,
            detail={"code": "draft_results_validation_failed", "message": str(exc)},
        ) from exc
    if payload.confirmation_token != prepared.confirmation_token:
        audit_failure("preview_stale")
        raise HTTPException(
            409,
            detail={
                "code": "preview_stale",
                "message": "Auction changed after preview; preview again before confirming",
            },
        )
    if not prepared.ready:
        audit_failure("auction_incomplete")
        raise HTTPException(
            409,
            detail={
                "code": "auction_incomplete",
                "message": "; ".join(prepared.readiness_errors),
            },
        )

    client_ip = request.client.host if request.client else "unknown"
    rate_key = login_rate_key(client_ip, username)
    if not login_allowed(rate_key):
        audit_failure("reauthentication_rate_limited")
        raise HTTPException(
            429,
            detail={
                "code": "mfl_reauthentication_rate_limited",
                "message": "Too many MFL sign-in attempts. Wait ten minutes and try again.",
            },
        )

    password = payload.password.get_secret_value()
    try:
        receipt = await asyncio.to_thread(
            send_plan,
            prepared.plan,
            prepared.artifacts,
            prepared.output_directory,
            username=username,
            password=password,
            timeout=30,
            assume_yes=True,
        )
    except MFLImportError as exc:
        record_login_failure(rate_key)
        audit_failure("mfl_rejected_import", exc)
        message = str(exc)
        lowered = message.casefold()
        permission_error = "commissioner access" in lowered or "authorization" in lowered
        raise HTTPException(
            403 if permission_error else 502,
            detail={
                "code": (
                    "mfl_commissioner_access_required"
                    if permission_error
                    else "mfl_draft_results_import_failed"
                ),
                "message": message,
            },
        ) from exc
    except OSError as exc:
        audit_failure("artifact_failure", exc)
        raise HTTPException(
            500,
            detail={"code": "import_artifact_failed", "message": str(exc)},
        ) from exc
    finally:
        password = ""

    clear_login_failures(rate_key)
    xml = prepared.artifacts["xml"].read_text(encoding="utf-8")
    response_path = Path(str(receipt["files"]["response"]))
    response_text = response_path.read_text(encoding="utf-8", errors="replace")
    db.add(ImportRecord(league_id=league.id, payload_xml=xml, response_text=response_text))
    db.commit()
    _audit_mutation(
        db,
        stream="mfl-imports",
        action="draft_results_import",
        league_id=league.id,
        details={
            "username": username,
            "verification": receipt["verification"],
            "expected_picks": receipt["expected_picks"],
            "observed_export_picks": receipt["observed_export_picks"],
            "league_host": receipt["league_host"],
            "source_sha256": receipt["source_sha256"],
            "xml_sha256": receipt["xml_sha256"],
            "receipt_file": Path(str(receipt["receipt"])).name,
        },
    )
    return {
        "verification": receipt["verification"],
        "expected_picks": receipt["expected_picks"],
        "observed_export_picks": receipt["observed_export_picks"],
        "league_host": receipt["league_host"],
        "mfl_response": response_text,
        "message": (
            "All draft results were verified on MFL."
            if receipt["verification"] == "matched"
            else "MFL returned no import error; its draft-results export is still updating."
        ),
    }
