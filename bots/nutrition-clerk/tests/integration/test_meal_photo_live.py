"""SHAPE D end-to-end: multi-dish text + meal photo -> per-dish log rows.

We can't cleanly synthesize a photorealistic dish photo in a test, so this
verifies the PIPELINE behaviour: with a plausible photo attached and text
naming multiple dishes, the meal agent produces per-dish rows using the
correct source enum (photo_estimate for cache-miss items) and never falls
back to photo_label (which is label-only).

Real portion accuracy needs a real photo — that's manual smoke-testing
territory once the bot is on Telegram.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("google.adk")
pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

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

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int):
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size=size)
            except OSError:
                continue
    return None


def _make_meal_scene_png(path: Path) -> None:
    """Synthetic 'restaurant table' image. Not photorealistic — the test
    doesn't depend on Haiku recognising food from pixels; it depends on the
    agent classifying the image as SHAPE D (meal, not label) given the
    text names the dishes."""
    W, H = 800, 600
    img = Image.new("RGB", (W, H), color=(240, 230, 210))  # warm tablecloth
    draw = ImageDraw.Draw(img)
    # Draw a few "plates" as circles.
    plates = [
        (180, 200, 320, 340, (255, 245, 230)),
        (420, 180, 570, 330, (255, 240, 220)),
        (240, 380, 400, 520, (250, 235, 210)),
        (500, 380, 660, 520, (245, 230, 200)),
    ]
    for x0, y0, x1, y1, col in plates:
        draw.ellipse((x0, y0, x1, y1), fill=col, outline=(150, 130, 100), width=4)
    font = _load_font(28)
    if font:
        draw.text((40, 30), "restaurant table photo (synthetic)", fill=(80, 60, 40), font=font)
    img.save(path, format="PNG")


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


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|")]


@pytest.mark.asyncio
async def test_multi_dish_meal_photo_produces_per_dish_rows(tmp_path):
    photo = tmp_path / "meal.png"
    _make_meal_scene_png(photo)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    popular = tmp_path / "popular.yaml"
    popular.write_text("[]\n")  # empty cache -> every dish is a miss

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
        "Dinner in a Turkish restaurant: tzatziki, hummus, bread basket, "
        "halloumi burger"
    )
    try:
        reply = await pipeline.handle(
            InboundEvent(chat_id=1, msg_id=99, text=text, photos=[photo])
        )
    finally:
        await toolset.close()

    assert reply and reply != "(no response)"

    rows = _data_rows(_monthly_path(mcp))
    # 4 dishes named. Allow a wide band because LLM might merge "bread
    # basket" into a single row or split "hummus" into "hummus + pita".
    assert 3 <= len(rows) <= 6, (
        f"expected 3-6 rows for a 4-dish meal, got {len(rows)}:\n" + "\n".join(rows)
    )

    foods = [_cells(r)[2].lower() for r in rows]
    sources = [_cells(r)[13].lower() for r in rows]
    kcals = [float(_cells(r)[12]) for r in rows]

    # No row should use photo_label — that's label-photo only.
    assert not any(s == "photo_label" for s in sources), (
        f"photo_label must NOT be used for a meal photo, got: {list(zip(foods, sources))}"
    )
    # All sources must be a recognised enum value (no drift).
    allowed = {"cache_lookup", "text_estimate", "photo_estimate"}
    unknown = [s for s in sources if s not in allowed]
    assert not unknown, f"unexpected source values: {unknown}"

    # At least one row should be tagged as an estimate (cache is empty).
    assert any(s in {"text_estimate", "photo_estimate"} for s in sources), (
        f"expected at least one estimate source, got: {sources}"
    )

    # Each dish name from the text should appear in at least one row.
    joined = " | ".join(foods)
    for expected in ["tzatziki", "hummus", "halloumi"]:
        assert expected in joined, (
            f"expected dish {expected!r} in logged rows, got foods: {foods}"
        )

    # Total kcal should be plausible for a Turkish dinner (300-3000).
    total = sum(kcals)
    assert 300 <= total <= 3000, f"suspicious total kcal {total} for dinner: {list(zip(foods, kcals))}"

    # personal-foods must not be mutated — no save was requested.
    assert not personal.exists() or personal.read_text().strip() == "", (
        f"personal-foods was silently mutated: {personal.read_text()}"
    )
