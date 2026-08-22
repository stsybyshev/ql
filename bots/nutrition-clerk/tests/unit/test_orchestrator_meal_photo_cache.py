"""SHAPE D: cache must beat a vision estimate for foods the user has measured.

Regression origin — a real cafe turn on 16-08-2026. The message was
"Anzac cookie (photo attached). And cappuccino"; only the cookie was in the
photo. Vision was handed the full message text, so it itemised BOTH, inventing
a cappuccino at 120 kcal with confidence 0.4 and its own admission in the note:
"not visible in photo but mentioned by user". That 0.4 guess overrode the
user's calibrated cache entry (80 kcal), and the row landed as photo_estimate.
Veda, which looks the drink up in cache, logged it correctly.

These tests pin the fix: every dish vision returns is checked against the cache
first, and a confident, unit-compatible hit wins.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nutrition_clerk.workflow import orchestrator as orch
from nutrition_clerk.workflow.orchestrator import _units_compatible, orchestrate
from nutrition_clerk.workflow.schemas import (
    ExtractedEntry,
    ExtractedMessage,
    MealDish,
    PhotoExtract,
)

CAPPUCCINO = {
    "name": "cappuccino",
    "unit": "cup",
    "qty_default": 1,
    "kcal_per_unit": 80,
    "protein_per_unit": 4.0,
    "fat_per_unit": 4.0,
    "carbs_per_unit": 6.0,
}


class _FakeClient:
    def __init__(self, lookup_by_name: dict[str, list[dict]] | None = None):
        self._by_name = lookup_by_name or {}
        self.log_food = AsyncMock(return_value={"entry": {}, "today": {}})
        self.add_personal_food = AsyncMock(return_value={"status": "ok"})
        self.fuzzy_lookup = lambda q, **kw: []

    async def lookup_food(self, name: str) -> list[dict]:
        return self._by_name.get(name.strip().lower(), [])


def _patch_vision(monkeypatch, dishes: list[MealDish]) -> None:
    async def _fake(model, photo, hint_text=""):
        return PhotoExtract(kind="meal", dishes=dishes)

    monkeypatch.setattr(orch, "analyse_photo", _fake)


def _log_kwargs_by_food(client: _FakeClient) -> dict[str, dict]:
    return {c.kwargs["food"]: c.kwargs for c in client.log_food.call_args_list}


@pytest.mark.asyncio
async def test_cached_dish_logs_from_cache_not_photo_estimate(monkeypatch, tmp_path):
    """The exact cafe regression: cappuccino resolves to 80 kcal, not 120."""
    _patch_vision(monkeypatch, [
        MealDish(name="Anzac cookie", qty=1, unit="piece", kcal_per_unit=140,
                 protein_per_unit=2.5, fat_per_unit=7.0, carbs_per_unit=16.0,
                 confidence=0.45),
        MealDish(name="cappuccino", qty=1, unit="cup", kcal_per_unit=120,
                 protein_per_unit=4.0, fat_per_unit=3.5, carbs_per_unit=10.0,
                 confidence=0.4, note="not visible in photo but mentioned by user"),
    ])
    client = _FakeClient({"cappuccino": [CAPPUCCINO]})

    result = await orchestrate(
        ExtractedMessage(entries=[ExtractedEntry(name="Anzac cookie")]),
        client,
        photos=[tmp_path / "cafe.jpg"],
        vision_model=object(),
        message_text="Anzac cookie (photo attached). And cappuccino",
    )

    calls = _log_kwargs_by_food(client)
    assert calls["cappuccino"]["source"] == "cache_lookup"
    assert calls["cappuccino"]["kcal_per_unit"] == 80
    assert calls["cappuccino"]["confidence"] == 0.95
    # The cookie IS in the photo and is not cached — it keeps its estimate.
    assert calls["Anzac cookie"]["source"] == "photo_estimate"
    assert calls["Anzac cookie"]["kcal_per_unit"] == 140
    assert len(result.logged) == 2


@pytest.mark.asyncio
async def test_uncached_dish_keeps_photo_estimate(monkeypatch, tmp_path):
    _patch_vision(monkeypatch, [
        MealDish(name="jungle curry", qty=1, unit="bowl", kcal_per_unit=470,
                 protein_per_unit=30, fat_per_unit=20, carbs_per_unit=40),
    ])
    client = _FakeClient()

    await orchestrate(
        ExtractedMessage(entries=[ExtractedEntry(name="Thai dinner")]),
        client,
        photos=[tmp_path / "thai.jpg"],
        vision_model=object(),
    )

    assert _log_kwargs_by_food(client)["jungle curry"]["source"] == "photo_estimate"


@pytest.mark.asyncio
async def test_weight_based_cache_entry_does_not_override_a_piece_count(
    monkeypatch, tmp_path
):
    """Guards the 100x bug: "1 piece" must never log against per-100g rates."""
    _patch_vision(monkeypatch, [
        MealDish(name="manchego", qty=1, unit="slice", kcal_per_unit=110,
                 protein_per_unit=7, fat_per_unit=9, carbs_per_unit=0.5),
    ])
    client = _FakeClient({"manchego": [{
        "name": "manchego", "unit": "100g", "qty_default": 1,
        "kcal_per_unit": 390, "protein_per_unit": 25,
        "fat_per_unit": 32, "carbs_per_unit": 1.5,
    }]})

    await orchestrate(
        ExtractedMessage(entries=[ExtractedEntry(name="cheese plate")]),
        client,
        photos=[tmp_path / "cheese.jpg"],
        vision_model=object(),
    )

    call = _log_kwargs_by_food(client)["manchego"]
    assert call["source"] == "photo_estimate"
    assert call["kcal_per_unit"] == 110


@pytest.mark.asyncio
async def test_ambiguous_cache_match_keeps_estimate_and_asks_nothing(
    monkeypatch, tmp_path
):
    """A photo turn must not stall on a disambiguation question."""
    _patch_vision(monkeypatch, [
        MealDish(name="latte", qty=1, unit="cup", kcal_per_unit=150,
                 protein_per_unit=8, fat_per_unit=8, carbs_per_unit=12),
    ])
    client = _FakeClient({"latte": [
        {"name": "oat latte", "unit": "cup", "qty_default": 1, "kcal_per_unit": 120,
         "protein_per_unit": 3, "fat_per_unit": 5, "carbs_per_unit": 15},
        {"name": "iced latte", "unit": "cup", "qty_default": 1, "kcal_per_unit": 90,
         "protein_per_unit": 5, "fat_per_unit": 4, "carbs_per_unit": 9},
    ]})

    result = await orchestrate(
        ExtractedMessage(entries=[ExtractedEntry(name="coffee")]),
        client,
        photos=[tmp_path / "coffee.jpg"],
        vision_model=object(),
    )

    assert result.pending_clarification is None
    assert _log_kwargs_by_food(client)["latte"]["source"] == "photo_estimate"


@pytest.mark.parametrize("dish_unit, cache_unit, expected", [
    ("cup", "cup", True),
    ("Cup", " cup ", True),          # normalisation
    ("serving", "cup", True),        # generic stands in for a real unit
    ("", "bowl", True),
    ("g", "100g", True),             # _apply_100g_rule converts
    ("piece", "100g", False),        # the 100x trap
    ("100g", "slice", False),
    ("slice", "bowl", False),        # two real, unrelated units
])
def test_units_compatible_matrix(dish_unit, cache_unit, expected):
    assert _units_compatible(dish_unit, cache_unit) is expected


# --- vision dishes given in grams (19-08-2026) ---
#
# `_log_meal_dish` passed the model's qty/unit/rates straight through — the
# one path that let the LLM do its own per-unit arithmetic. On the real Thai
# fixture it returned qty=100 unit="g" with PER-100G rates and logged
# 100 x 195 = 19,500 kcal of jasmine rice. Physical bound: nothing exceeds
# ~9 kcal/g (pure fat is 9), so 195 cannot be a per-gram figure.

from nutrition_clerk.workflow.orchestrator import _normalise_dish_weight  # noqa: E402


def test_impossible_per_gram_rate_is_read_as_per_100g():
    dish = MealDish(name="jasmine rice", qty=100, unit="g", kcal_per_unit=195,
                    protein_per_unit=4.0, fat_per_unit=0.4, carbs_per_unit=43.0)
    qty, unit, per_unit = _normalise_dish_weight(dish)
    assert (qty, unit) == (1.0, "100g")
    assert per_unit["kcal"] == 195
    assert qty * per_unit["kcal"] == 195, "must not be 19,500"


def test_plausible_per_gram_rate_is_rescaled_to_per_100g():
    dish = MealDish(name="kipper", qty=200, unit="g", kcal_per_unit=1.22,
                    protein_per_unit=0.08, fat_per_unit=0.094, carbs_per_unit=0.0135)
    qty, unit, per_unit = _normalise_dish_weight(dish)
    assert (qty, unit) == (2.0, "100g")
    assert round(per_unit["kcal"], 1) == 122.0
    assert round(qty * per_unit["kcal"]) == 244


def test_millilitres_land_on_the_100ml_basis():
    dish = MealDish(name="tomato juice", qty=250, unit="ml", kcal_per_unit=18,
                    protein_per_unit=0.9, fat_per_unit=0.1, carbs_per_unit=3.9)
    qty, unit, _ = _normalise_dish_weight(dish)
    assert (qty, unit) == (2.5, "100ml")


def test_natural_units_are_left_alone():
    dish = MealDish(name="anzac cookie", qty=1, unit="piece", kcal_per_unit=140,
                    protein_per_unit=2.5, fat_per_unit=7.0, carbs_per_unit=16.0)
    assert _normalise_dish_weight(dish)[:2] == (1.0, "piece")


def test_gram_label_on_a_portion_count_is_not_divided():
    """qty=1 unit="g" with per-100g rates is a mislabelled portion, not 1 gram."""
    dish = MealDish(name="jungle curry", qty=1, unit="g", kcal_per_unit=400,
                    protein_per_unit=30, fat_per_unit=20, carbs_per_unit=25)
    qty, unit, per_unit = _normalise_dish_weight(dish)
    assert (qty, unit) == (1.0, "100g")
    assert qty * per_unit["kcal"] == 400, "must not divide down to 4 kcal"


# --- a dish override must be the SAME food (21-08-2026) ---
#
# "Log my lunch: potato and artichoke salad" + a photo of the plate.
# Vision read it correctly (1 serving, 181 kcal, confidence 0.9), but
# lookup_food is a substring matcher: "potato" sits inside "potato and
# artichoke salad", and a generic "serving" unit at qty=1 is compatible with
# anything, so the per-potato entry overrode the salad and logged 103 kcal.
#
# Same fault as "salmon traybake" vs "salmon", which was fixed in
# _usable_hits. This path had its own lookup and never got it.

POTATO = {
    "name": "potato", "aliases": ["potatoes"],
    "unit": "potato", "qty_default": 1, "kcal_per_unit": 103,
    "protein_per_unit": 2.3, "fat_per_unit": 0.1, "carbs_per_unit": 24.0,
}


@pytest.mark.asyncio
async def test_substring_cache_entry_does_not_override_a_composite_dish(
    monkeypatch, tmp_path
):
    _patch_vision(monkeypatch, [
        MealDish(name="potato and artichoke salad", qty=1, unit="serving",
                 kcal_per_unit=181, protein_per_unit=0, fat_per_unit=0,
                 carbs_per_unit=0, confidence=0.9),
    ])
    client = _FakeClient({"potato and artichoke salad": [POTATO]})

    await orchestrate(
        ExtractedMessage(entries=[ExtractedEntry(name="Potato and artichoke salad")]),
        client,
        photos=[tmp_path / "salad.jpg"],
        vision_model=object(),
    )

    call = _log_kwargs_by_food(client)["potato and artichoke salad"]
    assert call["source"] == "photo_estimate", "a substring match overrode the dish"
    assert call["kcal_per_unit"] == 181


@pytest.mark.asyncio
async def test_exactly_named_dish_still_takes_the_cache(monkeypatch, tmp_path):
    """The cappuccino fix must keep working — exact names still override."""
    _patch_vision(monkeypatch, [
        MealDish(name="cappuccino", qty=1, unit="cup", kcal_per_unit=120,
                 protein_per_unit=4.0, fat_per_unit=3.5, carbs_per_unit=10.0,
                 confidence=0.4),
    ])
    client = _FakeClient({"cappuccino": [CAPPUCCINO]})

    await orchestrate(
        ExtractedMessage(entries=[ExtractedEntry(name="Coffee")]),
        client,
        photos=[tmp_path / "cafe.jpg"],
        vision_model=object(),
    )

    call = _log_kwargs_by_food(client)["cappuccino"]
    assert call["source"] == "cache_lookup"
    assert call["kcal_per_unit"] == 80
