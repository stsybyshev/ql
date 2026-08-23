"""Declaring a fast logs a zero-macro marker row.

A live test on purpose: this is prompt behaviour, not orchestration. The
orchestration is already deterministic once the extractor emits a FASTING
entry — what needs pinning is that the extractor recognises a declared fast
as loggable at all, and does NOT invent one whenever the word appears.

23-08-2026: the clerk declined every phrasing ("I only handle food logging").
The cache had the marker row all along, but the extractor set
is_food_related=false because no food is named, so the decline fired before
any lookup. Veda's skill knew the convention; the rebased extractor prompt
did not carry it across.

Why the row matters: the user says this at the END of the day, and on a day
they actually fasted there is nothing else to log. Without a row the day is
indistinguishable from one they forgot about, so it drops out of averages
instead of counting as a real zero.
"""
from __future__ import annotations

import os
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import Config, MCPFoodSettings, MCPSettings  # noqa: E402
from nutrition_clerk.workflow import build_handler  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)

# The marker lives in the user's real personal cache; the fixture below seeds a
# minimal copy so the test doesn't depend on that file's current contents.
FASTING_ENTRY = """\
- name: FASTING
  aliases: [fasting, fasting day, fast day, water fast]
  qty_default: 1
  unit: day
  kcal_per_unit: 0
  protein_per_unit: 0.0
  fat_per_unit: 0.0
  carbs_per_unit: 0.0
  notes: "Marker row for intentional fasting days."
  source: learned

- name: egg
  aliases: [eggs, boiled egg, boiled eggs]
  qty_default: 2
  unit: egg
  kcal_per_unit: 72
  protein_per_unit: 6.3
  fat_per_unit: 4.8
  carbs_per_unit: 0.4
  source: seed
"""


def _build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    personal.write_text(FASTING_ENTRY)
    popular = tmp_path / "popular.yaml"
    popular.write_text("[]\n")
    config = Config()
    config.mcp = MCPSettings(food_tracker=MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=popular,
        food_log_dir=log_dir,
    ))
    handler, client = build_handler(config)
    return handler, client, log_dir


def _rows(log_dir: Path) -> list[str]:
    return [
        line
        for md in sorted(log_dir.glob("*.md"))
        for line in md.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", line)
    ]


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|")]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "Today is my fasting day",
    "I'm fasting today",
    "fasting",
])
async def test_declared_fast_logs_a_zero_marker(tmp_path, text):
    handler, client, log_dir = _build(tmp_path)
    try:
        reply = await handler(InboundEvent(chat_id=1, msg_id=1, text=text))
    finally:
        await client.close()

    assert "only handle food logging" not in reply.lower(), (
        f"a declared fast was declined: {reply!r}"
    )
    rows = _rows(log_dir)
    assert len(rows) == 1, f"expected exactly one marker row, got: {rows}"
    cells = _cells(rows[0])
    assert cells[2].upper() == "FASTING", f"wrong food name: {cells[2]!r}"
    # Every macro column must be zero — that is the whole point of the row.
    assert [float(cells[i]) for i in (9, 10, 11, 12)] == [0.0, 0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_breaking_a_fast_logs_the_food_not_a_marker(tmp_path):
    """"breaking my fast with 2 boiled eggs" is a meal, not a fast."""
    handler, client, log_dir = _build(tmp_path)
    try:
        await handler(InboundEvent(
            chat_id=1, msg_id=1, text="breaking my fast with 2 boiled eggs",
        ))
    finally:
        await client.close()

    foods = [_cells(r)[2].upper() for r in _rows(log_dir)]
    assert "FASTING" not in foods, f"a phantom fast row was created: {foods}"
    assert any("EGG" in f for f in foods), f"the eggs were not logged: {foods}"


@pytest.mark.asyncio
async def test_a_retrospective_fast_is_dated_to_that_day(tmp_path):
    """Said at the end of the day — and sometimes the day after."""
    handler, client, log_dir = _build(tmp_path)
    try:
        await handler(InboundEvent(chat_id=1, msg_id=1, text="I fasted yesterday"))
    finally:
        await client.close()

    rows = _rows(log_dir)
    assert len(rows) == 1, rows
    expected = (date.today() - timedelta(days=1)).strftime("%d-%m-%Y")
    assert _cells(rows[0])[1].startswith(expected), (
        f"expected the row dated {expected}, got {_cells(rows[0])[1]!r}"
    )
