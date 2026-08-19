"""Regression test — Stan's real breakfast message, 2026-08-16.

    "Hi ! Can you please log my breakfast: espresso, 1tsp of MCT 8 oil,
     small capuccino, 50g of cashew nuts, 1 banana, 50g of pomegranate juice"

This one message exposed five bugs at once. Each assertion below maps to one:

1. SILENT DATA LOSS — the orchestrator `break`ed out of the entry loop on the
   first ambiguous item, so everything after it was dropped without a word.
   6 items in, 2 logged.  -> test_all_six_items_logged_in_one_turn
2. DOUBLE-LOGGING — the clarification resume replayed the whole original
   message, re-logging the items that had already succeeded.
   -> test_no_duplicate_rows
3. ASK-FOREVER LOOP — "cappuccino" is a substring of "small cappuccino", so
   answering the question scored 100 vs 90 (margin 10 < 15) and the same
   question came back every turn, adding duplicates each time.
   -> test_no_pending_clarification_left_hanging
4. WRONG VARIANT — the extractor normalised "small capuccino" to "Cappuccino",
   dropping the size qualifier, so an 80 kcal row was logged where the cache
   had a 35 kcal "small cappuccino".  -> test_size_qualifier_preserved
5. CASING — cache hits were written lowercase ("espresso") against a log whose
   convention is sentence case.  -> test_sentence_case_names

Uses a copy of the real personal-foods.yaml because the bugs depended on its
actual contents (both "cappuccino" and "small cappuccino" present).
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

pytest.importorskip("google.adk")

from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import Config, MCPFoodSettings, MCPSettings, PathSettings  # noqa: E402
from nutrition_clerk.workflow import build_handler  # noqa: E402
from nutrition_clerk.workflow import context as chat_context  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)

REAL_POPULAR = Path(
    "/home/stan/.openclaw/workspace/skills/openclaw-food-tracker/references/popular-foods.yaml"
)
REAL_PERSONAL = Path("/home/stan/.openclaw/workspace/food-tracker/personal-foods.yaml")

MESSAGE = (
    "Hi ! Can you please log my breakfast: espresso, 1tsp of MCT 8 oil, "
    "small capuccino, 50g of cashew nuts, 1 banana, 50g of pomegranate juice"
)

# The six foods named, as loose substrings — the extractor may tidy wording.
EXPECTED_FOODS = ["espresso", "mct", "cappuccino", "cashew", "banana", "pomegranate"]


@pytest.fixture(autouse=True)
def _reset_context():
    chat_context._reset_all()
    yield
    chat_context._reset_all()


@pytest.fixture(scope="module")
def breakfast_result(tmp_path_factory):
    """Run the message once; every test inspects the same outcome.

    Module-scoped so the six assertions cost one LLM call, not six.
    """
    if not REAL_PERSONAL.exists() or not REAL_POPULAR.exists():
        pytest.skip("real food caches not present")

    tmp = tmp_path_factory.mktemp("breakfast")
    log_dir = tmp / "logs"
    log_dir.mkdir()
    personal = tmp / "personal.yaml"
    shutil.copyfile(REAL_PERSONAL, personal)   # copy — never touch the real cache

    config = Config()
    config.paths = PathSettings(state_dir=tmp / "state")
    config.mcp = MCPSettings(food_tracker=MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=REAL_POPULAR,
        food_log_dir=log_dir,
    ))

    import asyncio

    async def _run():
        handler, client = build_handler(config)
        try:
            reply = await handler(InboundEvent(chat_id=99, msg_id=1, text=MESSAGE))
        finally:
            await client.close()
        return reply

    reply = asyncio.run(_run())

    today = date.today()
    md = log_dir / f"{today.year:04d}-{today.month:02d}.md"
    rows = [
        l for l in (md.read_text().splitlines() if md.exists() else [])
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", l)
    ]
    return {
        "reply": reply,
        "rows": rows,
        "cells": [[c.strip() for c in r.split("|")] for r in rows],
        "personal": personal,
        "ctx": chat_context.get_context(99),
    }


def test_all_six_items_logged_in_one_turn(breakfast_result):
    """Bug 1: items after an ambiguous one were silently dropped."""
    rows = breakfast_result["rows"]
    assert len(rows) == 6, (
        f"expected all 6 items logged in ONE turn, got {len(rows)}:\n"
        + "\n".join(rows)
    )
    foods = " | ".join(c[2].lower() for c in breakfast_result["cells"])
    missing = [f for f in EXPECTED_FOODS if f not in foods]
    assert not missing, f"items silently dropped: {missing}\nlogged: {foods}"


def test_no_duplicate_rows(breakfast_result):
    """Bug 2: the resume path used to re-log already-successful items."""
    foods = [c[2].lower() for c in breakfast_result["cells"]]
    dupes = {f for f in foods if foods.count(f) > 1}
    assert not dupes, f"duplicate rows for: {dupes}"


def test_no_pending_clarification_left_hanging(breakfast_result):
    """Bug 3: the ask-forever loop left a question pending every turn."""
    ctx = breakfast_result["ctx"]
    assert ctx.pending_clarification is None, (
        f"turn ended with an unanswered question: {ctx.pending_clarification!r}"
    )
    assert ctx.pending_entries == []


def test_size_qualifier_preserved(breakfast_result):
    """Bug 4: 'small capuccino' logged as plain 'cappuccino'.

    The extractor used to drop the size qualifier while fixing the typo.
    The user's cache holds both variants, so losing 'small' silently picks
    the wrong one.
    """
    cells = breakfast_result["cells"]
    cap = [c for c in cells if "cappuccino" in c[2].lower()]
    assert len(cap) == 1, f"expected exactly one cappuccino row, got {[c[2] for c in cap]}"
    assert "small" in cap[0][2].lower(), (
        f"size qualifier dropped: logged {cap[0][2]!r}. The cache has both "
        "'cappuccino' (80 kcal) and 'small cappuccino' (35 kcal)."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN, DELIBERATELY UNFIXED: food_cache.resolve_log_units does "
        "`entry = matches[0]` on a substring search, so logging 'small "
        "cappuccino' re-resolves to the 'cappuccino' entry that sits earlier "
        "in the YAML and overwrites 35 kcal with 80. The clerk picks the "
        "right entry; the MCP server discards it. Lives in the shared layer "
        "so Veda has it too — ~16 rows of real history are already affected. "
        "Parked pending a decision on changing shared behaviour. "
        "When fixed, this XPASSes and the marker should be removed."
    ),
)
def test_small_cappuccino_uses_the_small_macros(breakfast_result):
    cap = [c for c in breakfast_result["cells"] if "cappuccino" in c[2].lower()]
    kcal = float(cap[0][12])
    assert kcal < 60, (
        f"logged {kcal} kcal for a small cappuccino — the 80 kcal regular "
        "entry was used instead of the 35 kcal small one"
    )


def test_sentence_case_names(breakfast_result):
    """Bug 5: cache hits were written lowercase; the log uses sentence case."""
    for cells in breakfast_result["cells"]:
        name = cells[2]
        assert name[:1].isupper() or not name[:1].isalpha(), (
            f"food name not sentence-cased: {name!r}"
        )


def test_personal_cache_untouched(breakfast_result):
    """Nothing here asked to save — the cache must not have grown."""
    saved = yaml.safe_load(breakfast_result["personal"].read_text()) or []
    original = yaml.safe_load(REAL_PERSONAL.read_text()) or []
    assert len(saved) == len(original), "personal-foods was modified without a save request"


def test_reply_mentions_every_item(breakfast_result):
    """The user must be able to see what landed, without opening the file."""
    reply = breakfast_result["reply"].lower()
    missing = [f for f in EXPECTED_FOODS if f not in reply]
    assert not missing, f"reply doesn't mention: {missing}\nreply was:\n{breakfast_result['reply']}"


def test_turn_trace_written(breakfast_result, tmp_path_factory):
    """Tracing should have captured this turn end-to-end."""
    import json

    state_dirs = list(Path(breakfast_result["personal"]).parent.glob("state/turns.jsonl"))
    assert state_dirs, "no turns.jsonl written"
    recs = [json.loads(l) for l in state_dirs[0].read_text().splitlines() if l.strip()]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["error"] is None
    node_names = [n["node"] for n in rec["nodes"]]
    assert "extractor" in node_names
    assert any(n.startswith("mcp.log_food") for n in node_names)
    # The prompt/response capture is the whole point.
    extractor = next(n for n in rec["nodes"] if n["node"] == "extractor")
    assert extractor.get("prompt") and extractor.get("response")
