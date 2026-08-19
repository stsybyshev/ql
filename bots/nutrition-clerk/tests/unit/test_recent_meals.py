from __future__ import annotations

from datetime import date
from pathlib import Path

from nutrition_clerk.tools.recent_meals import build_recent_meals_tool

# A representative slice of the real MD schema: header + separator + rows.
SAMPLE_HEADER = """\
| Datetime         | Food                   | Qty | Unit    | Protein/u | Fat/u | Carbs/u | Kcal/u | Protein | Fat   | Carbs | Kcal  | Source       | Confidence |
|:-----------------|:-----------------------|----:|:--------|----------:|------:|--------:|-------:|--------:|------:|------:|------:|:-------------|:-----------|
"""


def _row(dt: str, food: str, kcal: float = 100.0, unit: str = "serving") -> str:
    return (
        f"| {dt:<16} | {food:<22} |   1 | {unit:<7} |       0.5 |   0.3 |     5.0 |   {kcal:>4} |     0.5 |   0.3 |   5.0 |   {kcal:>3} | cache_lookup | 0.95       |"
    )


def _write_month_file(log_dir: Path, when: date, rows: list[str]) -> Path:
    p = log_dir / f"{when.year:04d}-{when.month:02d}.md"
    body = SAMPLE_HEADER + "\n".join(rows) + "\n"
    p.write_text(body)
    return p


def _rows_from_tool(log_dir: Path, n: int) -> list[dict]:
    tool = build_recent_meals_tool(log_dir=log_dir)
    return tool.func(n=n)["entries"]


def test_returns_empty_when_no_files(tmp_path):
    assert _rows_from_tool(tmp_path, 5) == []


def test_parses_current_month_and_returns_latest(tmp_path):
    today = date.today()
    _write_month_file(tmp_path, today, [
        _row("01-08-2026 08:00", "Espresso"),
        _row("01-08-2026 12:30", "Salmon poke bowl", kcal=520),
        _row("02-08-2026 19:15", "Pomegranate juice", kcal=90, unit="cup"),
    ])
    entries = _rows_from_tool(tmp_path, 5)
    assert [e["food"] for e in entries] == [
        "Espresso",
        "Salmon poke bowl",
        "Pomegranate juice",
    ]
    # Newest is the last one — matches "chronological, oldest first" contract.
    assert entries[-1]["food"] == "Pomegranate juice"
    # Full per-unit fields are present (needed for add_personal_food).
    assert "kcal_per_unit" in entries[-1]
    assert "protein_per_unit" in entries[-1]
    assert entries[-1]["kcal_per_unit"] == 90.0


def test_n_caps_returned_entries(tmp_path):
    today = date.today()
    rows = [_row(f"0{i}-08-2026 08:00", f"Food {i}") for i in range(1, 6)]
    _write_month_file(tmp_path, today, rows)
    entries = _rows_from_tool(tmp_path, 2)
    assert len(entries) == 2
    # Should return the LAST two (most recent).
    assert [e["food"] for e in entries] == ["Food 4", "Food 5"]


def test_n_is_hard_capped_at_50(tmp_path):
    today = date.today()
    _write_month_file(tmp_path, today, [_row("01-08-2026 08:00", "X")])
    entries = build_recent_meals_tool(log_dir=tmp_path).func(n=9999)["entries"]
    assert len(entries) == 1  # only one row exists; cap doesn't manufacture rows


def test_ignores_malformed_rows(tmp_path):
    """Header/separator/comment lines must never sneak through as data."""
    today = date.today()
    p = tmp_path / f"{today.year:04d}-{today.month:02d}.md"
    p.write_text(
        SAMPLE_HEADER
        + "> comment: don't parse me\n"
        + _row("01-08-2026 08:00", "Real row")
        + "\n"
    )
    entries = _rows_from_tool(tmp_path, 10)
    assert [e["food"] for e in entries] == ["Real row"]
