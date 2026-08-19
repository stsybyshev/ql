"""M7 end-to-end: photo of nutrition label -> logged row via real Haiku vision.

Generates a synthetic PNG label at test time (known values) so we can assert
the extracted per-100g figures match. Requires ANTHROPIC_API_KEY and a
truetype font on the system (bundled on most Linux distros).
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("google.adk")
PIL = pytest.importorskip("PIL")
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
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | None:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return None


def _make_label_png(
    path: Path,
    *,
    product_name: str,
    kcal: float,
    protein: float,
    fat: float,
    carbs: float,
) -> None:
    """Draw a clean, high-contrast synthetic nutrition-label PNG."""
    W, H = 700, 900
    img = Image.new("RGB", (W, H), color="white")
    draw = ImageDraw.Draw(img)

    title_font = _load_font(48)
    header_font = _load_font(36)
    body_font = _load_font(30)
    if not title_font or not header_font or not body_font:
        pytest.skip("no truetype font available for synthetic label test")

    y = 40
    draw.text((40, y), product_name, fill="black", font=title_font)
    y += 90
    draw.text((40, y), "Nutrition Facts", fill="black", font=header_font)
    y += 60
    draw.line([(40, y), (W - 40, y)], fill="black", width=3)
    y += 30
    draw.text((40, y), "Per 100g:", fill="black", font=header_font)
    y += 70
    for label, value, unit in [
        ("Calories", kcal, "kcal"),
        ("Protein", protein, "g"),
        ("Fat", fat, "g"),
        ("Carbohydrates", carbs, "g"),
    ]:
        draw.text((60, y), f"{label}:", fill="black", font=body_font)
        # Right-align the number
        num_text = f"{value:g} {unit}"
        bbox = draw.textbbox((0, 0), num_text, font=body_font)
        draw.text((W - 40 - (bbox[2] - bbox[0]), y), num_text, fill="black", font=body_font)
        y += 55

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


def _row_cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|")]


@pytest.mark.asyncio
async def test_label_photo_logs_correct_totals(tmp_path):
    label_path = tmp_path / "label.png"
    _make_label_png(
        label_path,
        product_name="Pomegranate Juice",
        kcal=55,
        protein=1.0,
        fat=2.8,
        carbs=5.9,
    )

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
            InboundEvent(
                chat_id=1,
                msg_id=77,
                text="had 600g of this juice",
                photos=[label_path],
            )
        )
    finally:
        await toolset.close()

    assert reply and reply != "(no response)"

    rows = _data_rows(_monthly_path(mcp))
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}:\n" + "\n".join(rows)
    cells = _row_cells(rows[0])
    # Columns: | Datetime | Food | Qty | Unit | P/u | F/u | C/u | Kcal/u | P | F | C | Kcal | Source | Confidence |
    #             1         2      3     4      5     6     7     8       9  10  11  12    13       14
    food = cells[2].lower()
    qty = float(cells[3])
    unit = cells[4].lower()
    kcal_per_unit = float(cells[8])
    kcal_total = float(cells[12])
    source = cells[13].lower()

    # Product name identified.
    assert "pomegranate" in food, f"expected pomegranate in food, got: {cells[2]}"
    # 100g rule honoured — the critical production-safety check.
    assert unit == "100g", f"expected unit=100g for label log, got: {unit}"
    assert abs(qty - 6.0) < 0.01, f"expected qty=6 (600g/100g), got: {qty}"
    # Per-100g values within tolerance (Haiku might round).
    assert abs(kcal_per_unit - 55) < 3, f"expected ~55 kcal/100g, got: {kcal_per_unit}"
    # Total kcal = 55 * 6 = 330 (within tolerance).
    assert 320 <= kcal_total <= 340, f"expected total kcal ~330, got: {kcal_total}"
    # Source is the semantic enum for label photos.
    assert source == "photo_label", f"expected source=photo_label, got: {source}"
