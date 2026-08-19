"""Unit tests for orchestrator._disambiguate_cache_hits + N4 branch."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nutrition_clerk.workflow.orchestrator import (
    _disambiguate_cache_hits,
    orchestrate,
)
from nutrition_clerk.workflow.schemas import ExtractedEntry, ExtractedMessage


def _hit(name: str, unit: str = "serving") -> dict:
    return {
        "name": name, "unit": unit, "qty_default": 1,
        "kcal_per_unit": 100, "protein_per_unit": 5,
        "fat_per_unit": 3, "carbs_per_unit": 10,
    }


# Chia is asked for in grams below, so its cache entries must be per-100g.
# They used to be per-"serving", which `_usable_hits` now rejects outright —
# 50 of something measured per serving is unmappable, and pretending
# otherwise is what logged 150g of tomato juice as 150 tomatoes.
def _chia_hit(name: str) -> dict:
    return _hit(name, unit="100g")


class _FakeClient:
    def __init__(self, lookup_returns=None):
        self.lookup_food = AsyncMock(return_value=lookup_returns or [])
        self.log_food = AsyncMock(return_value={"entry": {}, "today": {}})
        self.add_personal_food = AsyncMock(return_value={"status": "ok"})
        self.fuzzy_lookup = lambda q, **kw: []


# --- _disambiguate_cache_hits ---

def test_single_hit_returns_it_no_question():
    hit, q = _disambiguate_cache_hits("apple", [_hit("Apple")])
    assert q is None
    assert hit["name"] == "Apple"


def test_ambiguous_two_close_matches_asks():
    hit, q = _disambiguate_cache_hits(
        "chia", [_hit("chia pudding"), _hit("chia seeds")]
    )
    assert hit is None
    assert q is not None
    assert "chia pudding" in q and "chia seeds" in q
    assert q.startswith("Did you mean")


def test_clear_winner_no_ask():
    # apple vs Apple = 100, apple vs banana ~18 -> huge margin, unambiguous.
    # (Deliberately not using pineapple: "apple" is a substring of "pineapple"
    # so rapidfuzz WRatio scores it ~90, well inside the ambiguity band. That
    # was the reason M5's clarify tests picked far-apart food names.)
    hit, q = _disambiguate_cache_hits("apple", [_hit("Apple"), _hit("banana")])
    assert q is None
    assert hit["name"] == "Apple"


def test_three_candidates_question_lists_all_three():
    hit, q = _disambiguate_cache_hits(
        "chia",
        [_hit("chia pudding"), _hit("chia seeds"), _hit("chia bar")],
    )
    assert hit is None
    assert q is not None
    # "Did you mean X, Y, or Z?" — 2 commas
    assert q.count(",") == 2
    for name in ("chia pudding", "chia seeds", "chia bar"):
        assert name in q


# --- orchestrate short-circuit ---

@pytest.mark.asyncio
async def test_orchestrate_sets_pending_clarification_on_ambiguous():
    client = _FakeClient(lookup_returns=[_chia_hit("chia pudding"), _chia_hit("chia seeds")])
    msg = ExtractedMessage(entries=[ExtractedEntry(name="chia", qty=50, unit="g")])
    result = await orchestrate(msg, client)
    assert result.pending_clarification is not None
    assert "chia pudding" in result.pending_clarification
    # No log fired.
    client.log_food.assert_not_called()
    assert result.logged == []


@pytest.mark.asyncio
async def test_orchestrate_partial_log_before_ambiguous_entry():
    """Entries logged BEFORE the ambiguous one stay logged; ambiguous stops the loop."""
    client = _FakeClient()
    # Two entries: first is a clean single-hit, second is ambiguous.
    async def fake_lookup(q):
        if q == "apple":
            return [_hit("Apple")]
        return [_chia_hit("chia pudding"), _chia_hit("chia seeds")]
    client.lookup_food = AsyncMock(side_effect=fake_lookup)

    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="apple", qty=1, unit="apple"),
        ExtractedEntry(name="chia", qty=50, unit="g"),
    ])
    result = await orchestrate(msg, client)
    assert len(result.logged) == 1
    assert result.pending_clarification is not None


# --- aliases participate in ranking (19-08-2026) ---
#
# `lookup_food("spicy eggs")` returns BOTH "Spanish eggs" (matched via its
# alias) and the generic "egg" (substring of "eggs"). Ranking scored only
# `name`, so "spicy eggs" vs "spanish eggs" (72.7) lost to "egg" (90.0) and
# the wrong entry was logged — 72 kcal instead of 280. `fuzzy_lookup` had
# always scored over aliases; this path had not.

_SPANISH_EGGS = {
    "name": "Spanish eggs",
    "aliases": ["spanish eggs", "huevos", "fried eggs with cumin", "spicy eggs"],
    "unit": "serving", "qty_default": 1, "kcal_per_unit": 280,
    "protein_per_unit": 19.0, "fat_per_unit": 20.0, "carbs_per_unit": 2.5,
}
_EGG = {
    "name": "egg",
    "aliases": ["eggs", "fried egg", "boiled egg", "scrambled egg",
                "scrambled eggs", "poached egg"],
    "unit": "egg", "qty_default": 2, "kcal_per_unit": 72,
    "protein_per_unit": 6.3, "fat_per_unit": 4.8, "carbs_per_unit": 0.4,
}


@pytest.mark.parametrize("query, expected", [
    ("Spanish eggs", "Spanish eggs"),
    ("spicy eggs", "Spanish eggs"),            # alias-only match
    ("huevos", "Spanish eggs"),                # alias sharing no word with the name
    ("fried eggs with cumin", "Spanish eggs"),
    ("scrambled eggs", "egg"),                 # the generic entry still wins its own
])
def test_alias_matches_beat_a_generic_name_match(query, expected):
    hit, question = _disambiguate_cache_hits(query, [_SPANISH_EGGS, _EGG])
    assert question is None, f"{query!r} should not need clarification"
    assert hit["name"] == expected
