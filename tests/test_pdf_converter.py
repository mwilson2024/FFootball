from scripts.convert_ranking_pdfs import parse_dynasty_text, parse_ppr_text


def test_ppr_parser_requires_and_returns_all_300_ranks() -> None:
    text = "\n".join(
        f"{rank}. (RB{rank}) Player {rank}, BUF ${301 - rank} {(rank % 14) + 1}"
        for rank in range(1, 301)
    )

    rows = parse_ppr_text(text, "ppr.pdf")

    assert len(rows) == 300
    assert rows[0]["overall_rank"] == "1"
    assert rows[-1]["overall_rank"] == "300"
    assert rows[0]["position"] == "RB"


def test_dynasty_parser_supports_undrafted_players_and_returns_all_240_ranks() -> None:
    text = "\n".join(
        f"{rank}. (WR{rank}) Player {rank}, BUF 2026-{'U' if rank == 240 else '1'} 22-6"
        for rank in range(1, 241)
    )

    rows = parse_dynasty_text(text, "dynasty.pdf")

    assert len(rows) == 240
    assert rows[0]["age_at_week_1"] == "22.5"
    assert rows[-1]["draft_round"] == "U"
