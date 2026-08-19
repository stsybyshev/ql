"""A label photo must be logged against the entry it actually depicts.

Regression origin — 19-08-2026:

    "Lunch: Spanish eggs, 150g of pomegranate juice, small orange,
     30g of Apricot yogurt (label attached)"                + 1 yogurt label

The orchestrator consumed photos POSITIONALLY: the first entry to reach the
photo branch took `photos[0]`, whatever it depicted. That was "Spanish eggs"
(qty=null), so `_log_shape_c_label` ran with the yogurt's label and the eggs'
entry, hit its "no gram quantity" fallback, and wrote 1 x 100g. Three faults
from one line:

  * the yogurt logged at 100g instead of 30g
  * Spanish eggs never logged at all — `continue` skipped its own lookup
  * the yogurt logged a SECOND time, as a knowledge estimate, when entry #4
    found no photo left

Ownership is now decided before the loop:
  1. the user's own marker ("(label attached)") -> photo_index
  2. a per-100g label needs an entry carrying a weight quantity
  3. otherwise ask, never guess
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nutrition_clerk.workflow import orchestrator as orch
from nutrition_clerk.workflow.orchestrator import orchestrate
from nutrition_clerk.workflow.schemas import (
    ExtractedEntry,
    ExtractedMessage,
    LabelExtract,
    PhotoExtract,
)

SPANISH_EGGS = {
    "name": "Spanish eggs",
    "aliases": ["spanish eggs", "huevos", "fried eggs with cumin", "spicy eggs"],
    "unit": "serving", "qty_default": 1, "kcal_per_unit": 280,
    "protein_per_unit": 19.0, "fat_per_unit": 20.0, "carbs_per_unit": 2.5,
}
POMEGRANATE = {
    "name": "pomegranate juice", "aliases": [],
    "unit": "100g", "qty_default": 1, "kcal_per_unit": 54,
    "protein_per_unit": 0.2, "fat_per_unit": 0.3, "carbs_per_unit": 13.1,
}
ORANGE = {
    "name": "orange", "aliases": ["oranges", "small orange"],
    "unit": "orange", "qty_default": 1, "kcal_per_unit": 62,
    "protein_per_unit": 1.2, "fat_per_unit": 0.2, "carbs_per_unit": 15.4,
}

YOGURT_LABEL = LabelExtract(
    label_name="Benecol Yogurt Drink (Apricot flavour)",
    kcal_per_100g=48, protein_per_100g=2.8, fat_per_100g=2.1, carbs_per_100g=3.9,
)


class _FakeClient:
    def __init__(self, by_name):
        self._by_name = {k.lower(): v for k, v in by_name.items()}
        self.log_food = AsyncMock(return_value={"entry": {}, "today": {}})
        self.add_personal_food = AsyncMock(return_value={"status": "ok"})
        self.fuzzy_lookup = lambda q, **kw: []

    async def lookup_food(self, name):
        return self._by_name.get(name.strip().lower(), [])


def _the_lunch_message(*, yogurt_photo_index=None):
    """The real message, as the extractor parsed it."""
    return ExtractedMessage(entries=[
        ExtractedEntry(name="Spanish eggs"),
        ExtractedEntry(name="Pomegranate juice", qty=150, unit="g"),
        ExtractedEntry(name="Small orange", qty=1),
        ExtractedEntry(name="Apricot yogurt", qty=30, unit="g",
                       photo_index=yogurt_photo_index),
    ])


def _patch_vision(monkeypatch, extracts):
    calls = iter(extracts)

    async def _fake(model, photo, hint_text=""):
        return next(calls)

    monkeypatch.setattr(orch, "analyse_photo", _fake)


def _by_food(client):
    return {c.kwargs["food"]: c.kwargs for c in client.log_food.call_args_list}


@pytest.fixture
def client():
    return _FakeClient({
        "Spanish eggs": [SPANISH_EGGS],
        "Pomegranate juice": [POMEGRANATE],
        "Small orange": [ORANGE],
        "Apricot yogurt": [],
    })


@pytest.mark.asyncio
async def test_label_goes_to_the_marked_entry_not_the_first(
    monkeypatch, tmp_path, client
):
    """photo_index from '(label attached)' picks entry #4."""
    _patch_vision(monkeypatch, [PhotoExtract(kind="label", label=YOGURT_LABEL)])

    result = await orchestrate(
        _the_lunch_message(yogurt_photo_index=0),
        client,
        photos=[tmp_path / "yogurt.jpg"],
        vision_model=object(),
        message_text="Lunch: Spanish eggs, 150g of pomegranate juice, small "
                     "orange, 30g of Apricot yogurt (label attached)",
    )

    calls = _by_food(client)
    assert result.pending_clarification is None

    # The yogurt owns the label and keeps ITS OWN 30g.
    yog = next(v for k, v in calls.items() if "yogurt" in k.lower())
    assert yog["source"] == "photo_label"
    assert yog["unit"] == "100g"
    assert abs(yog["qty"] - 0.3) < 1e-9, "30g must be 0.3 x 100g, not 1 x 100g"

    # Spanish eggs must log on its own merits — it used to vanish.
    assert "Spanish eggs" in calls, f"Spanish eggs was dropped: {list(calls)}"
    assert calls["Spanish eggs"]["source"] == "cache_lookup"
    assert calls["Spanish eggs"]["kcal_per_unit"] == 280

    # Four rows in, four rows out — no phantom, no duplicate.
    assert len(client.log_food.call_args_list) == 4


@pytest.mark.asyncio
async def test_entry_without_a_weight_cannot_own_a_per_100g_label(
    monkeypatch, tmp_path, client
):
    """Layer 1 alone, with no marker: Spanish eggs is ruled out deterministically.

    Two weighted candidates remain (pomegranate 150g, yogurt 30g), so this
    must ASK rather than guess — but it must never hand the label to an entry
    that has no weight to scale it by.
    """
    _patch_vision(monkeypatch, [PhotoExtract(kind="label", label=YOGURT_LABEL)])

    result = await orchestrate(
        _the_lunch_message(yogurt_photo_index=None),
        client,
        photos=[tmp_path / "yogurt.jpg"],
        vision_model=object(),
    )

    assert result.pending_clarification is not None
    assert "Spanish eggs" not in (result.pending_clarification or ""), (
        "an entry with no weight quantity is not a candidate for a per-100g label"
    )
    for food, kwargs in _by_food(client).items():
        assert kwargs["source"] != "photo_label", (
            f"{food} was given the label despite the attribution being ambiguous"
        )


@pytest.mark.asyncio
async def test_single_weighted_entry_resolves_without_a_marker(
    monkeypatch, tmp_path
):
    """"Spanish eggs, 30g apricot yogurt" + photo needs no marker at all."""
    _patch_vision(monkeypatch, [PhotoExtract(kind="label", label=YOGURT_LABEL)])
    client = _FakeClient({"Spanish eggs": [SPANISH_EGGS], "Apricot yogurt": []})

    result = await orchestrate(
        ExtractedMessage(entries=[
            ExtractedEntry(name="Spanish eggs"),
            ExtractedEntry(name="Apricot yogurt", qty=30, unit="g"),
        ]),
        client,
        photos=[tmp_path / "yogurt.jpg"],
        vision_model=object(),
    )

    assert result.pending_clarification is None
    calls = _by_food(client)
    yog = next(v for k, v in calls.items() if "yogurt" in k.lower())
    assert yog["source"] == "photo_label"
    assert abs(yog["qty"] - 0.3) < 1e-9
    assert calls["Spanish eggs"]["source"] == "cache_lookup"
