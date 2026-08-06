from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import quote, urlparse

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.assistant import ask_assistant
from app.auction import (
    AuctionValidationError,
    add_purchase,
    delete_purchase,
    franchise_budget,
    redo,
    undo,
    update_purchase,
)
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
from app.automation import daily_sync_loop, stop_daily_sync
from app.catalog import (
    draftable_consensus,
    player_detail,
    player_filters,
    query_players,
    roster_overview,
)
from app.config import get_settings
from app.consensus import create_consensus_snapshot, parse_ranking_csv
from app.db import get_db, init_db
from app.draft import (
    DraftValidationError,
    add_pick,
    apply_reconciliation,
    draft_state,
    export_draft_csv,
    pick_json,
    recommendations,
    reconcile_preview,
    remove_pick,
    undo_draft,
    update_pick,
)
from app.exports import build_xml, export_csv, export_xml
from app.mfl import MFLAuthenticationError, MFLClient, MFLError
from app.models import (
    AuctionLiveState,
    AuctionPurchase,
    DataSource,
    Franchise,
    ImportRecord,
    KeeperSelection,
    League,
    LeagueType,
    MFLSnapshot,
    Player,
    PlayerIdentity,
    SyncWarning,
    UserMFLMembership,
    UserPlayerPreference,
)
from app.schemas import (
    AssistantRequest,
    AuctionLiveUpdate,
    DraftPickCreate,
    DraftPickUpdate,
    IdentityUpdate,
    ImportConfirmation,
    KeeperCreate,
    LeagueConnect,
    MFLConnectionTest,
    PreferenceUpdate,
    PurchaseCreate,
    PurchaseUpdate,
    SetupUpdate,
    SourcePreview,
    SourceUpdate,
    UserLeagueSettingUpdate,
    WarningResolve,
)
from app.settings_store import CredentialStoreError, runtime_settings, save_setup, setup_status
from app.sources import (
    initialize_sources,
    source_json,
    sync_fantasypros,
    sync_gng,
    sync_nflverse,
    sync_sleeper,
)
from app.sync import RULE_DESCRIPTIONS, record_sync_warnings, sync_configured, sync_league
from app.user_context import (
    active_username,
    reset_active_league_ids,
    reset_active_username,
    set_active_league_ids,
    set_active_username,
)
from app.users import (
    AUCTION_STRATEGIES,
    authorized_league_ids,
    bootstrap_user,
    current_account,
    effective_source_settings,
    is_current_admin,
    league_setting,
    mfl_memberships_for_user,
    record_login,
    save_mfl_memberships,
    save_source_setting,
    strategy_json,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")
SESSION_SIGNING_SECRET = ""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global SESSION_SIGNING_SECRET
    settings = get_settings()
    if settings.auth_required:
        SESSION_SIGNING_SECRET = resolve_session_secret(settings)
    init_db()
    from app.db import SessionLocal

    with SessionLocal() as db:
        initialize_sources(db)
        for username in settings.admin_username_set or {"wilsonmw"}:
            bootstrap_user(db, username, admin_usernames=settings.admin_username_set)
        for league_item in db.scalars(select(League)):
            if league_item.warnings_json and not db.scalar(
                select(SyncWarning.id).where(SyncWarning.league_id == league_item.id).limit(1)
            ):
                record_sync_warnings(db, league_item.id, league_item.warnings_json)
        db.commit()
        runtime_settings(db).export_directory.mkdir(parents=True, exist_ok=True)
    auto_sync_task: asyncio.Task[None] | None = None
    if settings.auto_sync_enabled:
        auto_sync_task = asyncio.create_task(daily_sync_loop(settings), name="daily-mfl-sync")
    try:
        yield
    finally:
        if auto_sync_task is not None:
            await stop_daily_sync(auto_sync_task)


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
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
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


def _purchase_json(purchase: AuctionPurchase) -> dict[str, Any]:
    return {
        "id": purchase.id,
        "league_id": purchase.league_id,
        "franchise_id": purchase.franchise_id,
        "player_id": purchase.player_id,
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
    }


def _require_admin(db: Session) -> None:
    if not is_current_admin(db):
        raise HTTPException(
            403,
            detail={"code": "admin_required", "message": "An administrator must do that"},
        )


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
    return state


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


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request, db: Db) -> Any:
    return templates.TemplateResponse(request, "sources.html", _page_context(db, "Data sources"))


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


