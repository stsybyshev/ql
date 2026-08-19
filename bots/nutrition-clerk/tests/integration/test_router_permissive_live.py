"""The permissive router: food gets through, non-food still declines.

17-08-2026. The router was a regex whitelist of food words and silently
declined "200g cherries and 60g dark chocolate bar" — no verb, no meal word,
and `grams?` does not match "200g". No LLM call was made; the user just got
a refusal.

Removing that whitelist only works if the EXTRACTOR reliably declines non-food,
since it is now the sole classifier. Both directions are asserted here.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import Config, MCPFoodSettings, MCPSettings  # noqa: E402
from nutrition_clerk.workflow import build_handler  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)


def _build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    popular = tmp_path / "popular.yaml"
    popular.write_text("[]\n")
    config = Config()
    config.mcp = MCPSettings(food_tracker=MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=popular,
        food_log_dir=log_dir,
    ))
    handler, client = build_handler(config)
    return handler, client, log_dir


def _rows(log_dir: Path) -> list[str]:
    today = date.today()
    md = log_dir / f"{today.year:04d}-{today.month:02d}.md"
    if not md.exists():
        return []
    return [
        l for l in md.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", l)
    ]


@pytest.mark.asyncio
async def test_bare_food_message_reaches_the_extractor(tmp_path):
    """The verbatim message that was silently declined.

    Asserts on ACCOUNTABILITY, not row count. Against an empty cache the
    knowledge estimator legitimately refuses "dark chocolate bar" (cocoa
    content swings the macros too far to guess), and the formatter surfaces
    that under "Couldn't resolve:". Both outcomes are correct; what must
    never happen again is the whole message vanishing into a canned decline.
    """
    handler, client, log_dir = _build(tmp_path)
    try:
        reply = await handler(InboundEvent(
            chat_id=1, msg_id=1,
            text="200g cherries and 60g dark chocolate bar",
        ))
    finally:
        await client.close()

    assert "only handle food logging" not in reply.lower(), (
        f"router declined a plain food message: {reply!r}"
    )

    rows = _rows(log_dir)
    foods = " ".join(r.split("|")[2].lower() for r in rows)
    assert "cherr" in foods, f"cherries should log from world knowledge: {rows}"
    # 200g against per-100g rates -> qty=2.0, unit=100g (the 100g rule).
    cherry_row = next(r for r in rows if "cherr" in r.split("|")[2].lower())
    cells = [c.strip() for c in cherry_row.split("|")]
    assert cells[4].lower() == "100g", f"100g rule violated: {cherry_row}"
    assert abs(float(cells[3]) - 2.0) < 0.02, f"expected qty 2.0: {cherry_row}"

    # The chocolate is either logged or explicitly reported — never dropped.
    assert "chocolate" in (foods + reply).lower(), (
        f"chocolate silently vanished — neither logged nor reported: {reply!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "what's a good pasta recipe?",
    "what's the weather tomorrow?",
    "can you book me a table for two",
])
async def test_non_food_still_declines_and_logs_nothing(tmp_path, text):
    """The extractor is now the ONLY classifier — it must hold the line."""
    handler, client, log_dir = _build(tmp_path)
    try:
        reply = await handler(InboundEvent(chat_id=1, msg_id=1, text=text))
    finally:
        await client.close()

    assert _rows(log_dir) == [], f"non-food message logged rows: {_rows(log_dir)}"
    assert "logged" not in reply.lower(), reply
