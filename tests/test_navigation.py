from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_primary_navigation_prioritizes_live_draft_workflows() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    navigation = base.split('<nav class="main-nav"', 1)[1].split("</nav>", 1)[0]

    assert navigation.index('href="/draft"') < navigation.index('href="/auction"')
    assert navigation.index('href="/auction"') < navigation.index('href="/cheat-sheet"')
    assert navigation.index('href="/depth-charts"') < navigation.index('href="/power-rankings"')
    assert navigation.index('href="/power-rankings"') < navigation.index('href="/account"')
    assert 'href="/keepers"' not in navigation
    assert 'href="/scoring"' not in navigation


def test_account_contains_keeper_and_scoring_links() -> None:
    account = (TEMPLATES / "account.html").read_text(encoding="utf-8")

    assert 'href="/keepers"' in account
    assert 'href="/scoring"' in account