@app.get("/auction", response_class=HTMLResponse)
def auction_room(request: Request, db: Db) -> Any:
    settings = runtime_settings(db)
    context = _page_context(db, "Auction room", settings.mfl_auction_league_id)
    context["league"] = db.scalar(select(League).where(League.id == settings.mfl_auction_league_id))
    return templates.TemplateResponse(request, "auction.html", context)


@app.get("/keepers", response_class=HTMLResponse)
def keeper_room(request: Request, db: Db) -> Any:
    settings = runtime_settings(db)
    context = _page_context(db, "Keeper room", settings.mfl_keeper_league_id)
    context["league"] = db.scalar(select(League).where(League.id == settings.mfl_keeper_league_id))
    return templates.TemplateResponse(request, "keepers.html", context)


@app.get("/api/setup/status")
def api_setup_status(db: Db) -> dict[str, object]:
    return setup_status(db)


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
    return [
        {
            **source_json(item),
            "enabled": effective[item.id]["enabled"],
            "weight": str(effective[item.id]["weight"]),
        }
        for item in db.scalars(select(DataSource).order_by(DataSource.name))
    ]


@app.put("/api/setup/sources/{source_id}")
def update_source(source_id: str, payload: SourceUpdate, db: Db) -> dict[str, Any]:
    source = db.get(DataSource, source_id)
    if source is None:
        raise HTTPException(404, detail={"code": "source_not_found", "message": "Source not found"})
    setting = save_source_setting(db, source_id, enabled=payload.enabled, weight=payload.weight)
    return {**source_json(source), "enabled": setting.enabled, "weight": str(setting.weight)}


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
    if source is None:
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
        if source_id in {"gng", "fantasypros", "fantasypros_dynasty"}:
            settings = runtime_settings(db)
            leagues_to_sync = list(
                db.scalars(select(League).order_by(League.league_type, League.id))
            )
            if source_id == "fantasypros_dynasty":
                leagues_to_sync = [
                    item for item in leagues_to_sync if item.league_type == LeagueType.KEEPER
                ]
            results: list[dict[str, Any]] = []
            for index, league_item in enumerate(leagues_to_sync):
                if source_id == "gng":
                    synced = await sync_gng(
                        db, league_item.id, league_item.scoring_rules_json or {}
                    )
                else:
                    if index:
                        await asyncio.sleep(1.05)
                    synced = await sync_fantasypros(
                        db,
                        league_item.id,
                        league_item.season,
                        league_item.scoring_rules_json or {},
                        settings.fantasypros_api_key,
                        source_id=source_id,
                        ranking_type=("DYNASTY" if source_id == "fantasypros_dynasty" else "DRAFT"),
                    )
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
    return [_league_json(item) for item in db.scalars(select(League).order_by(League.name))]


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
    return cast(dict[str, Any], team)


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


@app.get("/api/draft/state")
def api_draft_state(league_id: str, db: Db) -> dict[str, Any]:
    return draft_state(db, league_id)


@app.post("/api/draft/picks", status_code=201)
def create_draft_pick(payload: DraftPickCreate, db: Db) -> dict[str, Any]:
    return pick_json(db, add_pick(db, payload))


@app.patch("/api/draft/picks/{pick_id}")
def patch_draft_pick(pick_id: str, payload: DraftPickUpdate, db: Db) -> dict[str, Any]:
    return pick_json(db, update_pick(db, pick_id, payload))


@app.delete("/api/draft/picks/{pick_id}", status_code=204)
def delete_draft_pick(pick_id: str, db: Db) -> None:
    remove_pick(db, pick_id)


@app.post("/api/draft/undo", status_code=204)
def api_draft_undo(league_id: str, db: Db) -> None:
    undo_draft(db, league_id)


@app.get("/api/draft/recommendations")
def api_recommendations(
    league_id: str, db: Db, franchise_id: str | None = None, limit: int = Query(12, ge=1, le=50)
) -> list[dict[str, Any]]:
    return recommendations(db, league_id, franchise_id, limit)


