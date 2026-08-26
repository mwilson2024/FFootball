from __future__ import annotations

import csv
import json

from scripts.convert_pff_projection_json import convert


def test_pff_json_converter_preserves_projection_range_and_analysis(tmp_path) -> None:
    source = tmp_path / "pff.json"
    output = tmp_path / "pff.csv"
    source.write_text(
        json.dumps(
            {
                "lastUpdatedAt": 1787754048.803565,
                "rankings": [
                    {
                        "id": 122474,
                        "playerId": 122474,
                        "firstName": "Jahmyr",
                        "lastName": "Gibbs",
                        "position": "RB",
                        "teamAbbreviation": "DET",
                        "byeWeek": 6,
                        "adpScoringType": "PPR",
                        "adp": 1.4,
                        "tags": ["target"],
                        "tier": 1,
                        "rank": {"rankerName": "Nathan", "current": 1, "position": 1},
                        "projection": {
                            "points": {"high": 445.38, "low": 240, "mid": 342.69}
                        },
                        "expertAnalysis": ["Elite workload.", "Strong offense."],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert convert(source, output, minimum_rows=1) == 1
    row = next(csv.DictReader(output.open(encoding="utf-8")))

    assert row["player_name"] == "Jahmyr Gibbs"
    assert row["season_projection"] == "342.690"
    assert row["projection_floor"] == "240.000"
    assert row["projection_ceiling"] == "445.380"
    assert row["pff_player_id"] == "122474"
    assert row["expert_analysis"] == "Elite workload.\n\nStrong offense."
