"""Unit tests for workflow.router — the food/other gate in front of the extractor.

The workflow router had NO test file before 17-08-2026; tests/unit/test_router.py
covers the legacy ADK ModelRouter, which is a different thing entirely. That
gap is how the silent-decline bug below survived.
"""
from __future__ import annotations

import pytest

from nutrition_clerk.workflow.router import route


# -----------------------------------------------------------------------------
# The regression: real messages the old food-word whitelist silently declined
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    # 16-08-2026, verbatim. No verb, no meal word, and `grams?` does not match
    # "200g" — the old regex declined it without any LLM call.
    "200g cherries and 60g dark chocolate bar",
    # Same shape, foods absent from any plausible whitelist.
    "150g blueberries",
    "membrillo 40g",
    "sundubu-jigaye",
    "2 kipper fillets",
    "handful of cashews",
])
def test_bare_food_messages_reach_the_extractor(text):
    assert route(text, has_photo=False) == "food", (
        f"{text!r} must reach the extractor — a router that declines it is "
        "guessing at semantics it cannot see."
    )


# -----------------------------------------------------------------------------
# Default is food
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "just had 1 apple",
    "what's a good pasta recipe?",   # non-food, but the EXTRACTOR declines it
    "hello",
    "asdfgh",
    "100g w/ sauce",                 # mid-string slash is not a command
])
def test_anything_with_content_routes_to_food(text):
    assert route(text, has_photo=False) == "food"


def test_photo_always_routes_to_food():
    assert route("", has_photo=True) == "food"
    assert route("what is this", has_photo=True) == "food"


# -----------------------------------------------------------------------------
# The only short-circuits
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_text_without_photo_declines(text):
    assert route(text, has_photo=False) == "other"


@pytest.mark.parametrize("text", ["/start", "/help", "/reset now", "  /start  "])
def test_slash_commands_decline_without_an_llm_call(text):
    assert route(text, has_photo=False) == "other"


def test_command_with_photo_still_routes_to_food():
    """A caption starting with '/' alongside a photo is still worth extracting."""
    assert route("/start", has_photo=True) == "food"
