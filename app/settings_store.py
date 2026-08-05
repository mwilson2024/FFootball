from datetime import UTC, datetime

import keyring
from keyring.errors import KeyringError, PasswordDeleteError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AppSetting
from app.schemas import SetupUpdate

KEYRING_SERVICE = "MFLDraftManager"
SECRET_NAMES = {
    "keeper_api_key": "mfl_keeper_api_key",
    "auction_api_key": "mfl_auction_api_key",
    "fantasypros_api_key": "fantasypros_api_key",
}


class CredentialStoreError(RuntimeError):
    pass


def _db_value(db: Session, key: str) -> str | None:
    item = db.get(AppSetting, key)
    return item.value if item else None


def _secret(name: str) -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, SECRET_NAMES[name])
    except KeyringError:
        return None


def runtime_settings(db: Session) -> Settings:
    base = get_settings()
    stored_season = _db_value(db, "mfl_season")
    stored_keeper = _db_value(db, "mfl_keeper_league_id") or ""
    stored_auction = _db_value(db, "mfl_auction_league_id") or ""
    stored_agent = _db_value(db, "mfl_user_agent")
    return base.model_copy(
        update={
            "mfl_season": int(stored_season) if stored_season else base.mfl_season,
            "mfl_keeper_league_id": base.mfl_keeper_league_id or stored_keeper,
            "mfl_auction_league_id": base.mfl_auction_league_id or stored_auction,
            "mfl_keeper_api_key": base.mfl_keeper_api_key or _secret("keeper_api_key") or "",
            "mfl_auction_api_key": base.mfl_auction_api_key or _secret("auction_api_key") or "",
            "mfl_user_agent": stored_agent or base.mfl_user_agent,
            "fantasypros_api_key": base.fantasypros_api_key or _secret("fantasypros_api_key") or "",
        }
    )


def _set_db_value(db: Session, key: str, value: str) -> None:
    item = db.get(AppSetting, key)
    if item is None:
        item = AppSetting(key=key, value=value)
        db.add(item)
    else:
        item.value = value
        item.updated_at = datetime.now(UTC)


def _set_secret(name: str, value: str | None) -> None:
    if value is None:
        return
    try:
        if value:
            keyring.set_password(KEYRING_SERVICE, SECRET_NAMES[name], value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, SECRET_NAMES[name])
            except PasswordDeleteError:
                pass
    except KeyringError as exc:
        raise CredentialStoreError(
            "The operating-system credential store is unavailable; use the .env fallback."
        ) from exc


def save_setup(db: Session, payload: SetupUpdate) -> Settings:
    _set_db_value(db, "mfl_season", str(payload.season))
    _set_db_value(db, "mfl_keeper_league_id", payload.keeper_league_id)
    _set_db_value(db, "mfl_auction_league_id", payload.auction_league_id)
    _set_db_value(db, "mfl_user_agent", payload.user_agent)
    _set_secret("keeper_api_key", payload.keeper_api_key)
    _set_secret("auction_api_key", payload.auction_api_key)
    _set_secret("fantasypros_api_key", payload.fantasypros_api_key)
    db.commit()
    return runtime_settings(db)


def setup_status(db: Session) -> dict[str, object]:
    settings = runtime_settings(db)
    return {
        "season": settings.mfl_season,
        "keeper_league_id": settings.mfl_keeper_league_id,
        "auction_league_id": settings.mfl_auction_league_id,
        "keeper_api_key_configured": bool(settings.mfl_keeper_api_key),
        "auction_api_key_configured": bool(settings.mfl_auction_api_key),
        "fantasypros_api_key_configured": bool(settings.fantasypros_api_key),
        "commissioner_configured": settings.commissioner_configured,
        "user_agent": settings.mfl_user_agent,
        "complete": bool(settings.mfl_keeper_league_id and settings.mfl_auction_league_id),
    }
