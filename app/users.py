from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    DataSource,
    UserAccount,
    UserLeagueSetting,
    UserMFLMembership,
    UserSourceSetting,
)
from app.user_context import (
    active_league_ids,
    active_username,
    normalize_username,
    source_visible_to_user,
)

DEFAULT_AUCTION_STRATEGY = "balanced"
ROB_MODE_SETTING_KEY = "auction_rob_mode"
AUCTION_STAGE_SETTING_PREFIX = "auction_stage:"
DRAFT_MODE_SETTING_PREFIX = "draft_mode:"
DRAFT_MODES = {"companion", "local"}
AUCTION_STRATEGIES: dict[str, dict[str, Any]] = {
    "balanced": {
        "name": "Balanced value",
        "description": "Spread budget across starters and react to value as it appears.",
        "priority_order": ["WR", "RB", "QB", "TE", "DEF"],
        "star_emphasis": Decimal("1.08"),
        "depth_emphasis": Decimal("1.00"),
    },
    "elite_wr": {
        "name": "Elite WR foundation",
        "description": "Prioritize star WR, then star QB and RB before filling depth.",
        "priority_order": ["WR", "QB", "RB", "TE", "DEF"],
        "star_emphasis": Decimal("1.28"),
        "depth_emphasis": Decimal("0.94"),
    },
    "hero_rb": {
        "name": "Hero RB",
        "description": "Pay for one premium RB, then emphasize WR depth and value quarterbacks.",
        "priority_order": ["RB", "WR", "TE", "QB", "DEF"],
        "star_emphasis": Decimal("1.22"),
        "depth_emphasis": Decimal("1.02"),
    },
    "elite_qb": {
        "name": "Elite QB anchor",
        "description": "Secure a difference-making quarterback, then build balanced skill depth.",
        "priority_order": ["QB", "WR", "RB", "TE", "DEF"],
        "star_emphasis": Decimal("1.20"),
        "depth_emphasis": Decimal("1.00"),
    },
    "depth_first": {
        "name": "Depth and flexibility",
        "description": "Flatten prices and preserve budget for a deep, adaptable roster.",
        "priority_order": ["WR", "RB", "TE", "QB", "DEF"],
        "star_emphasis": Decimal("0.94"),
        "depth_emphasis": Decimal("1.12"),
    },
    "stars_scrubs": {
        "name": "Stars and depth bargains",
        "description": "Concentrate money in elite players and reserve minimum bids for depth.",
        "priority_order": ["WR", "RB", "QB", "TE", "DEF"],
        "star_emphasis": Decimal("1.38"),
        "depth_emphasis": Decimal("0.82"),
    },
}


def bootstrap_user(
    db: Session,
    username: str,
    *,
    display_name: str | None = None,
    admin_usernames: set[str] | None = None,
) -> UserAccount:
    normalized = normalize_username(username)
    account = db.get(UserAccount, normalized)
    admins = {normalize_username(item) for item in (admin_usernames or {"wilsonmw"})}
    if account is None:
        account = UserAccount(
            username=normalized,
            display_name=display_name or username.strip(),
            is_admin=normalized in admins,
        )
        db.add(account)
    else:
        account.display_name = display_name or account.display_name
        if normalized in admins:
            account.is_admin = True
    db.commit()
    return account


def record_login(db: Session, username: str, admin_usernames: set[str]) -> UserAccount:
    account = bootstrap_user(db, username, admin_usernames=admin_usernames)
    account.last_login_at = datetime.now(UTC)
    db.commit()
    return account


def save_mfl_memberships(
    db: Session,
    username: str,
    season: int,
    memberships: list[dict[str, str | None]],
) -> None:
    normalized = normalize_username(username)
    for row in memberships:
        league_id = str(row["league_id"])
        membership = db.scalar(
            select(UserMFLMembership).where(
                UserMFLMembership.username == normalized,
                UserMFLMembership.season == season,
                UserMFLMembership.league_id == league_id,
            )
        )
        if membership is None:
            membership = UserMFLMembership(username=normalized, season=season, league_id=league_id)
            db.add(membership)
        membership.league_name = row.get("league_name")
        membership.franchise_id = row.get("franchise_id")
        membership.source_url = row.get("source_url")
        membership.discovered_at = datetime.now(UTC)
        if membership.franchise_id:
            connected = db.scalar(
                select(UserLeagueSetting).where(
                    UserLeagueSetting.username == normalized,
                    UserLeagueSetting.league_id == league_id,
                )
            )
            if connected is None:
                connected = UserLeagueSetting(
                    username=normalized,
                    league_id=league_id,
                    auction_strategy_json={"template": DEFAULT_AUCTION_STRATEGY},
                )
                db.add(connected)
            if not connected.franchise_id:
                connected.franchise_id = membership.franchise_id
    db.commit()


def mfl_memberships_for_user(db: Session, season: int) -> list[UserMFLMembership]:
    return list(
        db.scalars(
            select(UserMFLMembership)
            .where(
                UserMFLMembership.username == active_username(),
                UserMFLMembership.season == season,
            )
            .order_by(UserMFLMembership.league_name, UserMFLMembership.league_id)
        )
    )


def authorized_league_ids(db: Session) -> set[str] | None:
    """Return the current user's MFL leagues, or None when auth is disabled."""
    session_leagues = active_league_ids()
    if session_leagues is not None:
        return session_leagues
    rows = list(
        db.scalars(
            select(UserMFLMembership.league_id).where(
                UserMFLMembership.username == active_username()
            )
        )
    )
    if rows:
        return set(rows)
    return None


