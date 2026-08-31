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


def test_post_draft_analysis_lives_only_on_power_rankings() -> None:
    draft = (TEMPLATES / "draft.html").read_text(encoding="utf-8")
    power = (TEMPLATES / "power_rankings.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert "POST-DRAFT &amp; COUNTERFACTUAL ANALYSIS" not in draft
    assert 'id="projected-standings-body"' not in draft
    assert "POST-DRAFT &amp; COUNTERFACTUAL ANALYSIS" in power
    assert 'id="power-analysis-franchise"' in power
    assert 'id="projected-standings-body"' in power
    assert "Click a team below" in power
    assert "powerRosterHtml" in script
    assert "selectPowerRankingTeam" in script
    assert "View full roster" in script
    assert "power-roster-table" in script
    assert "powerCacheLabel" in script
    assert "Stored" in script
    assert "power-team-page-link" in script
    draft_loader = script.split("async function loadDraft()", 1)[1].split(
        "function renderDraftLive", 1
    )[0]
    power_loader = script.split("async function loadPowerDraftAnalysis", 1)[1].split(
        "async function initPowerRankings", 1
    )[0]
    assert "/api/draft/analysis" not in draft_loader
    assert "/api/draft/bootstrap" in draft_loader
    assert "/api/players?" not in draft_loader
    assert "void loadDraftIntelligence" in draft_loader
    assert "['power-rankings-cache']" in script
    assert "/api/draft/analysis" in power_loader


def test_franchise_page_uses_the_stored_power_report() -> None:
    franchise = (TEMPLATES / "franchise.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert "STORED POWER REPORT" in franchise
    assert 'id="franchise-power-metrics"' in franchise
    assert 'href="/power-rankings?league_id=' in franchise
    assert "renderStoredFranchisePower(team.stored_power)" in script


def test_account_contains_keeper_and_scoring_links() -> None:
    account = (TEMPLATES / "account.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'href="/keepers"' in account
    assert 'href="/scoring"' in account
    assert "Shared draft format" in script
    assert "/api/admin/leagues/" in script
    assert "/format" in script


def test_auction_page_supports_multiple_leagues_and_scoped_exports() -> None:
    auction = (TEMPLATES / "auction.html").read_text(encoding="utf-8")
    settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert "Auction league" in auction
    assert "auction_leagues" in auction
    assert "/api/auction/export.csv?league_id={{ league.id }}" in auction
    assert "/api/auction/export.xml?league_id={{ league.id }}" in auction
    assert "Preview MFL import" not in auction
    assert ">MFL Import<" in auction
    assert "Download CSV" in auction
    assert "Download MFL XML" in auction
    assert "Import as auction results" in auction
    assert "Import rosters as draft results" in auction
    assert 'id="mfl-commissioner-import" hidden' in auction
    assert "openMflImport()" in auction
    assert "showMflCommissionerImport()" in auction
    assert "showMflDraftResultsImport()" in auction
    assert "function openMflImport" in script
    assert "async function showMflCommissionerImport" in script
    assert "async function showMflDraftResultsImport" in script
    assert 'id="admin-auction-league"' in settings
    assert "loadAdminAuctionLeagues" in script
    assert "selectAdminAuctionLeague" in script


def test_auction_has_compact_money_cards_roster_viewer_and_auctioneer_page() -> None:
    auction = (TEMPLATES / "auction.html").read_text(encoding="utf-8")
    auctioneer = (TEMPLATES / "auctioneer.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")
    css = (TEMPLATES.parent / "static" / "app.css").read_text(encoding="utf-8")

    assert "/auction/auctioneer?league_id=" in auction
    assert "{% if auctioneer_view %}" in auction
    assert 'id="auction-roster-team"' in auction
    assert 'step="1" inputmode="numeric"' in auction
    assert 'id="record-winning-bid-button"' in auction
    assert 'id="sale-dialog"' in auction
    assert ">Purchase</button>" in script
    assert "/auction/history?league_id=" in auction
    assert "purchases.slice(0,12)" in script
    assert 'class="purchase-menu"' in script
    assert auctioneer.strip() == '{% extends "auction.html" %}'
    assert "function auctionRosterTeams" in script
    assert "function viewAuctionRoster" in script
    assert 'class="budget-primary"' in script
    assert 'class="budget-primary budget-full"' in script
    assert 'class="budget-extra"' in script
    assert '${escapeHtml(team.name)}</h3>' in script
    assert "Winning amount must be a whole dollar amount" in script
    assert ".budget:hover .budget-extra" in css
    assert 'class="auction-values-column"' in auction
    assert 'class="auction-values-top"' in auction
    assert ".auction-viewer-workspace" in css
    assert ".auctioneer-workspace" in css
    assert ".auction-values-column .budget dl.budget-primary" in css
    assert "grid-template-columns:auto auto auto auto" in css
    assert ".auction-values-column .budget:hover dl.budget-primary" in css
    assert ".auction-values-column .budget-heading h3" in css
    assert ".auction-values-column .budget-turns{flex:none}" in css
    assert "minmax(225px,.68fr)" in css
    assert "scrollbar-gutter:stable" in css


def test_admin_shows_persistent_commissioner_import_toggle() -> None:
    settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="commissioner-imports-toggle"' in settings
    assert 'id="commissioner-imports-status"' in settings
    assert "setCommissionerImports(this.checked)" in settings
    assert "/api/admin/commissioner-imports" in script
    assert "Current status: TRUE" in script
    assert "Current status: FALSE" in script


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


def test_every_source_has_a_printable_and_downloadable_spreadsheet() -> None:
    sources = (TEMPLATES / "sources.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert "printSourceData()" in sources
    assert 'id="source-data-download"' in sources
    assert "SOURCE SPREADSHEET" in sources
    assert "View spreadsheet" in script
    assert "View CSV rows" not in script


def test_cheat_sheet_links_to_ranking_tuning_without_snapshots() -> None:
    cheat_sheet = (TEMPLATES / "cheat_sheet.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'href="/sources"' in cheat_sheet
    assert "Tune rankings" in cheat_sheet
    assert "Save snapshot" not in cheat_sheet
    assert "saveSnapshot" not in script


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
    assert 'id="sale-access-note"' in auction
    assert "Rob mode is on" in script
    assert 'id="admin-auction-stage-toggle"' in settings
    assert 'id="admin-auction-live-toggle"' in settings
    assert 'id="interactive-auction-toggle"' in settings
    assert 'id="admin-auction-handoff"' in settings
    assert 'id="auction-owner-handoff"' not in auction
    assert "Switch to owner bidding" not in auction
    assert "Use live nominations and bidding" in settings
    assert 'id="interactive-auction-panel"' in auction
    assert "renderInteractiveAuction" in script
    assert "nominateInteractivePlayer" in script
    assert "placeInteractiveBid" in script
    assert "awardInteractiveAuction" in script
    assert "handoffAuctionToOwners" in script
    assert "/api/admin/interactive-auction/handoff" in script
    assert "/api/presence" in script


def test_auction_activity_strip_keeps_latest_purchase_and_live_state_separate() -> None:
    auction = (TEMPLATES / "auction.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="auction-last-purchase"' in auction
    assert 'id="auction-pick-status"' in auction
    assert 'id="auction-live-status"' in auction
    assert "Current pick" in auction
    assert "nomination.overall_pick" in script
    assert "auction-live-badge ${phase==='live'?'live':'offline'}" in script
    assert "Auction staging" in script
    assert "Auction is closed" in script
    assert "recent-purchases" in script


def test_live_draft_board_is_linked_from_draft_room_and_admin() -> None:
    draft = (TEMPLATES / "draft.html").read_text(encoding="utf-8")
    settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    board = (TEMPLATES / "draft_board.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert "Open live draft board" in draft
    assert 'href="/draft-board?league_id={{ selected_league_id }}"' in draft
    assert 'id="admin-draft-board-launch"' in settings
    assert 'id="admin-draft-mode-companion"' in settings
    assert 'id="admin-draft-mode-local"' in settings
    assert 'id="admin-real-draft-toggle"' in settings
    assert 'id="admin-mock-draft-toggle"' in settings
    assert 'id="admin-mock-draft-reset"' in settings
    assert 'id="admin-draft-connection"' in settings
    assert 'id="admin-draft-connection-warning"' in settings
    assert settings.index('id="admin-draft-connection"') > settings.index(
        'id="admin-nomination-panel"'
    )
    assert "/api/admin/draft-connection" in script
    assert "Go Live &amp; open board" in settings
    assert ">Go Live</button>" in draft
    assert "editRealDraftPick" in script
    assert "removeRealDraftPick" in script
    assert "launchLiveDraftBoard()" in settings
    assert "Team view" in board
    assert "Live draft view" in board
    assert 'id="team-draft-board"' in board
    assert 'id="chronological-draft-board"' in board
    assert "initLiveDraftBoard()" in board
    assert "renderTeamDraftBoard" in script
    assert "renderChronologicalDraftBoard" in script
    assert 'id="draft-tv-mode"' in board
    assert 'id="tv-board-stage"' in board
    assert 'id="tv-recent-picks"' in board
    assert "toggleDraftBoardTvMode" in script
    assert "renderTvDraftStage" in script
    assert "TV layout is on" in script


def test_draft_advisor_offers_broader_fantasy_topics_and_opinions() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assistant = (TEMPLATES.parent / "assistant.py").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    for label in (
        "Next pick",
        "Roster needs",
        "Compare players",
        "Auction plan",
        "Trade / keeper",
        "Lineup / waivers",
    ):
        assert label in base
    assert "setAssistantTopic" in script
    assert "trades, keepers, auctions, lineups, waivers, bye weeks" in script
    assert "experienced and opinionated fantasy-football strategist" in assistant
    assert "waiver and FAAB strategy" in assistant
    assert "clear recommendation or lean" in assistant
    assert "identify the biggest downside" in assistant
    assert '"max_output_tokens": 1100' in assistant


def test_real_draft_ui_supports_companion_and_local_modes() -> None:
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert "MFL companion is live" in script
    assert "import automatically every 30 seconds" in script
    assert "make selections on MFL" in script
    assert "MFL only" in script
    assert "Real-time local draft is live" in script
    assert "setAdminDraftMode" in script


def test_draft_room_has_personal_war_room_and_live_intelligence() -> None:
    draft = (TEMPLATES / "draft.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="draft-war-room"' in draft
    assert 'id="war-room-label"' in draft
    assert 'id="war-room-personal-button"' in draft
    assert 'id="war-room-team-page"' in draft
    assert 'id="war-room-targets-button"' in draft
    assert 'id="draft-recommendations"' in draft
    assert 'id="draft-position"' in draft
    assert 'id="draft-tier"' in draft
    assert 'id="war-room-positions"' in draft
    assert 'id="opponent-needs"' in draft
    assert 'id="draft-owner-insights"' in draft
    assert "Opposing owner intelligence" in draft
    assert 'id="draft-intelligence-strip"' in draft
    assert "Roster construction" in draft
    assert "Teams between your picks" in draft
    assert "renderWarRoom" in script
    assert "viewDraftWarRoom" in script
    assert "viewPotentialTargets" in script
    assert "syncDraftBoardFilters" in script
    assert "No available players match these filters." in script
    assert "teamPage.href=`/franchises/" in script
    assert "View potential targets" in draft
    assert "Roster strength" in script
    war_room_selector = script.split("async function viewDraftWarRoom", 1)[1].split(
        "function viewPotentialTargets", 1
    )[0]
    assert "scrollIntoView" not in war_room_selector
    target_jump = script.split("function viewPotentialTargets", 1)[1].split(
        "function renderWarRoom", 1
    )[0]
    assert "scrollIntoView" in target_jump
    assert "renderDraftIntelligence" in script
    assert "selectDraftIntelPlayer" in script
    assert "Player focus" in script
    assert "renderDraftRecommendations" in script
    assert "renderOwnerIntelligence" in script
    assert "heuristic only" in script
    assert (
        draft.index('class="draft-layout"')
        < draft.index('id="draft-intelligence-strip"')
        < draft.index('id="draft-war-room"')
        < draft.index("ROOM DISPLAY")
    )
    assert '<section class="panel" hidden aria-hidden="true"><h2>Tier inventory</h2>' in draft


def test_auction_has_live_intelligence_personal_war_room_and_owner_insights() -> None:
    auction = (TEMPLATES / "auction.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="auction-intelligence-strip"' in auction
    assert "LIVE AUCTION INTELLIGENCE" in auction
    assert 'id="auction-war-room"' in auction
    assert "PERSONAL AUCTION WAR ROOM" in auction
    assert 'id="auction-war-room-targets"' in auction
    assert 'id="auction-owner-insights"' in auction
    assert 'class="auction-layout auction-workspace ' in auction
    assert 'class="auction-values-top"' in auction
    assert 'id="budget-grid"' in auction
    assert 'id="auction-current-roster"' in auction
    assert 'id="auction-current-roster-content"' in auction
    assert 'id="auction-user-budget"' in auction
    assert 'class="auction-center-status"' in auction
    assert "Opposing owner intelligence" in auction
    assert "renderAuctionIntelligence" in script
    assert "renderAuctionWarRoom" in script
    assert "renderAuctionCurrentRoster" in script
    assert "position-${positionClass}" in script
    assert "canRecordAuctionPurchase" in script
    assert "auctionState.current_user_franchise_id" in script
    assert "minimum-only" in script
    assert "focusAuctionPlayer" in script
    assert "renderOwnerIntelligence" in script
    assert (
        auction.index('id="budget-grid"')
        < auction.index("Available players")
        < auction.index('id="auction-current-roster"')
        < auction.index('id="recent-purchases"')
        < auction.index('id="sale-dialog"')
    )
    assert (
        auction.index('class="auction-layout auction-workspace ')
        < auction.index('id="auction-intelligence-strip"')
        < auction.index('id="auction-war-room"')
    )
    assert ".auction-values-top .budget-grid" in (
        TEMPLATES.parent / "static" / "app.css"
    ).read_text(encoding="utf-8")
    css = (TEMPLATES.parent / "static" / "app.css").read_text(encoding="utf-8")
    assert ".auction-current-roster-table tbody tr.position-WR" in css
    assert ".auction-current-roster-table tbody tr.position-TE" in css
    assert ".auction-support-grid" in css
    assert ".auction-recent-list" in css
    assert ".purchase-menu-options" in css


def test_player_profile_has_contextual_back_button() -> None:
    profile = (TEMPLATES / "player_profile.html").read_text(encoding="utf-8")
    script = (TEMPLATES.parent / "static" / "app.js").read_text(encoding="utf-8")

    assert "← Back" in profile
    assert "returnToPlayerOrigin()" in profile
    assert "document.referrer" in script
