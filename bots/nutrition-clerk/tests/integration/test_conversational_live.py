"""Real-world conversational message test.

Sends a natural-language breakfast log with:
- conversational filler ("Just had my usual breakfast with...")
- multiple entries (espresso, MCT oil, banana, cashews)
- mixed units (implicit qty, tsp, count, grams)
- one entry NOT in the cache (MCT oil) that should trigger the
  llm_estimate fallback rather than a hard decline.

Requires ANTHROPIC_API_KEY. Uses fully-synthetic tmp caches so behaviour is
independent of the live personal-foods file.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import pytest

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

# Seed cache with common items Stan actually eats. MCT oil is deliberately
# omitted so we test the SHAPE A -> llm_estimate fallback.
POPULAR_YAML = """\
- name: espresso
  aliases: [coffee, espresso shot]
  qty_default: 1
  unit: cup
  kcal_per_unit: 2
  protein_per_unit: 0.3
  fat_per_unit: 0.0
  carbs_per_unit: 0.2
  notes: 'single shot'
  source: seed
- name: banana
  aliases: [bananas]
  qty_default: 1
  unit: banana
  kcal_per_unit: 105
  protein_per_unit: 1.3
  fat_per_unit: 0.4
  carbs_per_unit: 27.0
  notes: 'medium banana ~118g'
  source: seed
- name: cashews
  aliases: [cashew, cashew nuts]
  qty_default: 100
  unit: g
  kcal_per_unit: 5.53
  protein_per_unit: 0.18
  fat_per_unit: 0.44
  carbs_per_unit: 0.30
  notes: 'per gram'
  source: seed
"""


def _monthly_path(mcp: MCPFoodSettings) -> Path:
    today = date.today()
    return mcp.food_log_dir / f"{today.year:04d}-{today.month:02d}.md"


def _data_rows(md_path: Path) -> list[str]:
    if not md_path.exists():
        return []
    return [
        line
        for line in md_path.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", line)
    ]


def _row_food(row: str) -> str:
    # `| DD-MM-YYYY HH:MM | Food                   |   1 | ...`
    cells = [c.strip() for c in row.split("|")]
    # cells[0] is empty (leading '|'); cells[1] is Datetime; cells[2] is Food.
    return cells[2].lower() if len(cells) > 2 else ""


def _row_source(row: str) -> str:
    cells = [c.strip() for c in row.split("|")]
    # Source is second-to-last cell (last is Confidence + trailing empty).
    return cells[-3].lower() if len(cells) >= 3 else ""


@pytest.mark.asyncio
async def test_conversational_breakfast_multi_entry(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"  # not seeded — created on save if needed
    popular = tmp_path / "popular.yaml"
    popular.write_text(POPULAR_YAML)

    mcp = MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=popular,
        food_log_dir=log_dir,
    )
    toolset = build_food_mcp_toolset(mcp)
    model = build_model(ModelSettings().profiles["cloud_haiku"])
    meal = build_meal_agent(model, toolsets=[toolset], log_dir=log_dir)
    polite = build_polite_decline_agent(model)
    root = build_root_agent(model, sub_agents=[meal, polite])
    pipeline = AgentPipeline(root_agent=root)

    text = (
        "Just had my usual breakfast with espresso, 1 tsp of MCT 8 oil, "
        "1 banana, 50g of cashew nuts"
    )
    try:
        reply = await pipeline.handle(
            InboundEvent(chat_id=1, msg_id=42, text=text)
        )
    finally:
        await toolset.close()

    assert reply and reply != "(no response)"

    rows = _data_rows(_monthly_path(mcp))
    # We expect 4 entries. Allow 3 (LLM might merge espresso with something)
    # or 4; strictly assert not <3 and not >5 to avoid over-fitting on run
    # variance while still catching real breakages.
    assert 3 <= len(rows) <= 5, (
        f"expected 3-5 rows for a 4-entry conversational log, got {len(rows)}:\n"
        + "\n".join(rows)
    )

    foods = [_row_food(r) for r in rows]
    sources = [_row_source(r) for r in rows]

    # Cached items landed with cache_lookup source.
    def _in_any(term: str) -> bool:
        return any(term in f for f in foods)

    assert _in_any("banana"), f"banana missing from rows: {foods}"
    assert _in_any("cashew"), f"cashews missing from rows: {foods}"

    # MCT oil should be there via text_estimate (SHAPE A miss fallback) —
    # the whole point of the relax. Production schema uses text_estimate for
    # both user-typed and LLM-guessed values (distinguished by confidence).
    mct_rows = [(f, s) for f, s in zip(foods, sources) if "mct" in f or "coconut" in f]
    assert mct_rows, (
        f"MCT oil should be logged (text_estimate fallback), not declined. Rows: {list(zip(foods, sources))}"
    )
    mct_food, mct_source = mct_rows[0]
    assert mct_source == "text_estimate", (
        f"MCT oil is not in seeded cache — must use text_estimate, got source={mct_source}"
    )

    # Cached entries must NOT have been re-classified as llm_estimate.
    for f, s in zip(foods, sources):
        if "banana" in f or "cashew" in f or "espresso" in f:
            assert s == "cache_lookup", (
                f"cached food {f!r} logged with wrong source {s!r}"
            )

    # personal-foods must NOT have been mutated (no save requested, and
    # llm_estimates should never seed the cache silently).
    assert not personal.exists() or personal.read_text().strip() == "", (
        f"personal-foods was mutated: {personal.read_text()}"
    )
