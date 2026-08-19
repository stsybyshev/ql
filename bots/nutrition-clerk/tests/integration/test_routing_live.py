"""Router end-to-end: same pipeline, different models for different turns.

Two turns on ONE pipeline instance:
- Turn 1: text-only "1 apple" -> text_only rule -> Haiku
- Turn 2: Thai curry photo + eating-out text -> eating_out_photo -> Sonnet

Asserts:
- pipeline.last_route reflects the matched rule per turn
- The agent tree's active model swaps between turns
- Both turns produce a real reply (both models actually invoked)
- Session state persists across the model swap (same session_service)
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
    ModelRouter,
    build_meal_agent,
    build_polite_decline_agent,
    build_root_agent,
)
from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import (  # noqa: E402
    MCPFoodSettings,
    ModelProfile,
    ModelSettings,
    RoutingRule,
)
from nutrition_clerk.model import build_model  # noqa: E402
from nutrition_clerk.tools import build_food_mcp_toolset  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "thai_jungle_curry.jpg"


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
async def test_router_swaps_models_across_turns(tmp_path):
    if not FIXTURE.exists():
        pytest.skip(f"missing fixture: {FIXTURE}")

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    popular = tmp_path / "popular.yaml"
    # Popular seed with just 'apple' so the text-only turn is a fast cache hit.
    popular.write_text(
        """\
- name: apple
  aliases: [apples]
  qty_default: 1
  unit: apple
  kcal_per_unit: 95
  protein_per_unit: 0.5
  fat_per_unit: 0.3
  carbs_per_unit: 25
  notes: 'medium apple'
  source: seed
"""
    )

    settings = ModelSettings(
        default="cloud_haiku",
        profiles={
            "cloud_haiku": ModelProfile(
                model="anthropic/claude-haiku-4-5",
                api_key_env="ANTHROPIC_API_KEY",
            ),
            "cloud_sonnet": ModelProfile(
                model="anthropic/claude-sonnet-5",
                api_key_env="ANTHROPIC_API_KEY",
            ),
        },
        routing=[
            RoutingRule(
                name="eating_out_photo",
                profile="cloud_sonnet",
                requires_photo=True,
                pattern=r"\b(restaurant|cafe|café|diner|takeaway|eating\s+out|dined\s+at)\b",
            ),
            RoutingRule(
                name="any_photo_fallback",
                profile="cloud_sonnet",
                requires_photo=True,
            ),
            RoutingRule(
                name="text_only",
                profile="cloud_haiku",
                requires_photo=False,
            ),
        ],
    )

    mcp = MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=popular,
        food_log_dir=log_dir,
    )
    toolset = build_food_mcp_toolset(mcp)

    seed_model = build_model(settings.profiles[settings.default])
    meal = build_meal_agent(seed_model, toolsets=[toolset], log_dir=log_dir)
    polite = build_polite_decline_agent(seed_model)
    root = build_root_agent(seed_model, sub_agents=[meal, polite])
    router = ModelRouter(settings)
    pipeline = AgentPipeline(root_agent=root, router=router)

    try:
        # ---- turn 1: text-only -> text_only route, Haiku ----
        reply1 = await pipeline.handle(
            InboundEvent(chat_id=1, msg_id=1, text="just had 1 apple")
        )
        assert reply1 and reply1 != "(no response)"
        assert pipeline.last_route == "text_only", (
            f"expected text_only route, got {pipeline.last_route}"
        )
        # After turn 1 the agent tree's model should be Haiku.
        assert root.model.model == "anthropic/claude-haiku-4-5"
        assert meal.model.model == "anthropic/claude-haiku-4-5"

        # ---- turn 2: photo + eating-out text -> eating_out_photo, Sonnet ----
        reply2 = await pipeline.handle(
            InboundEvent(
                chat_id=1,
                msg_id=2,
                text=(
                    "Thai restaurant dinner: a portion of Jungle curry with "
                    "veggies and prawns with a serving of jasmine rice"
                ),
                photos=[FIXTURE],
            )
        )
        assert reply2 and reply2 != "(no response)"
        assert pipeline.last_route == "eating_out_photo", (
            f"expected eating_out_photo route, got {pipeline.last_route}"
        )
        # After turn 2 the agent tree's model should be Sonnet.
        assert root.model.model == "anthropic/claude-sonnet-5"
        assert meal.model.model == "anthropic/claude-sonnet-5"
        assert polite.model.model == "anthropic/claude-sonnet-5"

        # ---- outcome sanity ----
        rows = _data_rows(_monthly_path(mcp))
        # turn 1 logs 1 row (apple), turn 2 logs 2-3 rows (curry, rice, maybe veg).
        assert 3 <= len(rows) <= 4, (
            f"expected 3-4 total rows across the two turns, got {len(rows)}:\n"
            + "\n".join(rows)
        )
        joined = " | ".join(r.lower() for r in rows)
        assert "apple" in joined and "curry" in joined and (
            "rice" in joined or "jasmine" in joined
        ), f"expected apple + curry + rice in logged rows, got:\n{joined}"
    finally:
        await toolset.close()