def current_account(db: Session) -> UserAccount:
    return bootstrap_user(db, active_username())


def is_current_admin(db: Session) -> bool:
    return current_account(db).is_admin


def auction_rob_mode(db: Session) -> bool:
    """Return whether auction purchases are restricted to administrators."""
    setting = db.get(AppSetting, ROB_MODE_SETTING_KEY)
    if setting is None:
        return True
    return setting.value.strip().lower() not in {"0", "false", "off", "no"}


def save_auction_rob_mode(db: Session, enabled: bool) -> bool:
    setting = db.get(AppSetting, ROB_MODE_SETTING_KEY)
    if setting is None:
        setting = AppSetting(key=ROB_MODE_SETTING_KEY, value="true")
        db.add(setting)
    setting.value = "true" if enabled else "false"
    setting.updated_at = datetime.now(UTC)
    db.commit()
    return enabled


def auction_stage_enabled(db: Session, league_id: str) -> bool:
    setting = db.get(AppSetting, f"{AUCTION_STAGE_SETTING_PREFIX}{league_id}")
    if setting is None:
        return False
    return setting.value.strip().lower() in {"1", "true", "on", "yes"}


def save_auction_stage(db: Session, league_id: str, enabled: bool) -> bool:
    key = f"{AUCTION_STAGE_SETTING_PREFIX}{league_id}"
    setting = db.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value="false")
        db.add(setting)
    setting.value = "true" if enabled else "false"
    setting.updated_at = datetime.now(UTC)
    db.commit()
    return enabled


def draft_mode(db: Session, league_id: str) -> str:
    """Return the shared real-draft source for a league."""
    setting = db.get(AppSetting, f"{DRAFT_MODE_SETTING_PREFIX}{league_id}")
    if setting is None or setting.value not in DRAFT_MODES:
        return "companion"
    return setting.value


def save_draft_mode(db: Session, league_id: str, mode: str) -> str:
    if mode not in DRAFT_MODES:
        raise ValueError("Draft mode must be companion or local")
    key = f"{DRAFT_MODE_SETTING_PREFIX}{league_id}"
    setting = db.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value=mode)
        db.add(setting)
    else:
        setting.value = mode
        setting.updated_at = datetime.now(UTC)
    db.commit()
    return mode


def effective_source_settings(db: Session) -> dict[str, dict[str, Any]]:
    username = active_username()
    saved = {
        item.source_id: item
        for item in db.scalars(
            select(UserSourceSetting).where(UserSourceSetting.username == username)
        )
    }
    return {
        source.id: {
            "enabled": saved[source.id].enabled if source.id in saved else True,
            "weight": Decimal(saved[source.id].weight)
            if source.id in saved
            else Decimal(source.weight),
        }
        for source in db.scalars(select(DataSource))
        if source_visible_to_user(source.id, username)
    }


def save_source_setting(
    db: Session, source_id: str, *, enabled: bool, weight: Decimal
) -> UserSourceSetting:
    username = active_username()
    if not source_visible_to_user(source_id, username):
        raise ValueError("Ranking source is not available to this account")
    setting = db.scalar(
        select(UserSourceSetting).where(
            UserSourceSetting.username == username,
            UserSourceSetting.source_id == source_id,
        )
    )
    if setting is None:
        setting = UserSourceSetting(username=username, source_id=source_id)
        db.add(setting)
    setting.enabled = enabled
    setting.weight = weight
    setting.updated_at = datetime.now(UTC)
    db.commit()
    return setting


def reset_source_settings(db: Session) -> int:
    username = active_username()
    count = db.scalar(
        select(func.count())
        .select_from(UserSourceSetting)
        .where(UserSourceSetting.username == username)
    )
    db.execute(delete(UserSourceSetting).where(UserSourceSetting.username == username))
    db.commit()
    return int(count or 0)


def league_setting(db: Session, league_id: str) -> UserLeagueSetting:
    username = active_username()
    setting = db.scalar(
        select(UserLeagueSetting).where(
            UserLeagueSetting.username == username,
            UserLeagueSetting.league_id == league_id,
        )
    )
    if setting is None:
        setting = UserLeagueSetting(
            username=username,
            league_id=league_id,
            auction_strategy_json={"template": DEFAULT_AUCTION_STRATEGY},
        )
        db.add(setting)
        db.commit()
    return setting


def effective_auction_strategy(db: Session, league_id: str) -> dict[str, Any]:
    saved = league_setting(db, league_id).auction_strategy_json or {}
    template_id = str(saved.get("template") or DEFAULT_AUCTION_STRATEGY)
    template = AUCTION_STRATEGIES.get(template_id, AUCTION_STRATEGIES[DEFAULT_AUCTION_STRATEGY])
    priority = saved.get("priority_order") or template["priority_order"]
    return {
        **template,
        "template": template_id,
        "priority_order": [str(item).upper() for item in priority],
        "star_emphasis": Decimal(str(saved.get("star_emphasis", template["star_emphasis"]))),
        "depth_emphasis": Decimal(str(saved.get("depth_emphasis", template["depth_emphasis"]))),
    }


def strategy_json(value: dict[str, Any]) -> dict[str, Any]:
    return {key: str(item) if isinstance(item, Decimal) else item for key, item in value.items()}
