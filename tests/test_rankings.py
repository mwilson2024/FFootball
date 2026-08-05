from decimal import Decimal

from app.rankings import RankingInput, rank_players, scoring_warnings

PLAYERS = [
    RankingInput("qb1", "QB", Decimal("250"), 1, Decimal("5"), Decimal("40")),
    RankingInput("qb2", "QB", Decimal("220"), 20, Decimal("45"), Decimal("12")),
    RankingInput("qb3", "QB", Decimal("180"), 60, Decimal("90"), Decimal("4")),
    RankingInput("wr1", "WR", Decimal("210"), 2, Decimal("8"), Decimal("35")),
    RankingInput("wr2", "WR", Decimal("160"), 40, Decimal("70"), Decimal("8")),
    RankingInput("te1", "TE", Decimal("170"), 15, Decimal("35"), Decimal("15")),
]


def run(lineup, rules=None):
    return rank_players(
        PLAYERS,
        scoring_rules=rules or {},
        lineup=lineup,
        franchise_count=2,
        roster_size=4,
        available_spending_pool=Decimal("40"),
        minimum_bid=Decimal("1"),
    )


def score(rows, player_id):
    return next(row.custom_score for row in rows if row.player_id == player_id)


def test_ppr_and_te_premium_change_receiving_values():
    standard = run({"QB": 1, "WR": 1, "TE": 1})
    premium = run({"QB": 1, "WR": 1, "TE": 1}, {"ppr": 1, "te_premium": 1})
    assert score(premium, "wr1") > score(standard, "wr1")
    assert score(premium, "te1") > score(standard, "te1")


def test_superflex_raises_qb_value_and_is_deterministic():
    one_qb = run({"QB": 1, "WR": 1})
    superflex = run({"QB": 1, "WR": 1, "SUPERFLEX": 1})
    assert score(superflex, "qb1") > score(one_qb, "qb1")
    assert superflex == run({"QB": 1, "WR": 1, "SUPERFLEX": 1})


def test_unknown_rules_are_visible():
    assert scoring_warnings({"mystery_xyz": 4}) == ["Unmapped scoring rule retained: mystery_xyz"]


def test_unranked_defense_does_not_outrank_unranked_offense():
    rows = rank_players(
        [
            RankingInput(player_id="def", position="DEF"),
            RankingInput(player_id="rb", position="RB"),
        ],
        scoring_rules={},
        lineup={"DEF": 1, "RB": 1},
        franchise_count=12,
        roster_size=16,
        available_spending_pool=Decimal("200"),
        minimum_bid=Decimal("1"),
    )

    assert [row.player_id for row in rows] == ["rb", "def"]
