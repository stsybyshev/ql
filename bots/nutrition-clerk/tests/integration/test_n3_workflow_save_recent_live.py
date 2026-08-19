"""N3 end-to-end: log a meal, then "save the X" next turn — promotes it into
personal-foods without duplicating the log row."""
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


def _build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"          # not seeded — created on first save
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
async def test_log_then_save_recent_end_to_end(tmp_path):
    handler, client, log_dir, personal = _build(tmp_path)
    chat_id = 3333

    try:
        # ---- turn 1: log a novel meal with typed macros ----
        reply1 = await handler(InboundEvent(
            chat_id=chat_id, msg_id=1,
            text="just had 1 cup of pomegranate juice, 90 kcal 0.5P 0F 22C",
        ))
        assert reply1 and reply1 != "(no response)"
        rows_after_log = _data_rows(_monthly_path(log_dir))
        assert len(rows_after_log) == 1, (
            f"expected 1 row after SHAPE B log, got {len(rows_after_log)}: {rows_after_log}"
        )
        # No save requested this turn — personal-foods should not exist yet.
        assert not personal.exists() or personal.read_text().strip() == "", (
            "personal-foods should not exist without an explicit save"
        )

        # ---- turn 2: SHAPE 6.4 promotion ----
        reply2 = await handler(InboundEvent(
            chat_id=chat_id, msg_id=2,
            text="save the pomegranate juice to my personal foods",
        ))
        assert reply2 and reply2 != "(no response)"

        # 1. MD file still has ONE row — no double-log.
        rows_after_save = _data_rows(_monthly_path(log_dir))
        assert len(rows_after_save) == 1, (
            f"SHAPE 6.4 must NOT create a new log row; got {len(rows_after_save)}: "
            f"{rows_after_save}"
        )

        # 2. personal-foods.yaml has the promoted entry with correct macros.
        assert personal.exists(), "personal-foods.yaml must exist after promotion"
        saved = yaml.safe_load(personal.read_text()) or []
        assert isinstance(saved, list) and len(saved) >= 1
        entry = next(
            (e for e in saved if "pomegranate" in (e.get("name", "").lower())),
            None,
        )
        assert entry is not None, f"expected pomegranate entry, got names: {[e.get('name') for e in saved]}"
        assert entry["kcal_per_unit"] == 90
        assert abs(entry["protein_per_unit"] - 0.5) < 0.01
        assert entry["fat_per_unit"] == 0
        assert entry["carbs_per_unit"] == 22

        # 3. Reply mentions saved.
        assert "saved" in reply2.lower(), f"reply should confirm save: {reply2!r}"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_save_recent_not_found_when_no_prior_log(tmp_path):
    """User asks to save something they haven't logged this session — get told."""
    handler, client, log_dir, personal = _build(tmp_path)
    chat_id = 3334

    try:
        reply = await handler(InboundEvent(
            chat_id=chat_id, msg_id=1,
            text="save the pomegranate juice I had to my personal foods",
        ))
        assert reply and reply != "(no response)"

        # No log row (nothing to save from).
        assert _data_rows(_monthly_path(log_dir)) == []
        # personal-foods.yaml should NOT exist.
        assert not personal.exists() or personal.read_text().strip() == ""
        # Reply should tell the user we couldn't find it.
        low = reply.lower()
        assert "don't see" in low or "log it first" in low or "no recent" in low, (
            f"expected 'not found' message; got: {reply!r}"
        )
    finally:
        await client.close()
