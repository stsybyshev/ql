"""N4.5 end-to-end: typo/formatting variants land on the correct cache entry.

Seeds a tmp personal-foods.yaml with 'sundubu jigaye'. Sends messages with
typos and formatting mismatches — verifies the fuzzy fallback finds the
entry and logs it with source=cache_lookup and confidence=0.85 (fuzzy).

Requires ANTHROPIC_API_KEY for the extractor call (which just parses the
user text — the fuzzy lookup itself is deterministic Python).
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import pytest
import yaml

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


def _row_cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|")]


PERSONAL_SEED = [
    {
        "name": "sundubu jigaye",
        "aliases": [],
        "qty_default": 1,
        "unit": "serving",
        "kcal_per_unit": 350,
        "protein_per_unit": 20,
        "fat_per_unit": 15,
        "carbs_per_unit": 30,
        "notes": "spicy korean tofu stew",
        "source": "learned",
    },
]


def _build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    personal.write_text(yaml.safe_dump(PERSONAL_SEED))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text,expected_food_substring", [
    ("just had 1 ssundubu-jigaye",  "sundubu jigaye"),   # extra 's' + dash-vs-space
    ("had 1 serving of sundubu-jigaye", "sundubu jigaye"),  # only dash-vs-space
])
async def test_fuzzy_fallback_finds_typo_variants(tmp_path, user_text, expected_food_substring):
    handler, client, log_dir = _build(tmp_path)
    try:
        reply = await handler(InboundEvent(chat_id=1, msg_id=1, text=user_text))
    finally:
        await client.close()

    assert reply and reply != "(no response)"
    rows = _data_rows(_monthly_path(log_dir))
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    cells = _row_cells(rows[0])
    food = cells[2].lower()
    source = cells[13].lower()
    confidence = float(cells[14])
    assert expected_food_substring in food, (
        f"fuzzy fallback should have logged {expected_food_substring!r}; got: {food!r}"
    )
    assert source == "cache_lookup", f"expected cache_lookup, got {source}"
    # 0.85 confidence signals fuzzy fallback fired (vs 0.95 for exact substring).
    assert confidence == 0.85, (
        f"expected fuzzy confidence 0.85, got {confidence} — did the fallback path fire?"
    )
