from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Player, SourcePlayerValue

NFL_TEAMS: tuple[dict[str, str], ...] = (
    {"code": "ARI", "name": "Arizona Cardinals", "espn_code": "ari"},
    {"code": "ATL", "name": "Atlanta Falcons", "espn_code": "atl"},
    {"code": "BAL", "name": "Baltimore Ravens", "espn_code": "bal"},
    {"code": "BUF", "name": "Buffalo Bills", "espn_code": "buf"},
    {"code": "CAR", "name": "Carolina Panthers", "espn_code": "car"},
    {"code": "CHI", "name": "Chicago Bears", "espn_code": "chi"},
    {"code": "CIN", "name": "Cincinnati Bengals", "espn_code": "cin"},
    {"code": "CLE", "name": "Cleveland Browns", "espn_code": "cle"},
    {"code": "DAL", "name": "Dallas Cowboys", "espn_code": "dal"},
    {"code": "DEN", "name": "Denver Broncos", "espn_code": "den"},
    {"code": "DET", "name": "Detroit Lions", "espn_code": "det"},
    {"code": "GBP", "name": "Green Bay Packers", "espn_code": "gb"},
    {"code": "HOU", "name": "Houston Texans", "espn_code": "hou"},
    {"code": "IND", "name": "Indianapolis Colts", "espn_code": "ind"},
    {"code": "JAC", "name": "Jacksonville Jaguars", "espn_code": "jax"},
    {"code": "KCC", "name": "Kansas City Chiefs", "espn_code": "kc"},
    {"code": "LAC", "name": "Los Angeles Chargers", "espn_code": "lac"},
    {"code": "LAR", "name": "Los Angeles Rams", "espn_code": "lar"},
    {"code": "LVR", "name": "Las Vegas Raiders", "espn_code": "lv"},
    {"code": "MIA", "name": "Miami Dolphins", "espn_code": "mia"},
    {"code": "MIN", "name": "Minnesota Vikings", "espn_code": "min"},
    {"code": "NEP", "name": "New England Patriots", "espn_code": "ne"},
    {"code": "NOS", "name": "New Orleans Saints", "espn_code": "no"},
    {"code": "NYG", "name": "New York Giants", "espn_code": "nyg"},
    {"code": "NYJ", "name": "New York Jets", "espn_code": "nyj"},
    {"code": "PHI", "name": "Philadelphia Eagles", "espn_code": "phi"},
    {"code": "PIT", "name": "Pittsburgh Steelers", "espn_code": "pit"},
    {"code": "SEA", "name": "Seattle Seahawks", "espn_code": "sea"},
    {"code": "SFO", "name": "San Francisco 49ers", "espn_code": "sf"},
    {"code": "TBB", "name": "Tampa Bay Buccaneers", "espn_code": "tb"},
    {"code": "TEN", "name": "Tennessee Titans", "espn_code": "ten"},
    {"code": "WAS", "name": "Washington Commanders", "espn_code": "wsh"},
)
TEAM_ALIASES = {
    "GB": "GBP",
    "JAX": "JAC",
    "KC": "KCC",
    "LV": "LVR",
    "NE": "NEP",
    "NO": "NOS",
    "SF": "SFO",
    "TB": "TBB",
    "WSH": "WAS",
}
TEAM_LOOKUP = {team["code"]: team for team in NFL_TEAMS}
POSITION_ORDER = {
    position: index
    for index, position in enumerate(
        (
            "QB",
            "RB",
            "FB",
            "LWR",
            "WR",
            "RWR",
            "SWR",
            "TE",
            "LT",
            "LG",
            "C",
            "RG",
            "RT",
            "OL",
            "LDE",
            "DE",
            "RDE",
            "LDT",
            "DT",
            "RDT",
            "NT",
            "WLB",
            "ILB",
            "MLB",
            "OLB",
            "SLB",
            "LB",
            "LCB",
            "CB",
            "RCB",
            "NB",
            "SS",
            "FS",
            "S",
            "DB",
            "PK",
            "K",
            "P",
            "H",
            "PR",
            "KR",
            "LS",
        )
    )
}


def normalize_team_code(value: str | None) -> str:
    code = str(value or "").strip().upper()
    return TEAM_ALIASES.get(code, code)


def _depth_order(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def depth_chart_overview(db: Session, team: str | None = None) -> dict[str, Any]:
    latest_depth: dict[str, SourcePlayerValue] = {}
    for value in db.scalars(
        select(SourcePlayerValue)
        .where(SourcePlayerValue.value_type == "depth_chart")
        .order_by(SourcePlayerValue.fetched_at.desc(), SourcePlayerValue.id.desc())
    ):
        latest_depth.setdefault(value.player_id, value)

    rows: list[dict[str, Any]] = []
    for player in db.scalars(select(Player).order_by(Player.name)):
        metadata = player.metadata_json or {}
        nflverse_depth = (metadata.get("nflverse") or {}).get("depth_chart") or {}
        sleeper_depth = metadata.get("sleeper") or {}
        source_value = latest_depth.get(player.id)
        source_depth = source_value.raw_value_json or {} if source_value else {}
        depth = source_depth or nflverse_depth or sleeper_depth
        depth_position = depth.get("depth_position") or depth.get("depth_chart_position")
        depth_order = _depth_order(depth.get("depth_team") or depth.get("depth_chart_order"))
        team_code = normalize_team_code(depth.get("team") or player.nfl_team)
        if not depth_position or team_code not in TEAM_LOOKUP:
            continue
        rows.append(
            {
                "player_id": player.id,
                "player_name": player.name,
                "nfl_team": team_code,
                "position": player.position,
                "depth_position": str(depth_position).upper(),
                "depth_order": depth_order,
                "formation": depth.get("formation"),
                "injury_status": player.injury_status,
                "rookie": player.rookie,
                "source": "nflverse" if source_value or nflverse_depth else "Sleeper",
                "updated_at": source_value.fetched_at if source_value else player.updated_at,
            }
        )

    rows.sort(
        key=lambda row: (
            row["nfl_team"],
            POSITION_ORDER.get(row["depth_position"], 999),
            row["depth_position"],
            row["depth_order"] if row["depth_order"] is not None else 99,
            row["player_name"],
        )
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["nfl_team"]] = counts.get(row["nfl_team"], 0) + 1

    selected_code = normalize_team_code(team)
    if selected_code not in TEAM_LOOKUP:
        selected_code = next(
            (item["code"] for item in NFL_TEAMS if counts.get(item["code"])), "ARI"
        )
    selected_team = TEAM_LOOKUP[selected_code]
    selected_players = [row for row in rows if row["nfl_team"] == selected_code]
    updated_values = [
        row["updated_at"] for row in selected_players if row["updated_at"] is not None
    ]
    teams = [
        {
            **item,
            "player_count": counts.get(item["code"], 0),
            "espn_url": f"https://www.espn.com/nfl/team/depth/_/name/{item['espn_code']}",
        }
        for item in NFL_TEAMS
    ]
    return {
        "selected_team": {
            **selected_team,
            "espn_url": (
                f"https://www.espn.com/nfl/team/depth/_/name/{selected_team['espn_code']}"
            ),
        },
        "teams": teams,
        "players": selected_players,
        "updated_at": max(updated_values) if updated_values else None,
        "ordered_player_count": sum(
            1 for row in selected_players if row["depth_order"] is not None
        ),
        "source_note": (
            "Depth order uses the latest cached nflverse chart when available, "
            "then Sleeper metadata as a fallback."
        ),
    }
