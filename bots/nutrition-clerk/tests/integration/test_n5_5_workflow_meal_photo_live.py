"""N5.5 — SHAPE D: photo of a plated meal + text naming the dishes.

Ported from the old-pipeline test_real_photo_thai_curry_live.py onto the
new workflow handler.

Uses Stan's real Pixel 9 Pro photo of a Thai jungle curry + jasmine rice.

Assertions are STRUCTURAL, not numeric: vision portion estimation is
inherently noisy and drifts between model versions. Numeric drift belongs
in the eval set (tests/agent_evals/), not here.

Reference totals for this dish, for context:
  ChatGPT Sol 5.6 estimate : 650 kcal · 35P · 15F · 100C
  Old ADK pipeline (Haiku) : ~1091 kcal (over-estimated the curry)
  New workflow (Haiku)     : ~470 kcal
The three disagree by ~2x, which is the honest state of photo-based portion
estimation. The band below only catches structural breakage.
"""
from __future__ import annotations

import os
import re
from datetime import date
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

FIXTURE = Path(__file__).parent.parent / "fixtures" / "thai_jungle_curry.jpg"
USER_TEXT = (
    "Thai restaurant dinner: a portion of Jungle curry with veggies and "
    "prawns with a serving of jasmine rice"
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
    popular.write_text("[]\n")
    config = Config()
    config.mcp = MCPSettings(food_tracker=MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=popular,
        food_log_dir=log_dir,
    ))
    handler, client = build_handler(config)
    return handler, client, log_dir, personal


@pytest.mark.asyncio
async def test_real_thai_meal_photo_logs_per_dish_rows(tmp_path):
    assert FIXTURE.exists(), f"missing fixture: {FIXTURE}"
    handler, client, log_dir, personal = _build(tmp_path)
    try:
        reply = await handler(
            InboundEvent(chat_id=1, msg_id=1, text=USER_TEXT, photos=[FIXTURE])
        )
    finally:
        await client.close()

    assert reply and reply != "(no response)"

    rows = _data_rows(_monthly_path(log_dir))
    # Two dishes named. Allow 2-3 (the model may split "curry with veggies
    # and prawns" into components).
    assert 2 <= len(rows) <= 3, (
        f"expected 2-3 dish rows, got {len(rows)}:\n" + "\n".join(rows)
    )

    foods = [_cells(r)[2].lower() for r in rows]
    sources = [_cells(r)[13].lower() for r in rows]
    confidences = [float(_cells(r)[14]) for r in rows]
    kcals = [float(_cells(r)[12]) for r in rows]

    # Meal photos must use photo_estimate — never photo_label (that's for
    # nutrition panels) and never cache_lookup (cache is empty here).
    assert all(s == "photo_estimate" for s in sources), (
        f"meal-photo rows must all use photo_estimate; got {list(zip(foods, sources))}"
    )
    # Visual portion estimates stay in the low-confidence band.
    assert all(0.25 <= c <= 0.55 for c in confidences), (
        f"photo_estimate confidences outside 0.25-0.55: {confidences}"
    )
    # Both dishes the user named should appear.
    joined = " | ".join(foods)
    assert "curry" in joined, f"expected 'curry' in rows, got {foods}"
    assert "rice" in joined or "jasmine" in joined, f"expected rice in rows, got {foods}"
    # No zero-kcal rows.
    assert all(k > 0 for k in kcals), f"zero-kcal row: {list(zip(foods, kcals))}"
    # Plausible dinner band — catches structural breakage (100x, zero) without
    # flaking on the genuine ~2x spread between estimators.
    total = sum(kcals)
    assert 300 <= total <= 1600, (
        f"total {total} kcal outside plausible Thai-dinner band. "
        f"Refs: ChatGPT 650, old pipeline ~1091. Rows: {list(zip(foods, kcals))}"
    )

    # Photo estimates must never seed the personal cache.
    assert not personal.exists() or personal.read_text().strip() == "", (
        "photo estimates must not be written to personal-foods"
    )


@pytest.mark.asyncio
async def test_meal_photo_does_not_crash_the_turn(tmp_path):
    """Regression guard for the bug this milestone fixed.

    Before N5.5 every photo went to the label extractor. A meal photo made
    the model return nulls, LabelExtract validation raised, the turn failed,
    the Telegram offset was never committed — so the message redelivered
    forever. The handler must ALWAYS return a string.
    """
    assert FIXTURE.exists()
    handler, client, log_dir, _ = _build(tmp_path)
    try:
        reply = await handler(
            InboundEvent(chat_id=1, msg_id=1, text=USER_TEXT, photos=[FIXTURE])
        )
    finally:
        await client.close()
    assert isinstance(reply, str) and reply.strip(), (
        "handler must return a non-empty reply for a meal photo, never raise"
    )
