from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


@dataclass(frozen=True)
class RankingInput:
    player_id: str
    position: str
    projection: Decimal | None = None
    mfl_rank: int | None = None
    adp: Decimal | None = None
    aav: Decimal | None = None


@dataclass(frozen=True)
class RankedPlayer:
    player_id: str
    position: str
    overall_rank: int
    position_rank: int
    tier: int
    custom_score: Decimal
    projected_points: Decimal
    replacement_points: Decimal
    vorp: Decimal
    baseline_value: Decimal
    live_value: Decimal
    sources: dict[str, Any]


KNOWN_RULE_TOKENS = {
    "pass",
    "rush",
    "rec",
    "reception",
    "first_down",
    "bonus",
    "interception",
    "fumble",
    "kick",
    "def",
    "idp",
    "return",
    "position",
    "premium",
}


def scoring_warnings(rules: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for key, value in rules.items():
        if isinstance(value, dict) and value.get("description"):
            continue
        lowered = key.lower()
        if not any(token in lowered for token in KNOWN_RULE_TOKENS):
            warnings.append(f"Unmapped scoring rule retained: {key}")
    return warnings


def _decimal(value: Any, fallback: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(fallback)


def _projection(item: RankingInput, rules: dict[str, Any]) -> tuple[Decimal, str]:
    if item.projection is not None:
        base = item.projection
        source = "MFL weekly projected score; not a full-season projection"
    else:
        if item.mfl_rank is None and item.adp is None:
            base = Decimal("0")
            source = "no projection or market rank available; retained in the full player pool"
        else:
            rank = Decimal(item.mfl_rank or 300)
            adp = item.adp or Decimal("300")
            base = max(
                Decimal("0"),
                Decimal("250") - rank * Decimal("0.35") - adp * Decimal("0.15"),
            )
            source = "market proxy from MFL rank and ADP; not a season projection"
    position = item.position.upper()
    reception_points = _decimal(rules.get("receptions", rules.get("ppr", 0)))
    if position in {"WR", "RB", "TE"}:
        base *= Decimal("1") + reception_points * Decimal("0.08")
    if position == "TE":
        base *= Decimal("1") + _decimal(rules.get("te_premium", 0)) * Decimal("0.08")
    if position == "QB":
        pass_td = _decimal(rules.get("passing_td", 4), "4")
        base *= Decimal("1") + (pass_td - Decimal("4")) * Decimal("0.04")
    return base, source


def rank_players(
    players: list[RankingInput],
    *,
    scoring_rules: dict[str, Any],
    lineup: dict[str, Any],
    franchise_count: int,
    roster_size: int,
    available_spending_pool: Decimal,
    minimum_bid: Decimal,
    inflation: Decimal = Decimal("1"),
) -> list[RankedPlayer]:
    if not players:
        return []
    projections: dict[str, tuple[Decimal, str]] = {
        item.player_id: _projection(item, scoring_rules) for item in players
    }
    by_position: dict[str, list[RankingInput]] = defaultdict(list)
    for item in players:
        by_position[item.position.upper()].append(item)
    replacement: dict[str, Decimal] = {}
    flex = int(lineup.get("FLEX", 0) or 0)
    superflex = int(lineup.get("SUPERFLEX", lineup.get("QB_FLEX", 0)) or 0)
    bench_per_position = max(1, roster_size // max(1, len(by_position)) // 2)
    for position, candidates in by_position.items():
        ordered = sorted(candidates, key=lambda p: projections[p.player_id][0], reverse=True)
        starters = int(lineup.get(position, 0) or 0)
        flex_share = flex if position in {"RB", "WR", "TE"} else 0
        if position == "QB":
            flex_share += superflex
        replacement_demand = franchise_count * (starters + flex_share + bench_per_position)
        index = min(max(replacement_demand - 1, 0), len(ordered) - 1)
        replacement[position] = projections[ordered[index].player_id][0]
    raw: list[tuple[RankingInput, Decimal, Decimal, Decimal, str]] = []
    for item in players:
        projection, source = projections[item.player_id]
        repl = replacement[item.position.upper()]
        vorp = projection - repl
        scarcity = max(Decimal("0"), vorp) * Decimal("0.10")
        lineup_demand = Decimal(str(lineup.get(item.position.upper(), 0) or 0)) * Decimal("1.5")
        if item.position.upper() == "QB" and superflex:
            lineup_demand += Decimal(franchise_count) * Decimal("0.5")
        if item.position.upper() == "TE":
            lineup_demand += _decimal(scoring_rules.get("te_premium", 0)) * Decimal("2")
        market = Decimal("0")
        if item.adp is not None:
            market += max(Decimal("0"), Decimal("200") - item.adp) / Decimal("100")
        if item.aav is not None:
            market += item.aav / Decimal("100")
        score = vorp + scarcity + lineup_demand + market
        raw.append((item, projection, repl, score, source))
    primary_positions = {"QB", "RB", "WR", "TE"}

    def ranking_key(
        row: tuple[RankingInput, Decimal, Decimal, Decimal, str],
    ) -> tuple[bool, Decimal, bool, Decimal, str]:
        item, projection, _, score, _ = row
        has_signal = any(
            value is not None for value in (item.projection, item.mfl_rank, item.adp, item.aav)
        )
        return (
            has_signal,
            score if has_signal else Decimal("0"),
            item.position.upper() in primary_positions,
            projection,
            item.player_id,
        )

    raw.sort(key=ranking_key, reverse=True)
    positive_pool = sum((max(Decimal("0"), row[3]) for row in raw), Decimal("0"))
    reserve = minimum_bid * Decimal(len(raw))
    allocatable = max(Decimal("0"), available_spending_pool - reserve)
    position_counts: dict[str, int] = defaultdict(int)
    result: list[RankedPlayer] = []
    tier = 1
    previous: Decimal | None = None
    for overall, (item, projection, repl, score, source) in enumerate(raw, 1):
        position_counts[item.position.upper()] += 1
        if previous is not None and previous - score >= Decimal("8"):
            tier += 1
        previous = score
        share = max(Decimal("0"), score) / positive_pool if positive_pool else Decimal("0")
        baseline = (minimum_bid + share * allocatable).quantize(Decimal("0.01"), ROUND_HALF_UP)
        live = (baseline * inflation).quantize(Decimal("0.01"), ROUND_HALF_UP)
        result.append(
            RankedPlayer(
                player_id=item.player_id,
                position=item.position.upper(),
                overall_rank=overall,
                position_rank=position_counts[item.position.upper()],
                tier=tier,
                custom_score=score.quantize(Decimal("0.0001")),
                projected_points=projection.quantize(Decimal("0.0001")),
                replacement_points=repl.quantize(Decimal("0.0001")),
                vorp=(projection - repl).quantize(Decimal("0.0001")),
                baseline_value=baseline,
                live_value=live,
                sources={
                    "projection_note": source,
                    "formula": "projection - replacement + scarcity + lineup demand + market",
                },
            )
        )
    return result
