from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Any

CSV_FIELDS = [
    "player_name",
    "team",
    "position",
    "overall_rank",
    "position_rank",
    "tier",
    "projection",
    "auction_value",
    "source",
    "bye_week",
    "draft_year",
    "draft_round",
    "age_at_week_1",
    "source_file",
]

PPR_PATTERN = re.compile(
    r"(?P<overall_rank>\d+)\.\s+"
    r"\((?P<position>DST|[A-Z]+)(?P<position_rank>\d+)\)\s+"
    r"(?P<player_name>[^\n]+?),\s+"
    r"(?P<team>[A-Z]{2,3})\s+"
    r"\$(?P<auction_value>\d+)\s+"
    r"(?P<bye_week>\d+)",
    re.MULTILINE,
)

DYNASTY_PATTERN = re.compile(
    r"(?P<overall_rank>\d+)\.\s+"
    r"\((?P<position>DST|[A-Z]+)(?P<position_rank>\d+)\)\s+"
    r"(?P<player_name>[^\n]+?),\s+"
    r"(?P<team>[A-Z]{2,3})\s*"
    r"(?P<draft_year>\d{4})-(?P<draft_round>\d+|U)\s+"
    r"(?P<age_years>\d+)-(?P<age_months>\d+)",
    re.MULTILINE,
)


def _position(value: str) -> str:
    return "DEF" if value == "DST" else "PK" if value == "K" else value


def _validate_ranks(rows: list[dict[str, str]], expected_count: int, label: str) -> None:
    ranks = sorted(int(row["overall_rank"]) for row in rows)
    expected = list(range(1, expected_count + 1))
    if ranks != expected:
        missing = sorted(set(expected) - set(ranks))
        duplicates = sorted(rank for rank in set(ranks) if ranks.count(rank) > 1)
        raise ValueError(
            f"{label}: expected ranks 1-{expected_count}; "
            f"found {len(rows)} rows, missing={missing[:10]}, duplicates={duplicates[:10]}"
        )


def parse_ppr_text(text: str, source_file: str) -> list[dict[str, str]]:
    rows_by_rank: dict[int, dict[str, str]] = {}
    for match in PPR_PATTERN.finditer(text):
        row = match.groupdict()
        overall_rank = int(row["overall_rank"])
        if not 1 <= overall_rank <= 300:
            continue
        rows_by_rank.setdefault(
            overall_rank,
            {
                "player_name": row["player_name"].strip(),
                "team": row["team"],
                "position": _position(row["position"]),
                "overall_rank": row["overall_rank"],
                "position_rank": row["position_rank"],
                "tier": "",
                "projection": "",
                "auction_value": row["auction_value"],
                "source": "ESPN 2026 PPR Top 300",
                "bye_week": row["bye_week"],
                "draft_year": "",
                "draft_round": "",
                "age_at_week_1": "",
                "source_file": source_file,
            },
        )
    rows = list(rows_by_rank.values())
    _validate_ranks(rows, 300, "PPR Top 300")
    return sorted(rows, key=lambda row: int(row["overall_rank"]))


def parse_dynasty_text(text: str, source_file: str) -> list[dict[str, str]]:
    rows_by_rank: dict[int, dict[str, str]] = {}
    for match in DYNASTY_PATTERN.finditer(text):
        row = match.groupdict()
        overall_rank = int(row["overall_rank"])
        if not 1 <= overall_rank <= 240:
            continue
        age = int(row["age_years"]) + int(row["age_months"]) / 12
        rows_by_rank.setdefault(
            overall_rank,
            {
                "player_name": row["player_name"].strip(),
                "team": row["team"],
                "position": _position(row["position"]),
                "overall_rank": row["overall_rank"],
                "position_rank": row["position_rank"],
                "tier": "",
                "projection": "",
                "auction_value": "",
                "source": "ESPN 2026 Dynasty Top 240",
                "bye_week": "",
                "draft_year": row["draft_year"],
                "draft_round": row["draft_round"],
                "age_at_week_1": f"{age:.2f}".rstrip("0").rstrip("."),
                "source_file": source_file,
            },
        )
    rows = list(rows_by_rank.values())
    _validate_ranks(rows, 240, "Dynasty Top 240")
    return sorted(rows, key=lambda row: int(row["overall_rank"]))


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "Install project requirements first: pip install -r requirements.txt"
        ) from exc
    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def convert(input_dir: Path, output_dir: Path) -> list[tuple[Path, int]]:
    jobs = [
        (
            input_dir / "NFL26_CS_PPR300.pdf",
            output_dir / "NFL26_CS_PPR300.csv",
            parse_ppr_text,
        ),
        (
            input_dir / "espnDynastyNFL26_CS_Dyn.pdf",
            output_dir / "espnDynastyNFL26_CS_Dyn.csv",
            parse_dynasty_text,
        ),
    ]
    results: list[tuple[Path, int]] = []
    for pdf_path, csv_path, parser in jobs:
        if not pdf_path.exists():
            raise FileNotFoundError(f"Ranking PDF not found: {pdf_path}")
        rows = parser(extract_pdf_text(pdf_path), pdf_path.name)
        write_csv(csv_path, rows)
        results.append((csv_path, len(rows)))
    return results


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Convert the two 2026 ranking PDFs to CSV.")
    parser.add_argument("--input-dir", type=Path, default=repository / "PDF")
    parser.add_argument("--output-dir", type=Path, default=repository / "CSV")
    args = parser.parse_args()
    for path, count in convert(args.input_dir, args.output_dir):
        print(f"Wrote {count} rows to {path}")


if __name__ == "__main__":
    main()
