from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "CSV" / "PFF_2026_PPR_Projections.csv"
POSITION_ALIASES = {"D": "DEF", "DST": "DEF", "K": "PK"}
FIELDS = (
    "player_name",
    "team",
    "position",
    "season_projection",
    "projected_average",
    "projection_floor",
    "projection_ceiling",
    "pff_player_id",
    "overall_rank",
    "position_rank",
    "tier",
    "adp",
    "bye_week",
    "tags",
    "expert_analysis",
    "ranker",
    "scoring_format",
    "source",
    "last_updated_at",
)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_number(value: Any) -> str:
    number = _number(value)
    return f"{number:.3f}" if number is not None else ""


def convert(input_path: Path, output_path: Path, *, minimum_rows: int = 400) -> int:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    updated_timestamp = _number(payload.get("lastUpdatedAt"))
    updated_at = (
        datetime.fromtimestamp(updated_timestamp, UTC).isoformat()
        if updated_timestamp is not None
        else ""
    )
    rows: list[dict[str, Any]] = []
    for raw in payload.get("rankings") or []:
        projection = raw.get("projection") or {}
        points = projection.get("points") or {}
        midpoint = _number(points.get("mid"))
        if midpoint is None or midpoint <= 0:
            continue
        position = str(raw.get("position") or "").strip().upper()
        position = POSITION_ALIASES.get(position, position)
        if position not in {"QB", "RB", "WR", "TE", "PK", "DEF"}:
            continue
        rank = raw.get("rank") or {}
        analysis = [
            " ".join(str(item).split())
            for item in (raw.get("expertAnalysis") or [])
            if str(item).strip()
        ]
        player_name = " ".join(
            part
            for part in (
                str(raw.get("firstName") or "").strip(),
                str(raw.get("lastName") or "").strip(),
            )
            if part
        )
        rows.append(
            {
                "player_name": player_name,
                "team": str(raw.get("teamAbbreviation") or "").strip().upper(),
                "position": position,
                "season_projection": f"{midpoint:.3f}",
                "projected_average": f"{midpoint / 17:.3f}",
                "projection_floor": _format_number(points.get("low")),
                "projection_ceiling": _format_number(points.get("high")),
                "pff_player_id": str(raw.get("playerId") or raw.get("id") or ""),
                "overall_rank": str(rank.get("current") or ""),
                "position_rank": str(rank.get("position") or ""),
                "tier": str(raw.get("tier") or ""),
                "adp": str(raw.get("adp") or ""),
                "bye_week": str(raw.get("byeWeek") or ""),
                "tags": "|".join(str(tag).strip() for tag in (raw.get("tags") or [])),
                "expert_analysis": "\n\n".join(analysis),
                "ranker": str(rank.get("rankerName") or ""),
                "scoring_format": str(raw.get("adpScoringType") or "PPR"),
                "source": "PFF 2026 PPR season projections",
                "last_updated_at": updated_at,
            }
        )
    rows.sort(key=lambda row: (-float(row["season_projection"]), row["player_name"]))
    if len(rows) < minimum_rows:
        raise ValueError(
            f"Expected at least {minimum_rows} usable PFF projections, found {len(rows)}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output_path)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert PFF fantasy projection JSON to CSV")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    count = convert(args.input, args.output)
    print(f"Wrote {count} PFF projections to {args.output}")


if __name__ == "__main__":
    main()
