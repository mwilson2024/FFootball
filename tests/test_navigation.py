from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_primary_navigation_prioritizes_live_draft_workflows() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    navigation = base.split('<nav class="main-nav"', 1)[1].split("</nav>", 1)[0]

    assert navigation.index('href="/draft"') < navigation.index('href="/auction"')
    assert navigation.index('href="/auction"') < navigation.index('href="/cheat-sheet"')
    assert 'href="/links"' in navigation
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

    assert "Reset my rankings" in sources
    assert "resetMyRankings()" in sources


def test_cheat_sheet_offers_three_player_comparison_and_tie_breakers() -> None:
    cheat_sheet = (TEMPLATES / "cheat_sheet.html").read_text(encoding="utf-8")

    assert "Compare players" in cheat_sheet
    assert cheat_sheet.count('class="compare-player-input"') == 3
    assert "Flip a coin" in cheat_sheet
    assert "Ask ChatGPT to break the tie" in cheat_sheet


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
