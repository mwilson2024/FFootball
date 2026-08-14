from __future__ import annotations

from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OWNER_LINKS_FILE = PROJECT_ROOT / "Links" / "Links.txt"
ESPN_CHEAT_SHEET_URL = (
    "https://www.espn.com/fantasy/football/story/_/page/"
    "FFCheatSheetCent26-48640423/"
    "2026-fantasy-football-rankings-cheat-sheet-depth-charts-ppr"
)

OWNER_LINK_DETAILS: dict[str, dict[str, str]] = {
    "https://www.pff.com/fantasy/rankings/draft": {
        "title": "PFF Fantasy Draft Rankings",
        "provider": "PFF",
        "category": "Rankings",
        "description": "PFF's current overall fantasy draft board.",
    },
    "https://www.fantasypros.com/nfl/rankings/half-point-ppr-cheatsheets.php": {
        "title": "Half-PPR Expert Rankings",
        "provider": "FantasyPros",
        "category": "Rankings",
        "description": "Half-PPR expert rankings and positional cheat sheets.",
    },
    "https://www.fantasypros.com/nfl/rankings/dynasty-overall.php": {
        "title": "Dynasty Overall Rankings",
        "provider": "FantasyPros",
        "category": "Dynasty & rookies",
        "description": "Overall dynasty rankings for long-term roster decisions.",
    },
    "https://www.fantasypros.com/nfl/rankings/dynasty-rookies-overall.php": {
        "title": "Dynasty Rookie Rankings",
        "provider": "FantasyPros",
        "category": "Dynasty & rookies",
        "description": "Rookie-only dynasty rankings for the ADFL player pool.",
    },
    ESPN_CHEAT_SHEET_URL: {
        "title": "ESPN 2026 Rankings, Cheat Sheet & Depth Charts",
        "provider": "ESPN",
        "category": "Rankings",
        "description": "ESPN's 2026 PPR rankings hub with linked depth charts.",
    },
}

CURATED_DRAFT_LINKS: tuple[dict[str, str], ...] = (
    {
        "title": "2026 Fantasy Football Draft Kit",
        "provider": "FantasyPros",
        "category": "Draft strategy",
        "description": (
            "A central guide to rankings, roster construction, tiers, and draft strategy."
        ),
        "url": "https://www.fantasypros.com/nfl/fantasy-football-draft-kit/",
    },
    {
        "title": "2026 Salary-Cap Draft Strategy & Targets",
        "provider": "FantasyPros",
        "category": "Auction strategy",
        "description": "Budgeting, tiers, nomination tactics, and common auction mistakes.",
        "url": "https://www.fantasypros.com/2026/06/fantasy-football-salary-cap-draft-strategy-targets-2026/",
    },
    {
        "title": "2026 Draft Values to Target",
        "provider": "FantasyPros",
        "category": "Draft strategy",
        "description": "Players whose expected role may be stronger than their market price.",
        "url": "https://www.fantasypros.com/2026/01/early-draft-values-to-target-fantasy-football/",
    },
    {
        "title": "2026 Sleepers to Target",
        "provider": "FantasyPros",
        "category": "Targets & fades",
        "description": "Late-round targets with a path to outperforming their draft cost.",
        "url": "https://www.fantasypros.com/2026/07/10-fantasy-football-sleepers-that-will-win-your-draft-2026/",
    },
    {
        "title": "2026 Busts Experts Avoid",
        "provider": "FantasyPros",
        "category": "Targets & fades",
        "description": "Risk cases for highly drafted running backs and wide receivers.",
        "url": "https://www.fantasypros.com/2026/07/2026-fantasy-football-busts-players-to-avoid/",
    },
    {
        "title": "Fantasy Football Injury News",
        "provider": "FantasyPros",
        "category": "Live updates",
        "description": "A frequently updated injury-news stream with fantasy impact notes.",
        "url": "https://www.fantasypros.com/nfl/injury-news.php",
    },
    {
        "title": "Lessons From Recent Dynasty Rookie Drafts",
        "provider": "Footballguys",
        "category": "Dynasty & rookies",
        "description": "Patterns from recent rookie classes and how to apply them in 2026.",
        "url": "https://www.footballguys.com/article/2026-lessons-learned-from-recent-rookie-drafts-0415",
    },
    {
        "title": "NFL Important Dates",
        "provider": "NFL Football Operations",
        "category": "Live updates",
        "description": (
            "Official league calendar for roster deadlines, camps, cuts, and the season."
        ),
        "url": "https://operations.nfl.com/calendar-events/nfl-important-dates",
    },
)

CATEGORY_ORDER = (
    "Rankings",
    "Draft strategy",
    "Auction strategy",
    "Dynasty & rookies",
    "Targets & fades",
    "Live updates",
)


def _owner_links(path: Path = OWNER_LINKS_FILE) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    links: list[dict[str, Any]] = []
    provider = "Saved link"
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("https://"):
            provider = line
            continue
        details = OWNER_LINK_DETAILS.get(
            line,
            {
                "title": provider,
                "provider": provider,
                "category": "Rankings",
                "description": "Saved in Links/Links.txt.",
            },
        )
        links.append({**details, "url": line, "owner_saved": True})
    return links


def draft_link_groups(path: Path = OWNER_LINKS_FILE) -> list[dict[str, Any]]:
    links = [*_owner_links(path), *({**item, "owner_saved": False} for item in CURATED_DRAFT_LINKS)]
    unique: dict[str, dict[str, Any]] = {str(item["url"]): item for item in links}
    groups = []
    for category in CATEGORY_ORDER:
        category_links = [item for item in unique.values() if item["category"] == category]
        if category_links:
            groups.append({"name": category, "links": category_links})
    return groups
