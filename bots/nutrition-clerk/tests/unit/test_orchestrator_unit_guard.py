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


# --- the exact-name rescue must not eat a stated weight (19-08-2026) ---
#
# "Dinner: 160g buckwheat, 240g yellow fin tuna, 200g cucumber, ..."
#
# The rescue added for "3 eggs omelette" keeps a unit-incompatible hit when the
# user names it exactly, then falls back to qty_default. That is right when the
# user's number counts something the cache cannot measure. It is wrong when the
# user gave a WEIGHT, which is exact and transferable:
#
#   200g cucumber -> per-cucumber entry (qty_default 0.5) -> logged 0.5 cucumber,
#                    the 200g silently discarded.
#   160g buckwheat -> pulled a per-CUP entry back in beside the per-100g one,
#                     manufacturing "Did you mean buckwheat or buckwheat?".

CUCUMBER = {
    "name": "cucumber", "aliases": ["cucumbers"],
    "unit": "cucumber", "qty_default": 0.5, "kcal_per_unit": 45,
    "protein_per_unit": 2.0, "fat_per_unit": 0.3, "carbs_per_unit": 11.0,
}
BUCKWHEAT_100G = {
    "name": "buckwheat", "aliases": ["kasha", "buckwheat groats"],
    "unit": "100g", "qty_default": 1, "kcal_per_unit": 334,
    "protein_per_unit": 13.3, "fat_per_unit": 3.0, "carbs_per_unit": 57.6,
}
BUCKWHEAT_CUP = {
    "name": "buckwheat", "aliases": ["kasha", "buckwheat groats", "toasted buckwheat"],
    "unit": "cup", "qty_default": 1, "kcal_per_unit": 155,
    "protein_per_unit": 5.7, "fat_per_unit": 1.0, "carbs_per_unit": 33.5,
}


def test_stated_weight_is_not_replaced_by_qty_default():
    """200g against a per-cucumber entry must drop the hit, not log 0.5."""
    entry = ExtractedEntry(name="Cucumber", qty=200, unit="g")
    assert _usable_hits(entry, [CUCUMBER]) == []


def test_weight_entry_survives_while_incompatible_twin_is_dropped():
    """160g buckwheat keeps the per-100g entry and drops the per-cup one."""
    entry = ExtractedEntry(name="Buckwheat", qty=160, unit="g")
    kept = _usable_hits(entry, [BUCKWHEAT_100G, BUCKWHEAT_CUP])
    assert kept == [BUCKWHEAT_100G], "the per-cup twin created a bogus ambiguity"


def test_non_weight_count_still_rescues():
    """The omelette case must keep working — "egg" is a count, not a weight."""
    entry = ExtractedEntry(name="3 eggs omelette", qty=3, unit="egg")
    assert _usable_hits(entry, [OMELETTE]) == [OMELETTE]


def test_question_never_offers_two_identical_labels():
    """"Did you mean buckwheat or buckwheat?" is unanswerable."""
    from nutrition_clerk.workflow.orchestrator import _disambiguate_cache_hits
    hit, question = _disambiguate_cache_hits("buckwheat", [BUCKWHEAT_100G, BUCKWHEAT_CUP])
    if question is not None:
        offered = question.replace("Did you mean ", "").rstrip("?").split(" or ")
        assert len(set(offered)) == len(offered), f"indistinguishable options: {question}"
        assert "100g" in question and "cup" in question


# --- exact name outranks the unit; the unit breaks a name tie (20-08-2026) ---
#
# The MCP is a substring matcher, so "salmon traybake" also returns plain
# "salmon". Filtering on unit alone would keep the per-100g fish and log a
# traybake as salmon. Name first, then unit as the tiebreak.

SALMON = {"name": "salmon", "aliases": [], "unit": "100g", "qty_default": 1,
          "kcal_per_unit": 208, "protein_per_unit": 20.0,
          "fat_per_unit": 13.0, "carbs_per_unit": 0.0}
TRAYBAKE = {"name": "salmon traybake", "aliases": ["salmon tray bake"],
            "unit": "serving", "qty_default": 1, "kcal_per_unit": 1070,
            "protein_per_unit": 57.0, "fat_per_unit": 69.0, "carbs_per_unit": 58.0}


def test_exact_name_outranks_a_unit_compatible_substring_match():
    """"300g salmon traybake" must not resolve to plain salmon."""
    entry = ExtractedEntry(name="Salmon traybake", qty=300, unit="g")
    kept = _usable_hits(entry, [SALMON, TRAYBAKE])
    assert SALMON not in kept, "unit compatibility beat an exact name match"


def test_exact_name_wins_when_its_unit_does_fit():
    entry = ExtractedEntry(name="Salmon traybake", qty=1, unit="serving")
    assert _usable_hits(entry, [SALMON, TRAYBAKE]) == [TRAYBAKE]


def test_a_plain_name_still_reaches_its_own_entry():
    entry = ExtractedEntry(name="Salmon", qty=200, unit="g")
    assert _usable_hits(entry, [SALMON, TRAYBAKE]) == [SALMON]


@pytest.mark.parametrize("qty, unit, expected_unit", [
    (160, "g", "100g"),      # weight -> the per-100g twin
    (1, "cup", "cup"),       # a cup   -> the per-cup twin
    (None, None, "cup"),     # bare    -> one normal helping
])
def test_unit_breaks_a_tie_between_identically_named_entries(qty, unit, expected_unit):
    """Personal 'buckwheat' is per-100g; popular 'buckwheat' is per-cup."""
    entry = ExtractedEntry(name="Buckwheat", qty=qty, unit=unit)
    kept = _usable_hits(entry, [BUCKWHEAT_100G, BUCKWHEAT_CUP])
    assert [h["unit"] for h in kept] == [expected_unit]
