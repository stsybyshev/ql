"""Real-photo SHAPE D test: Stan's Pixel 9 Pro shot of a Thai jungle curry
+ jasmine rice at a restaurant.

Unlike the synthetic SHAPE D test, this one uses a real dish photo — the
whole point is to verify the pipeline against something that looks like
what a Telegram user actually sends.

We assert STRUCTURE, not specific macro values. Vision-based portion
estimation is noisy and drifts run-to-run; tracking numeric drift belongs
in the M8 eval suite (see tests/agent_evals/meal_agent.evalset.json).

Reference values, for context:
  ChatGPT Sol 5.6 estimate: 650 kcal · 35P · 15F · 100C total
  Haiku 4.5 (2026-08-09):   ~1100 kcal · ~50P · ~60F · ~85C total
    -> systematically over on kcal/fat, close on carbs.
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

FIXTURE = Path(__file__).parent.parent / "fixtures" / "thai_jungle_curry.jpg"
USER_TEXT = (
    "Thai restaurant dinner: a portion of Jungle curry with veggies and "
    "prawns with a serving of jasmine rice"
)


def _monthly_path(mcp: MCPFoodSettings) -> Path:
    today = date.today()
    return mcp.food_log_dir / f"{today.year:04d}-{today.month:02d}.md"


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|")]


def _data_rows(md_path: Path) -> list[str]:
    if not md_path.exists():
        return []
    return [
        line
        for line in md_path.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", line)
    ]


@pytest.mark.asyncio
async def test_real_thai_dinner_photo_logs_two_dishes(tmp_path):
    assert FIXTURE.exists(), f"missing fixture: {FIXTURE}"

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    popular = tmp_path / "popular.yaml"
    popular.write_text("[]\n")

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

    try:
        reply = await pipeline.handle(
            InboundEvent(chat_id=1, msg_id=8080, text=USER_TEXT, photos=[FIXTURE])
        )
    finally:
        await toolset.close()

    assert reply and reply != "(no response)"

    rows = _data_rows(_monthly_path(mcp))
    # Two dishes named -> two rows expected. Allow 2-3 (LLM might split
    # "curry with prawns and veggies" into components).
    assert 2 <= len(rows) <= 3, (
        f"expected 2-3 rows for a 2-dish photo, got {len(rows)}:\n"
        + "\n".join(rows)
    )

    foods = [_cells(r)[2].lower() for r in rows]
    sources = [_cells(r)[13].lower() for r in rows]
    kcals = [float(_cells(r)[12]) for r in rows]

    # Every row is a meal-photo estimate (not label, not cache — the popular
    # cache is empty).
    assert all(s == "photo_estimate" for s in sources), (
        f"real-meal photo rows must all use photo_estimate; got: "
        f"{list(zip(foods, sources))}"
    )
    # Both dishes named by the user must appear in at least one row.
    joined = " | ".join(foods)
    assert "curry" in joined, f"expected 'curry' in rows, got: {foods}"
    assert "rice" in joined or "jasmine" in joined, (
        f"expected rice/jasmine in rows, got: {foods}"
    )
    # No zero-kcal drift — every row must have a real macro estimate.
    assert all(k > 0 for k in kcals), f"zero-kcal row appeared: {list(zip(foods, kcals))}"

    # Sanity band on total kcal — a real Thai dinner sits somewhere between
    # a light 400 kcal lunch and a heavy 1500 kcal blowout. Anything outside
    # this range means vision has gone off the rails (bug, prompt regression,
    # unit misuse).
    total_kcal = sum(kcals)
    assert 400 <= total_kcal <= 1600, (
        f"total kcal {total_kcal} outside plausible Thai-dinner band. "
        f"Reference: ChatGPT Sol 5.6=650, prior Haiku~1100. Rows: "
        f"{list(zip(foods, kcals))}"
    )
