"""A cache hit's macros must never be multiplied by a foreign unit.

All three failures below came from one turn on 17-08-2026:
"Lunch: 3 egg omelette, 150g tomato juice, 1 red grapefruit"

The MCP's `search_foods` is a substring matcher, so it returned confidently
wrong food, and nothing downstream questioned a single hit:

  "Tomato juice"   -> "tomato"  (22 kcal per TOMATO) x 150 = 3300 kcal
  "Red grapefruit" -> "grapes"  (via alias "red grapes"; "grape" is a
                                 substring of "grapefruit") -> 1 cup grapes
  "3 egg omelette" -> "3 eggs omelette" (390 kcal per SERVING) x 3 = 1170

Name similarity cannot separate these: measured with rapidfuzz, the WRONG
"tomato juice"->"tomato" scores 90 while the RIGHT "dark chocolate bar"->
"dark chocolate" scores 95. The unit is the discriminator.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nutrition_clerk.workflow.orchestrator import (
    _apply_100g_rule,
    _units_compatible,
    _usable_hits,
    orchestrate,
)
from nutrition_clerk.workflow.schemas import ExtractedEntry, ExtractedMessage

TOMATO = {
    "name": "tomato", "aliases": ["tomatoes", "fresh tomato", "raw tomato"],
    "unit": "tomato", "qty_default": 1, "kcal_per_unit": 22,
    "protein_per_unit": 1.1, "fat_per_unit": 0.2, "carbs_per_unit": 4.8,
}
GRAPES = {
    "name": "grapes", "aliases": ["grape", "red grapes", "green grapes"],
    "unit": "cup", "qty_default": 1, "kcal_per_unit": 104,
    "protein_per_unit": 1.1, "fat_per_unit": 0.2, "carbs_per_unit": 27.3,
}
OMELETTE = {
    "name": "3 eggs omelette",
    "aliases": ["omelette", "omelet", "egg omelette", "three egg omelette",
                "3 egg omelette"],
    "unit": "serving", "qty_default": 1, "kcal_per_unit": 390,
    "protein_per_unit": 24.0, "fat_per_unit": 30.0, "carbs_per_unit": 3.0,
}
DARK_CHOCOLATE = {
    "name": "dark chocolate", "aliases": ["chocolate", "70% chocolate", "dark choc"],
    "unit": "100g", "qty_default": 1, "kcal_per_unit": 546,
    "protein_per_unit": 5.5, "fat_per_unit": 32.4, "carbs_per_unit": 60.5,
}


class _FakeClient:
    def __init__(self, hits=None):
        self.lookup_food = AsyncMock(return_value=hits or [])
        self.log_food = AsyncMock(return_value={"entry": {}, "today": {}})
        self.add_personal_food = AsyncMock(return_value={"status": "ok"})
        self.fuzzy_lookup = lambda q, **kw: []


# -----------------------------------------------------------------------------
# _usable_hits — the filter
# -----------------------------------------------------------------------------

def test_grams_against_a_per_item_entry_is_dropped():
    """150g of juice cannot be 150 tomatoes."""
    entry = ExtractedEntry(name="Tomato juice", qty=150, unit="g")
    assert _usable_hits(entry, [TOMATO]) == []


def test_wrong_food_with_wrong_unit_is_dropped():
    entry = ExtractedEntry(name="Red grapefruit", qty=1, unit="grapefruit")
    assert _usable_hits(entry, [GRAPES]) == []


def test_exactly_named_entry_survives_a_unit_mismatch():
    """The food is certainly right; only the count is unmappable."""
    entry = ExtractedEntry(name="3 eggs omelette", qty=3, unit="egg")
    assert _usable_hits(entry, [OMELETTE]) == [OMELETTE]


def test_compatible_units_pass_through_untouched():
    entry = ExtractedEntry(name="dark chocolate bar", qty=60, unit="g")
    assert _usable_hits(entry, [DARK_CHOCOLATE]) == [DARK_CHOCOLATE]


# -----------------------------------------------------------------------------
# _apply_100g_rule — the quantity fallback
# -----------------------------------------------------------------------------

def test_exact_name_mismatched_unit_falls_back_to_qty_default():
    """3 "eggs" against a per-serving entry must log 1 serving, not 3."""
    entry = ExtractedEntry(name="3 eggs omelette", qty=3, unit="egg")
    assert _apply_100g_rule(entry, OMELETTE) == (1.0, "serving")


def test_grams_to_100g_still_converts():
    entry = ExtractedEntry(name="dark chocolate bar", qty=60, unit="g")
    qty, unit = _apply_100g_rule(entry, DARK_CHOCOLATE)
    assert (round(qty, 4), unit) == (0.6, "100g")


# -----------------------------------------------------------------------------
# _units_compatible — the generic-unit qty rule
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("asked, cache, qty, expected", [
    ("g", "100g", 150, True),          # weight vs weight
    ("g", "tomato", 150, False),       # the 3300 kcal bug
    ("grapefruit", "cup", 1, False),   # two unrelated concrete units
    ("egg", "serving", 3, False),      # generic at qty>1 — the 3x bug
    ("serving", "cup", 1, True),       # generic at qty=1 means "one helping"
    ("serving", "cup", 2, False),      # ...but not at qty=2
    ("cup", "cup", 5, True),           # identical units scale freely
    (None, "cup", None, True),         # no unit given
])
def test_units_compatible_matrix(asked, cache, qty, expected):
    assert _units_compatible(asked, cache, qty) is expected


# -----------------------------------------------------------------------------
# End-to-end through orchestrate
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tomato_juice_does_not_log_3300_kcal():
    client = _FakeClient(hits=[TOMATO])
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="Tomato juice", qty=150, unit="g")
    ])
    result = await orchestrate(msg, client)
    client.log_food.assert_not_called()
    assert result.logged == []
    assert [u.name for u in result.unknown] == ["Tomato juice"]


@pytest.mark.asyncio
async def test_red_grapefruit_does_not_log_grapes():
    client = _FakeClient(hits=[GRAPES])
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="Red grapefruit", qty=1, unit="grapefruit")
    ])
    result = await orchestrate(msg, client)
    client.log_food.assert_not_called()
    assert result.logged == []


@pytest.mark.asyncio
async def test_knowledge_estimate_per_100ml_is_divided_not_multiplied():
    """The 100g rule used to test `est_unit == "100g"` exactly.

    A drink estimate comes back per 100ML, skipped the conversion, and
    "150g tomato juice" logged 150 x 18 = 2700 kcal.
    """
    import nutrition_clerk.workflow.orchestrator as orch
    from nutrition_clerk.workflow.schemas import KnowledgeExtract

    async def fake_estimate(model, name):
        return KnowledgeExtract(
            unit="100ml", kcal_per_unit=18, protein_per_unit=0.9,
            fat_per_unit=0.1, carbs_per_unit=3.9, confidence=0.65,
        )

    client = _FakeClient(hits=[])
    orig = orch.estimate_macros
    orch.estimate_macros = fake_estimate
    try:
        msg = ExtractedMessage(entries=[
            ExtractedEntry(name="Tomato juice", qty=150, unit="g")
        ])
        await orchestrate(msg, client, knowledge_model=object())
    finally:
        orch.estimate_macros = orig

    kwargs = client.log_food.call_args.kwargs
    assert kwargs["qty"] == 1.5, "150g against per-100ml must be 1.5, not 150"
    assert kwargs["unit"] == "100ml"
    assert kwargs["qty"] * kwargs["kcal_per_unit"] == 27


@pytest.mark.asyncio
async def test_omelette_logs_one_serving_not_three():
    client = _FakeClient(hits=[OMELETTE])
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="3 eggs omelette", qty=3, unit="egg")
    ])
    await orchestrate(msg, client)
    kwargs = client.log_food.call_args.kwargs
    assert kwargs["qty"] == 1.0
    assert kwargs["unit"] == "serving"
    assert kwargs["kcal_per_unit"] == 390
