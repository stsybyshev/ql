"""M6 end-to-end: log a meal, then promote it to personal-foods via
'save the X' — without providing new macros."""
from __future__ import annotations

import os
import re
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
async def test_log_then_save_recent_end_to_end(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    popular = tmp_path / "popular.yaml"
    # Empty popular cache — forces SHAPE B path on turn 1 (user provides macros).
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

    chat_id = 6666

    try:
        # ---- turn 1: log a novel meal with macros (SHAPE B, no save) ----
        reply1 = await pipeline.handle(
            InboundEvent(
                chat_id=chat_id,
                msg_id=1,
                text="just had 1 cup of pomegranate juice, 90 kcal 0.5P 0F 22C",
            )
        )
        assert reply1 and reply1 != "(no response)"

        rows_after_log = _data_rows(_monthly_path(mcp))
        assert len(rows_after_log) == 1, (
            f"expected 1 log row after SHAPE B log, got {len(rows_after_log)}: {rows_after_log}"
        )
        assert not personal.exists() or personal.read_text().strip() == "", (
            "SHAPE B without save-it must NOT touch personal-foods; "
            f"got: {personal.read_text() if personal.exists() else '(no file)'}"
        )

        # ---- turn 2: SAVE_RECENT — promote the just-logged meal to cache ----
        reply2 = await pipeline.handle(
            InboundEvent(
                chat_id=chat_id,
                msg_id=2,
                text="save the pomegranate juice to my personal foods",
            )
        )
        assert reply2 and reply2 != "(no response)"

        # 1. MD must have EXACTLY one row still — SAVE_RECENT must NOT double-log.
        rows_after_save = _data_rows(_monthly_path(mcp))
        assert len(rows_after_save) == 1, (
            f"SAVE_RECENT must not log a new row; got {len(rows_after_save)} rows: "
            f"{rows_after_save}"
        )

        # 2. personal-foods.yaml now contains the pomegranate juice entry.
        assert personal.exists(), "personal-foods.yaml should exist after SAVE_RECENT"
        saved = yaml.safe_load(personal.read_text()) or []
        assert isinstance(saved, list)
        names_lower = [e.get("name", "").lower() for e in saved]
        matches = [n for n in names_lower if "pomegranate" in n]
        assert matches, f"expected pomegranate entry in personal-foods, got: {names_lower}"

        entry = next(e for e in saved if "pomegranate" in e.get("name", "").lower())
        assert entry["kcal_per_unit"] == 90
        assert abs(entry["protein_per_unit"] - 0.5) < 0.01
        assert entry["fat_per_unit"] == 0
        assert entry["carbs_per_unit"] == 22
        # Unit and qty_default should have carried over from the logged row.
        # Log was "1 cup" -> qty=1, unit=cup (SHAPE B falls back to unit=serving
        # when the user didn't specify, but here they did say "cup").
        assert entry["unit"].lower() in ("cup", "serving"), (
            f"expected unit=cup or serving, got: {entry['unit']}"
        )
    finally:
        await toolset.close()
