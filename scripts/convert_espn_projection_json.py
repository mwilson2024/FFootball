from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "CSV" / "ESPN_2026_PPR_Projections.csv"

POSITION_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "PK", 16: "DEF"}
TEAM_BY_ID = {
    1: "ATL",
    2: "BUF",
    3: "CHI",
    4: "CIN",
    5: "CLE",
    6: "DAL",
    7: "DEN",
    8: "DET",
    9: "GB",
    10: "TEN",
    11: "IND",
    12: "KC",
    13: "LV",
    14: "LAR",
    15: "MIA",
    16: "MIN",
    17: "NE",
    18: "NO",
    19: "NYG",
    20: "NYJ",
    21: "PHI",
    22: "ARI",
    23: "PIT",
    24: "LAC",
    25: "SF",
    26: "SEA",
    27: "TB",
    28: "WAS",
    29: "CAR",
    30: "JAX",
    33: "BAL",
    34: "HOU",
}
FIELDS = (
    "player_name",
    "team",
    "position",
    "season_projection",
    "projected_average",
    "espn_player_id",
    "injury_status",
    "season_outlook",
    "source",
    "source_url",
    "season",
    "scoring_preset_id",
)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _projection_stat(player: dict[str, Any], season: int) -> dict[str, Any] | None:
    for stat in player.get("stats") or []:
        if not isinstance(stat, dict):
            continue
        if (
            _as_int(stat.get("seasonId")) == season
            and _as_int(stat.get("scoringPeriodId"), -1) == 0
            and _as_int(stat.get("statSourceId")) == 1
            and _as_int(stat.get("statSplitTypeId"), -1) == 0
        ):
            return stat
    return None


def convert(input_path: Path, output_path: Path) -> int:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    season = int(payload.get("season") or 0)
    source_url = str(payload.get("source_url") or "")
    scoring_preset_id = payload.get("scoring_preset_id")
    rows_by_id: dict[str, dict[str, Any]] = {}
    for position_group in (payload.get("positions") or {}).values():
        for entry in position_group.get("players") or []:
            player = entry.get("player") or {}
            stat = _projection_stat(player, season)
            total = float((stat or {}).get("appliedTotal") or 0)
            position = POSITION_BY_ID.get(_as_int(player.get("defaultPositionId")))
            if total <= 0 or position is None:
                continue
            espn_id = str(player.get("id") or entry.get("id") or "")
            if not espn_id:
                continue
            rows_by_id[espn_id] = {
                "player_name": str(player.get("fullName") or "").strip(),
                "team": TEAM_BY_ID.get(_as_int(player.get("proTeamId")), ""),
                "position": position,
                "season_projection": f"{total:.3f}",
                "projected_average": f"{float((stat or {}).get('appliedAverage') or 0):.3f}",
                "espn_player_id": espn_id,
                "injury_status": str(player.get("injuryStatus") or ""),
                "season_outlook": str(player.get("seasonOutlook") or "").strip(),
                "source": "ESPN 2026 PPR season projection",
                "source_url": source_url,
                "season": season,
                "scoring_preset_id": scoring_preset_id,
            }
    rows = sorted(
        rows_by_id.values(),
        key=lambda row: (-float(row["season_projection"]), row["player_name"]),
    )
    if len(rows) < 400:
        raise ValueError(f"Expected at least 400 usable ESPN projections, found {len(rows)}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output_path)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert ESPN fantasy projection JSON to CSV")
    parser.add_argument("input", type=Path, help="Path to espn_2026_ppr_raw.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    count = convert(args.input, args.output)
    print(f"Wrote {count} ESPN projections to {args.output}")


if __name__ == "__main__":
    main()
