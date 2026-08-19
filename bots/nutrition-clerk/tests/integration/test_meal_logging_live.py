"""End-to-end M3 test: real MCP + real LLM logs '1 apple' to a tmp MD file.

Requires ANTHROPIC_API_KEY. Uses the real popular-foods.yaml (which contains
'apple') and a copy of personal-foods.yaml so the test can't accidentally
mutate live state. FOOD_LOG_DIR points at tmp so the test file is isolated.
"""
from __future__ import annotations

import os
import re
import shutil
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

REAL_POPULAR = Path(
    "/home/stan/.openclaw/workspace/skills/openclaw-food-tracker/references/popular-foods.yaml"
)
REAL_PERSONAL = Path("/home/stan/.openclaw/workspace/food-tracker/personal-foods.yaml")

MSG_ID = 987654321  # arbitrary but recognisable in the row


@pytest.mark.asyncio
async def test_logs_one_apple_end_to_end(tmp_path):
    if not REAL_POPULAR.exists():
        pytest.skip(f"missing popular-foods.yaml at {REAL_POPULAR}")

    # Isolate every mutable path under tmp_path.
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    if REAL_PERSONAL.exists():
        shutil.copyfile(REAL_PERSONAL, personal)
    else:
        personal.write_text("[]\n")

    mcp_settings = MCPFoodSettings(
        project_dir=Path(
            "/home/stan/dev/ql/skills/nutrition-tracker/mcp-server/food-tracker"
        ),
        personal_foods_path=personal,
        popular_foods_path=REAL_POPULAR,
        food_log_dir=log_dir,
    )
    toolset = build_food_mcp_toolset(mcp_settings)

    model = build_model(ModelSettings().profiles["cloud_haiku"])
    meal = build_meal_agent(model, toolsets=[toolset], log_dir=log_dir)
    polite = build_polite_decline_agent(model)
    root = build_root_agent(model, sub_agents=[meal, polite])
    pipeline = AgentPipeline(root_agent=root)

    try:
        reply = await pipeline.handle(
            InboundEvent(chat_id=1, msg_id=MSG_ID, text="1 apple")
        )
    finally:
        await toolset.close()

    assert reply and reply != "(no response)"

    # A monthly file for today's month should exist and contain one data row.
    today = date.today()
    md_path = log_dir / f"{today.year:04d}-{today.month:02d}.md"
    assert md_path.exists(), f"expected monthly log at {md_path}, got {list(log_dir.iterdir())}"

    data_rows = [
        line
        for line in md_path.read_text().splitlines()
        # Rows start with `| DD-MM-YYYY HH:MM |`. Skip the header / separator.
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", line)
    ]
    assert len(data_rows) == 1, f"expected 1 data row, got {len(data_rows)}:\n{data_rows}"

    row = data_rows[0].lower()
    assert "apple" in row, f"row does not mention apple: {data_rows[0]}"
    # M3 must use the semantic source enum, not the msg_id.
    assert "cache_lookup" in row, (
        f"expected source=cache_lookup in row, got: {data_rows[0]}"
    )
    assert "telegram:" not in row, (
        f"source column should NOT contain telegram: prefix, got: {data_rows[0]}"
    )
