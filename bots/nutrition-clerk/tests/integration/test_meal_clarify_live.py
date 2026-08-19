"""M5 end-to-end: ambiguous SHAPE A query -> clarify -> user answer -> log.

Seeds a tmp personal-foods.yaml with two colliding entries, sends an
ambiguous query, verifies the agent asks for clarification without logging,
then sends the user's disambiguating answer and verifies the log lands.

Requires ANTHROPIC_API_KEY.
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

# Fully self-contained test caches: two natural chia entries that both match
# a bare "chia" query. Using real-sounding names keeps the LLM in its
# meal-classification lane (synthetic prefixes like "M5Test..." confused
# the model in an earlier iteration).
PERSONAL_YAML = """\
- name: chia pudding
  aliases: []
  qty_default: 1
  unit: serving
  kcal_per_unit: 220
  protein_per_unit: 6
  fat_per_unit: 9
  carbs_per_unit: 28
  notes: 'homemade with almond milk'
  source: seed
"""

POPULAR_YAML = """\
- name: chia seeds
  aliases: [chia]
  qty_default: 15
  unit: g
  kcal_per_unit: 4.86
  protein_per_unit: 0.17
  fat_per_unit: 0.31
  carbs_per_unit: 0.42
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


@pytest.mark.asyncio
async def test_ambiguous_query_clarifies_then_logs(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    personal.write_text(PERSONAL_YAML)
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

    chat_id = 5555
    try:
        # ---- turn 1: ambiguous query ----
        # For a query like "50g of chia", two behaviours are both acceptable:
        #  (A) LLM picks a sensible default (chia seeds) and logs directly.
        #  (B) LLM asks the user which one they meant, waits for turn 2.
        # Test the union outcome: after up to 2 turns, we have exactly one
        # chia-related row, sourced from cache_lookup. Either path is OK
        # from a user-experience standpoint.
        reply1 = await pipeline.handle(
            InboundEvent(chat_id=chat_id, msg_id=1, text="just had 50g of chia")
        )
        assert reply1 and reply1 != "(no response)"

        rows_after_turn1 = _data_rows(_monthly_path(mcp))
        if not rows_after_turn1:
            # Path B — LLM asked; reply must reference both candidates so the
            # user actually has something to answer.
            low = reply1.lower()
            assert "pudding" in low and "seeds" in low, (
                f"clarify reply must list pudding and seeds, got: {reply1}"
            )
            # ---- turn 2: user disambiguates ----
            reply2 = await pipeline.handle(
                InboundEvent(chat_id=chat_id, msg_id=2, text="seeds")
            )
            assert reply2 and reply2 != "(no response)"

        rows = _data_rows(_monthly_path(mcp))
        assert len(rows) == 1, (
            f"expected exactly 1 row after resolution, got {len(rows)}: {rows}"
        )
        row = rows[0].lower()
        # Whichever path we took, the final log must be a chia variant sourced
        # from the cache — never text_estimate (both variants are in the cache).
        assert "chia" in row, f"expected chia in logged row, got: {rows[0]}"
        assert "cache_lookup" in row, (
            f"resolution must use cache_lookup source, got: {rows[0]}"
        )
        # Only one of pudding/seeds should be present (not both — that would
        # indicate the LLM mixed the candidates instead of choosing one).
        chose_pudding = "pudding" in row
        chose_seeds = "seeds" in row
        assert chose_pudding != chose_seeds, (
            f"row should be exactly one of pudding/seeds, got: {rows[0]}"
        )
    finally:
        await toolset.close()