@app.post("/api/draft/reconcile")
async def reconcile_draft(
    league_id: str,
    db: Db,
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
def assistant_status() -> dict[str, Any]:
    settings = get_settings()
    return {"enabled": bool(settings.openai_api_key), "model": settings.openai_model}


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
    return {
        "league": _league_json(league),
        "franchises": [
            franchise_budget(db, league, franchise)
            for franchise in db.scalars(
                select(Franchise).where(Franchise.league_id == selected).order_by(Franchise.name)
            )
        ],
        "purchases": [
            _purchase_json(item)
            for item in db.scalars(
                select(AuctionPurchase)
                .where(AuctionPurchase.league_id == selected, AuctionPurchase.active.is_(True))
                .order_by(AuctionPurchase.purchase_order.desc())
            )
        ],
        "synced_at": league.synced_at,
        "stale": not league.synced_at,
        "live": {
            "is_live": live.is_live,
            "revision": live.revision,
            "updated_at": live.updated_at,
            "updated_by": live.updated_by,
        },
        "is_admin": is_current_admin(db),
    }


@app.put("/api/auction/live")
def set_auction_live(
    payload: AuctionLiveUpdate, db: Db, league_id: str | None = None
) -> dict[str, Any]:
    _require_admin(db)
    selected = league_id or runtime_settings(db).mfl_auction_league_id
    _league_or_404(db, selected)
    state = _auction_live(db, selected)
    state.is_live = payload.is_live
    state.revision += 1
    state.updated_by = active_username()
    state.updated_at = datetime.now(UTC)
    db.commit()
    return {"is_live": state.is_live, "revision": state.revision}


@app.post("/api/auction/purchases", status_code=201)
def purchase(payload: PurchaseCreate, db: Db) -> dict[str, Any]:
    _require_admin(db)
    result = add_purchase(db, payload)
    _bump_auction(db, payload.league_id)
    return _purchase_json(result)


@app.patch("/api/auction/purchases/{purchase_id}")
def patch_purchase(purchase_id: str, payload: PurchaseUpdate, db: Db) -> dict[str, Any]:
    _require_admin(db)
    result = update_purchase(db, purchase_id, payload)
    _bump_auction(db, result.league_id)
    return _purchase_json(result)


@app.delete("/api/auction/purchases/{purchase_id}", status_code=204)
def remove_purchase(purchase_id: str, db: Db) -> None:
    _require_admin(db)
    current = db.get(AuctionPurchase, purchase_id)
    league_id = current.league_id if current else runtime_settings(db).mfl_auction_league_id
    delete_purchase(db, purchase_id)
    _bump_auction(db, league_id)


@app.post("/api/auction/undo", status_code=204)
def undo_purchase(db: Db, league_id: str | None = None) -> None:
    _require_admin(db)
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_auction_league_id
    undo(db, selected)
    _bump_auction(db, selected)


@app.post("/api/auction/redo", status_code=204)
def redo_purchase(db: Db, league_id: str | None = None) -> None:
    _require_admin(db)
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_auction_league_id
    redo(db, selected)
    _bump_auction(db, selected)


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
    settings = runtime_settings(db)
    selected = league_id or settings.mfl_auction_league_id
    _, xml, count = build_xml(db, selected)
    return {
        "league_id": selected,
        "purchase_count": count,
        "xml": xml.decode(),
        "confirmation_token": hashlib.sha256(xml).hexdigest(),
        "imports_enabled": settings.commissioner_configured,
        "warning": "OVERWRITE without CLEAR can create inconsistent franchise funds.",
    }


@app.post("/api/auction/push-to-mfl")
async def push_to_mfl(payload: ImportConfirmation, db: Db) -> dict[str, str]:
    _require_admin(db)
    settings = runtime_settings(db)
    if not settings.commissioner_configured:
        raise HTTPException(
            403,
            detail={
                "code": "commissioner_disabled",
                "message": "Commissioner import is disabled or credentials are missing",
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
    async with MFLClient(settings) as client:
        await client.export("auctionResults", league_id=payload.league_id, db=db, force=True)
        response = await client.import_auction_results(
            payload.league_id, xml.decode(), clear=payload.clear, overwrite=payload.overwrite
        )
    db.add(
        ImportRecord(league_id=payload.league_id, payload_xml=xml.decode(), response_text=response)
    )
    db.commit()
    return {"response": response}
