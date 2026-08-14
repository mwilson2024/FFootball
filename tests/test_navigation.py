from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import PlayerComparisonRequest

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_primary_navigation_prioritizes_live_draft_workflows() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    navigation = base.split('<nav class="main-nav"', 1)[1].split("</nav>", 1)[0]

    assert navigation.index('href="/draft"') < navigation.index('href="/auction"')
    assert navigation.index('href="/auction"') < navigation.index('href="/cheat-sheet"')
    assert 'href="/links"' not in navigation
    assert navigation.index('href="/depth-charts"') < navigation.index('href="/power-rankings"')
    assert navigation.index('href="/power-rankings"') < navigation.index('href="/account"')
    assert 'href="/keepers"' not in navigation
    assert 'href="/scoring"' not in navigation


def test_account_contains_keeper_and_scoring_links() -> None:
    account = (TEMPLATES / "account.html").read_text(encoding="utf-8")

    assert 'href="/keepers"' in account
    assert 'href="/scoring"' in account


def test_sources_page_offers_ranking_reset() -> None:
    sources = (TEMPLATES / "sources.html").read_text(encoding="utf-8")

    assert 'href="/links"' in sources
    assert "Links &amp; draft resources" in sources
    assert "Reset my rankings" in sources
    assert "resetMyRankings()" in sources


def test_cheat_sheet_reveals_up_to_five_comparison_players() -> None:
    cheat_sheet = (TEMPLATES / "cheat_sheet.html").read_text(encoding="utf-8")

    assert "Compare players" in cheat_sheet
    assert cheat_sheet.count('class="compare-player-input"') == 5
    assert cheat_sheet.count("data-compare-optional hidden") == 3
    assert "Add player" in cheat_sheet
    assert "Flip a coin" in cheat_sheet
    assert "Ask ChatGPT to break the tie" in cheat_sheet
    assert 'id="compare-recommendation"' in cheat_sheet


def test_player_comparison_schema_accepts_five_but_rejects_six() -> None:
    request = PlayerComparisonRequest(player_ids=["1", "2", "3", "4", "5"])

    assert request.player_ids == ["1", "2", "3", "4", "5"]
    with pytest.raises(ValidationError):
        PlayerComparisonRequest(player_ids=["1", "2", "3", "4", "5", "6"])


def test_sources_csv_preview_is_printable_and_downloadable() -> None:
    sources = (TEMPLATES / "sources.html").read_text(encoding="utf-8")

    assert "printSourceData()" in sources
    assert 'id="source-data-download"' in sources


def test_nomination_controls_live_in_admin_and_budget_boxes_show_turns() -> None:
    settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    auction = (TEMPLATES / "auction.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="admin-nomination-panel"' in settings
    assert "Randomize teams" in settings
    assert "Reset auction" in settings
    assert "Rob mode" in settings
    assert 'id="admin-user-list"' in settings
    assert 'draggable="true"' in script
    assert "setNominationPosition" in script
    assert "moveNominationTeam" not in script
    assert 'id="nomination-board"' not in auction
    assert "current-nominator" in script
    assert "next-nominator" in script
    assert "auction-complete" in script
    assert "changeAuctionPurchasePlayer" in script
    assert "Finished':`max" in script
    assert "Rob mode is on" in auction


def test_auction_activity_strip_keeps_latest_purchase_and_live_state_separate() -> None:
    auction = (TEMPLATES / "auction.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="auction-last-purchase"' in auction
    assert 'id="auction-pick-status"' in auction
    assert 'id="auction-live-status"' in auction
    assert "Current pick" in auction
    assert "nomination.overall_pick" in script
    assert "auction-live-badge ${live.is_live?'live':'offline'}" in script
    assert "recent-purchases" in script


def test_live_draft_board_is_linked_from_draft_room_and_admin() -> None:
    draft = (TEMPLATES / "draft.html").read_text(encoding="utf-8")
    settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    board = (TEMPLATES / "draft_board.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert "Open live draft board" in draft
    assert 'href="/draft-board?league_id={{ selected_league_id }}"' in draft
    assert 'id="admin-draft-board-launch"' in settings
    assert "Go live &amp; open board" in settings
    assert "launchLiveDraftBoard()" in settings
    assert "Team view" in board
    assert "Live draft view" in board
    assert 'id="team-draft-board"' in board
    assert 'id="chronological-draft-board"' in board
    assert "initLiveDraftBoard()" in board
    assert "renderTeamDraftBoard" in script
    assert "renderChronologicalDraftBoard" in script


def test_draft_room_has_personal_war_room_and_live_intelligence() -> None:
    draft = (TEMPLATES / "draft.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="draft-war-room"' in draft
    assert 'id="war-room-positions"' in draft
    assert 'id="opponent-needs"' in draft
    assert 'id="draft-intelligence-strip"' in draft
    assert "Roster construction" in draft
    assert "Teams between your picks" in draft
    assert "renderWarRoom" in script
    assert "renderDraftIntelligence" in script
    assert "renderDraftRecommendations" in script
    assert "heuristic only" in script
    assert (
        draft.index('class="draft-layout"')
        < draft.index('id="draft-intelligence-strip"')
        < draft.index('id="draft-war-room"')
        < draft.index("ROOM DISPLAY")
    )
    assert '<section class="panel" hidden aria-hidden="true"><h2>Tier inventory</h2>' in draft
