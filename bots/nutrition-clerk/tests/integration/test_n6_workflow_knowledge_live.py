"""N6 end-to-end: knowledge enricher for cache misses + rich datetime hints.

Empty caches, so every food is a miss and the knowledge enricher is the only
path to a logged row. Verifies:
- Common foods (banana) get estimated and logged with source=text_estimate.
- Branded/unknown foods are refused, not invented.
- Natural time phrasing ("this morning") lands on the right timestamp.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import Config, MCPFoodSettings, MCPSettings  # noqa: E402
from nutrition_clerk.workflow import build_handler  # noqa: E402
from nutrition_clerk.workflow import context as chat_context  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)


@pytest.fixture(autouse=True)
def _reset_context():
    chat_context._reset_all()
    yield
    chat_context._reset_all()


def _monthly_path(log_dir: Path) -> Path:
    today = date.today()
    return log_dir / f"{today.year:04d}-{today.month:02d}.md"


def _data_rows(md: Path) -> list[str]:
    if not md.exists():
        return []
    return [
        l for l in md.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", l)
    ]


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|")]


def _build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    popular = tmp_path / "popular.yaml"
    popular.write_text("[]\n")     # empty -> every lookup misses
    config = Config()
    config.mcp = MCPSettings(food_tracker=MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=popular,
        food_log_dir=log_dir,
    ))
    handler, client = build_handler(config)
    return handler, client, log_dir, personal


@pytest.mark.asyncio
async def test_common_food_gets_knowledge_estimate(tmp_path):
    """'1 banana' isn't cached — the knowledge enricher should estimate it."""
    handler, client, log_dir, personal = _build(tmp_path)
    try:
        reply = await handler(InboundEvent(chat_id=1, msg_id=1, text="just had 1 banana"))
    finally:
        await client.close()

    assert reply and reply != "(no response)"
    rows = _data_rows(_monthly_path(log_dir))
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    cells = _cells(rows[0])
    food = cells[2].lower()
    kcal_total = float(cells[12])
    source = cells[13].lower()
    confidence = float(cells[14])

    assert "banana" in food
    assert source == "text_estimate", f"expected text_estimate, got {source}"
    # Knowledge estimates use the model's own 0.4-0.7 confidence band.
    assert 0.3 <= confidence <= 0.75, f"confidence {confidence} outside estimate band"
    # A medium banana is ~90-130 kcal. Wide band — this catches structural
    # breakage (zero, or 100x) without flaking on model variance.
    assert 70 <= kcal_total <= 160, f"implausible banana kcal: {kcal_total}"

    # Estimates must never seed the personal cache.
    assert not personal.exists() or personal.read_text().strip() == "", (
        "knowledge estimates must not be written to personal-foods"
    )


@pytest.mark.asyncio
async def test_branded_food_is_refused_not_invented(tmp_path):
    """Branded product -> model should decline, and we ask for macros/photo."""
    handler, client, log_dir, personal = _build(tmp_path)
    try:
        reply = await handler(InboundEvent(
            chat_id=1, msg_id=1,
            text="just had a Trader Joe's Sriracha Kimchi Rice Bowl",
        ))
    finally:
        await client.close()

    assert reply and reply != "(no response)"
    # Nothing logged — we must not invent macros for a branded product.
    rows = _data_rows(_monthly_path(log_dir))
    assert rows == [], f"branded product should not be logged, got: {rows}"
    # Reply should point the user at macros or a label photo.
    low = reply.lower()
    assert "macro" in low or "label" in low or "photo" in low, (
        f"expected a 'send macros or a label photo' style reply; got: {reply!r}"
    )


@pytest.mark.asyncio
async def test_datetime_hint_this_morning(tmp_path):
    """'this morning' should land on today at the morning hour, not now()."""
    handler, client, log_dir, personal = _build(tmp_path)
    try:
        reply = await handler(InboundEvent(
            chat_id=1, msg_id=1, text="this morning I had 2 boiled eggs",
        ))
    finally:
        await client.close()

    assert reply and reply != "(no response)"
    rows = _data_rows(_monthly_path(log_dir))
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    dt_str = _cells(rows[0])[1]
    logged = datetime.strptime(dt_str, "%d-%m-%Y %H:%M")
    assert logged.date() == date.today(), f"expected today's date, got {dt_str}"
    # Morning-ish: our resolver maps "morning"->09:00, "breakfast"->08:00.
    assert 6 <= logged.hour <= 11, (
        f"expected a morning hour for 'this morning', got {logged.hour} ({dt_str})"
    )
