from __future__ import annotations

import hashlib
import json
import secrets
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exports import export_csv, export_rows
from app.models import Franchise
from scripts.import_auction_csv_as_draft import (
    ImportPlan,
    build_draft_xml,
    load_plan,
    validate_draft_xml,
    write_artifacts,
)


@dataclass(frozen=True)
class PreparedDraftResultsImport:
    plan: ImportPlan
    artifacts: dict[str, Path]
    output_directory: Path
    confirmation_token: str
    expected_capacity: int
    franchise_counts: dict[str, int]
    readiness_errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.readiness_errors


def _state_token(
    league_id: str,
    season: int,
    rows: list[dict[str, str]],
) -> str:
    canonical = json.dumps(
        {
            "league_id": str(league_id),
            "season": season,
            "rows": rows,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def prepare_draft_results_import(
    db: Session,
    league_id: str,
    *,
    export_directory: Path,
    audit_directory: Path,
) -> PreparedDraftResultsImport:
    """Run the standalone CSV importer against the website's current auction state."""

    league, rows = export_rows(db, league_id)
    confirmation_token = _state_token(league.id, league.season, rows)
    csv_path = export_csv(db, league.id, export_directory)
    plan = load_plan(csv_path, league_id=league.id, season=league.season)

    attempt = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + f"-{secrets.token_hex(4)}"
    output_directory = audit_directory / "mfl-draft-results" / league.id / attempt
    artifacts = write_artifacts(plan, output_directory)
    xml = build_draft_xml(plan)
    validate_draft_xml(xml, len(plan.picks))

    franchises = list(
        db.scalars(select(Franchise).where(Franchise.league_id == league.id).order_by(Franchise.id))
    )
    franchise_counts = Counter(pick.franchise_id for pick in plan.picks)
    expected_capacity = sum(
        max(int(franchise.roster_slots or league.roster_size or 0), 0) for franchise in franchises
    )
    errors: list[str] = []
    if not franchises:
        errors.append("No league franchises are available")
    if len(plan.picks) != expected_capacity:
        errors.append(
            f"The auction has {len(plan.picks)} purchases but the league requires "
            f"{expected_capacity} filled roster spots"
        )
    for franchise in franchises:
        expected = max(int(franchise.roster_slots or league.roster_size or 0), 0)
        observed = franchise_counts.get(franchise.id, 0)
        if observed != expected:
            errors.append(f"{franchise.name} has {observed} players; {expected} are required")

    return PreparedDraftResultsImport(
        plan=plan,
        artifacts=artifacts,
        output_directory=output_directory,
        confirmation_token=confirmation_token,
        expected_capacity=expected_capacity,
        franchise_counts=dict(sorted(franchise_counts.items())),
        readiness_errors=tuple(errors),
    )
