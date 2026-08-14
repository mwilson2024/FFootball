from app.draft_links import OWNER_LINKS_FILE, draft_link_groups


def test_draft_links_include_saved_folder_and_curated_draft_articles() -> None:
    assert OWNER_LINKS_FILE.is_file()
    groups = draft_link_groups()
    links = [link for group in groups for link in group["links"]]
    urls = {link["url"] for link in links}

    assert len([link for link in links if link["owner_saved"]]) == 5
    assert "https://www.pff.com/fantasy/rankings/draft" in urls
    assert "https://www.fantasypros.com/nfl/fantasy-football-draft-kit/" in urls
    assert any(group["name"] == "Auction strategy" for group in groups)
    assert len(urls) == len(links)
