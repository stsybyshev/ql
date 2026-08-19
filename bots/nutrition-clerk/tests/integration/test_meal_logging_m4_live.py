"""M4 end-to-end: typed macros + save-to-cache.

Requires ANTHROPIC_API_KEY. Uses a tmp copy of personal-foods.yaml so
the save actually mutates a file we then inspect.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

pytest.importorskip("google.adk")

from nutrition_clerk.agents import (  # noqa: E402
    AgentPipeline,
    build_meal_agent,
    build_polite_decline_agent,
    build_root_agent,
)
from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import MCPFoodSettings, ModelSettings  # noqa: E402
from nutrition_clerk.model import build_model  # noqa: E402
from nutrition_clerk.tools import build_food_mcp_toolset  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)

REAL_POPULAR = Path(
    "/home/stan/.openclaw/workspace/skills/openclaw-food-tracker/references/popular-foods.yaml"
)
FOOD_NAME = "M4Test Chia Pudding"  # unlikely to collide with real personal cache


def _build_pipeline_and_settings(tmp_path):
    """Assemble a real ADK pipeline pointed at tmp state, return (pipeline, toolset, mcp)."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    # Do NOT pre-create personal.yaml: append_personal_food appends yaml.dump([...])
    # and would produce an invalid multi-document file if we seeded it with `[]`.
    # load_yaml handles missing files (returns []); the MCP creates it on first save.
    personal = tmp_path / "personal.yaml"

    mcp = MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=REAL_POPULAR,
        food_log_dir=log_dir,
    )
    toolset = build_food_mcp_toolset(mcp)
    model = build_model(ModelSettings().profiles["cloud_haiku"])
    meal = build_meal_agent(model, toolsets=[toolset], log_dir=log_dir)
    polite = build_polite_decline_agent(model)
    root = build_root_agent(model, sub_agents=[meal, polite])
    return AgentPipeline(root_agent=root), toolset, mcp


def _monthly_path(mcp: MCPFoodSettings) -> Path:
    today = date.today()
    return mcp.food_log_dir / f"{today.year:04d}-{today.month:02d}.md"


def _data_rows(md_path: Path) -> list[str]:
    return [
        line
        for line in md_path.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", line)
    ]


@pytest.mark.asyncio
async def test_typed_macros_with_save_end_to_end(tmp_path):
    if not REAL_POPULAR.exists():
        pytest.skip(f"missing popular-foods.yaml at {REAL_POPULAR}")
    pipeline, toolset, mcp = _build_pipeline_and_settings(tmp_path)
    text = f"{FOOD_NAME} 300 kcal 12P 20F 8C, save it"

    try:
        reply = await pipeline.handle(
            InboundEvent(chat_id=1, msg_id=4444, text=text)
        )
    finally:
        await toolset.close()

    assert reply and reply != "(no response)"

    # 1. One row logged, source=text_estimate.
    md_path = _monthly_path(mcp)
    assert md_path.exists(), f"no monthly log at {md_path}"
    rows = _data_rows(md_path)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}"
    row = rows[0].lower()
    assert "text_estimate" in row, f"expected source=text_estimate, got: {rows[0]}"
    assert "cache_lookup" not in row, f"SHAPE B must NOT write cache_lookup: {rows[0]}"
    # 300 kcal appears as the total.
    assert "300" in row, f"expected 300 kcal in row, got: {rows[0]}"

    # 2. Personal-foods was mutated by add_personal_food.
    saved = yaml.safe_load(mcp.personal_foods_path.read_text()) or []
    assert isinstance(saved, list), f"personal-foods.yaml must be a list, got {type(saved)}"
    names = [entry.get("name", "").lower() for entry in saved]
    assert any(FOOD_NAME.lower() in n or n in FOOD_NAME.lower() for n in names), (
        f"expected {FOOD_NAME!r} to appear in personal-foods, got names: {names}"
    )
    # Find the saved entry and verify macros made it through.
    match = next(
        e for e in saved if FOOD_NAME.lower() in e.get("name", "").lower()
        or e.get("name", "").lower() in FOOD_NAME.lower()
    )
    assert match["kcal_per_unit"] == 300
    assert match["protein_per_unit"] == 12
    assert match["fat_per_unit"] == 20
    assert match["carbs_per_unit"] == 8
    # Per prompt: unit=serving, qty_default=1 for shape-B saves.
    assert match["unit"] == "serving"


@pytest.mark.asyncio
async def test_typed_macros_without_save_does_not_touch_cache(tmp_path):
    """Shape B without 'save it' must LOG but NOT write to personal-foods.yaml."""
    if not REAL_POPULAR.exists():
        pytest.skip(f"missing popular-foods.yaml at {REAL_POPULAR}")
    pipeline, toolset, mcp = _build_pipeline_and_settings(tmp_path)

    try:
        reply = await pipeline.handle(
            InboundEvent(
                chat_id=1,
                msg_id=4445,
                text="Random Snack 150 kcal 5P 8F 12C",
            )
        )
    finally:
        await toolset.close()

    assert reply and reply != "(no response)"

    rows = _data_rows(_monthly_path(mcp))
    assert len(rows) == 1, f"expected 1 row, got: {rows}"
    assert "text_estimate" in rows[0].lower()

    # Without save-it, the MCP should never open personal-foods.yaml for write,
    # so the file must remain absent (we did not seed it).
    assert not mcp.personal_foods_path.exists(), (
        f"personal-foods.yaml must not exist without save-it, got: "
        f"{mcp.personal_foods_path.read_text()}"
    )
