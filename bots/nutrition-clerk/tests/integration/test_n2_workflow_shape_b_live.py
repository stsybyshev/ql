"""N2: SHAPE B (user-typed macros) + save_to_cache, through the new workflow.

Same asserts as the archived M4 test — row lands with source=text_estimate;
personal-foods.yaml gets a new entry when 'save it' is present; extractor
token cost stays under budget.
"""
from __future__ import annotations

import logging
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

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)

REAL_POPULAR = Path(
    "/home/stan/.openclaw/workspace/skills/openclaw-food-tracker/references/popular-foods.yaml"
)
# Natural food name — LLM strips prefixes like "N2Test" as looking-like-a-test-marker.
# Empty popular_foods.yaml (tmp) ensures the lookup misses so save can actually fire.
FOOD_NAME = "Chia pudding"


def _monthly_path(log_dir: Path) -> Path:
    today = date.today()
    return log_dir / f"{today.year:04d}-{today.month:02d}.md"


def _data_rows(md_path: Path) -> list[str]:
    if not md_path.exists():
        return []
    return [
        line
        for line in md_path.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", line)
    ]


def _build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"          # do NOT pre-create (append semantics)
    # Empty popular-foods so the food name (whatever the LLM extracts) can't hit
    # a cache entry and be classified as SHAPE A. We rely on `kcal is not None`
    # to trigger SHAPE B in the orchestrator, which skips lookup anyway — but
    # using an empty popular ensures we don't accidentally save into a real
    # user's food cache.
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
async def test_shape_b_typed_macros_with_save(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="nutrition_clerk.workflow.extractor")
    if not REAL_POPULAR.exists():
        pytest.skip(f"missing popular-foods.yaml at {REAL_POPULAR}")

    handler, client, log_dir, personal = _build(tmp_path)
    text = f"{FOOD_NAME} 300 kcal 12P 20F 8C, save it"
    try:
        reply = await handler(InboundEvent(chat_id=1, msg_id=1, text=text))
    finally:
        await client.close()

    assert reply and reply != "(no response)"

    # 1. One row logged with source=text_estimate
    rows = _data_rows(_monthly_path(log_dir))
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    row = rows[0].lower()
    assert "text_estimate" in row
    assert "cache_lookup" not in row
    assert "300" in row

    # 2. personal-foods.yaml has the new entry (fuzzy name match — LLM may
    #    title-case or slightly rephrase the food name)
    assert personal.exists(), "personal-foods.yaml should exist after save"
    saved = yaml.safe_load(personal.read_text()) or []
    names = [e.get("name", "").lower() for e in saved]
    match = next(
        (e for e in saved if "chia" in e.get("name", "").lower()
                          and "pudding" in e.get("name", "").lower()),
        None,
    )
    assert match is not None, f"expected chia-pudding entry in personal-foods, got names: {names}"
    assert match["kcal_per_unit"] == 300
    assert match["protein_per_unit"] == 12
    assert match["fat_per_unit"] == 20
    assert match["carbs_per_unit"] == 8
    assert match["unit"] == "serving"

    # 3. Reply notes the save
    assert "saved" in reply.lower(), f"expected 'saved' note in reply, got: {reply}"

    # 4. Token budget still holds (SHAPE B prompt is a bit longer than N1's SHAPE A)
    lines = [r.getMessage() for r in caplog.records if "extractor: in=" in r.getMessage()]
    assert lines
    m = re.search(r"in=(\d+)", lines[-1])
    tokens_in = int(m.group(1))
    print(f"\n[N2 SPIKE] extractor tokens_in={tokens_in} (target <5000)")
    assert tokens_in < 5000, f"tokens_in {tokens_in} exceeds N2 budget of 5000"


@pytest.mark.asyncio
async def test_shape_b_without_save_does_not_touch_cache(tmp_path):
    if not REAL_POPULAR.exists():
        pytest.skip(f"missing popular-foods.yaml at {REAL_POPULAR}")
    handler, client, log_dir, personal = _build(tmp_path)
    try:
        reply = await handler(InboundEvent(
            chat_id=1, msg_id=2,
            text="Random Snack 150 kcal 5P 8F 12C",
        ))
    finally:
        await client.close()

    assert reply and reply != "(no response)"
    rows = _data_rows(_monthly_path(log_dir))
    assert len(rows) == 1, f"expected 1 row, got: {rows}"
    assert "text_estimate" in rows[0].lower()
    assert not personal.exists(), (
        f"personal-foods should NOT exist without 'save it', got: "
        f"{personal.read_text() if personal.exists() else '(no file)'}"
    )
    assert "saved" not in reply.lower(), f"reply should not claim save: {reply}"
