"""Unit tests for N3: SHAPE 6.4 recent-meal promotion + recent-entry snapshotting."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nutrition_clerk.workflow.context import ChatContext
from nutrition_clerk.workflow.orchestrator import (
    RecentEntry,
    _find_recent,
    orchestrate,
)
from nutrition_clerk.workflow.schemas import ExtractedEntry, ExtractedMessage


def _hit(name: str) -> dict:
    return {
        "name": name, "unit": "cup", "qty_default": 1,
        "kcal_per_unit": 90, "protein_per_unit": 0.5,
        "fat_per_unit": 0, "carbs_per_unit": 22,
    }


class _FakeClient:
    def __init__(self, lookup_returns=None, add_returns=None):
        self.lookup_food = AsyncMock(return_value=lookup_returns or [])

        async def _echo_log(**kw):
            # Echo what was passed in so downstream tests see the right food name.
            qty = kw.get("qty", 1)
            return {
                "entry": {
                    "food": kw.get("food", ""),
                    "kcal_total": qty * kw.get("kcal_per_unit", 0),
                    "protein_total": qty * kw.get("protein_per_unit", 0),
                    "fat_total": qty * kw.get("fat_per_unit", 0),
                    "carbs_total": qty * kw.get("carbs_per_unit", 0),
                },
                "today": {"kcal": 100, "protein": 5, "fat": 3, "carbs": 10},
            }
        self.log_food = AsyncMock(side_effect=_echo_log)
        self.add_personal_food = AsyncMock(return_value=add_returns or {"status": "ok"})
        self.fuzzy_lookup = lambda q: []


# --- _find_recent ---

def test_find_recent_exact_match():
    ring = [
        RecentEntry(name="Pomegranate juice", qty=1, unit="cup",
                    kcal_per_unit=90, protein_per_unit=0.5,
                    fat_per_unit=0, carbs_per_unit=22, source="text_estimate"),
    ]
    match = _find_recent("pomegranate juice", ring)
    assert match is not None and match.name == "Pomegranate juice"


def test_find_recent_fuzzy_match():
    ring = [
        RecentEntry(name="Pomegranate juice", qty=1, unit="cup",
                    kcal_per_unit=90, protein_per_unit=0.5,
                    fat_per_unit=0, carbs_per_unit=22, source="text_estimate"),
    ]
    # typo tolerance — threshold 70 is generous
    assert _find_recent("pomegranate", ring) is not None
    assert _find_recent("pom juice", ring) is not None


def test_find_recent_returns_none_when_nothing_close():
    ring = [
        RecentEntry(name="Pomegranate juice", qty=1, unit="cup",
                    kcal_per_unit=90, protein_per_unit=0.5,
                    fat_per_unit=0, carbs_per_unit=22, source="text_estimate"),
    ]
    assert _find_recent("chicken curry", ring) is None
    assert _find_recent("", ring) is None


def test_find_recent_picks_best_of_multiple():
    ring = [
        RecentEntry(name="Coffee", qty=1, unit="cup",
                    kcal_per_unit=2, protein_per_unit=0.3, fat_per_unit=0, carbs_per_unit=0.2,
                    source="cache_lookup"),
        RecentEntry(name="Pomegranate juice", qty=1, unit="cup",
                    kcal_per_unit=90, protein_per_unit=0.5, fat_per_unit=0, carbs_per_unit=22,
                    source="text_estimate"),
    ]
    match = _find_recent("pomegranate", ring)
    assert match is not None and match.name == "Pomegranate juice"


# --- orchestrate SHAPE 6.4 branch ---

@pytest.mark.asyncio
async def test_shape_6_4_promotes_recent_entry():
    client = _FakeClient(add_returns={"status": "ok"})
    ctx = ChatContext(chat_id=1)
    ctx.recent_entries.append(RecentEntry(
        name="Pomegranate juice", qty=1, unit="cup",
        kcal_per_unit=90, protein_per_unit=0.5,
        fat_per_unit=0, carbs_per_unit=22, source="text_estimate",
    ))
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="pomegranate juice", reference_recent=True, save_to_cache=True),
    ])
    result = await orchestrate(msg, client, context=ctx)

    # add_personal_food called with the values from the recent entry.
    client.add_personal_food.assert_awaited_once()
    kwargs = client.add_personal_food.call_args.kwargs
    assert kwargs["name"] == "Pomegranate juice"
    assert kwargs["unit"] == "cup"
    assert kwargs["kcal_per_unit"] == 90
    assert kwargs["protein_per_unit"] == 0.5
    assert kwargs["carbs_per_unit"] == 22
    # log_food must NOT be called for SHAPE 6.4 — would double-log.
    client.log_food.assert_not_called()
    # Result reports the promotion.
    assert len(result.saved_recent) == 1
    assert result.saved_recent[0].status == "saved"
    assert result.saved_recent[0].name == "Pomegranate juice"
    # Nothing else in the result.
    assert result.logged == []
    assert result.unknown == []


@pytest.mark.asyncio
async def test_shape_6_4_reports_duplicate_gracefully():
    client = _FakeClient(add_returns={"error": "Alias 'x' already exists"})
    ctx = ChatContext(chat_id=1)
    ctx.recent_entries.append(RecentEntry(
        name="Chia pudding", qty=1, unit="serving",
        kcal_per_unit=220, protein_per_unit=6,
        fat_per_unit=9, carbs_per_unit=28, source="text_estimate",
    ))
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="chia pudding", reference_recent=True, save_to_cache=True),
    ])
    result = await orchestrate(msg, client, context=ctx)
    assert result.saved_recent[0].status == "duplicate"


@pytest.mark.asyncio
async def test_shape_6_4_not_found_when_ring_empty():
    client = _FakeClient()
    ctx = ChatContext(chat_id=1)   # empty ring
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="pomegranate juice", reference_recent=True, save_to_cache=True),
    ])
    result = await orchestrate(msg, client, context=ctx)
    client.add_personal_food.assert_not_called()
    assert result.saved_recent[0].status == "not_found"


@pytest.mark.asyncio
async def test_shape_6_4_not_found_when_no_context():
    """No context (never happens in production, but defensive)."""
    client = _FakeClient()
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="pomegranate juice", reference_recent=True),
    ])
    result = await orchestrate(msg, client)  # no context param
    assert result.saved_recent[0].status == "not_found"
    client.add_personal_food.assert_not_called()


# --- recent-entries snapshot on successful log ---

@pytest.mark.asyncio
async def test_shape_a_cache_hit_appends_to_recent():
    client = _FakeClient(lookup_returns=[_hit("Pomegranate juice")])
    ctx = ChatContext(chat_id=1)
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="pomegranate juice", qty=1, unit="cup"),
    ])
    result = await orchestrate(msg, client, context=ctx)
    assert len(result.logged) == 1
    assert len(ctx.recent_entries) == 1
    recent = ctx.recent_entries[0]
    assert recent.name == "Pomegranate juice"
    assert recent.qty == 1
    assert recent.unit == "cup"
    assert recent.kcal_per_unit == 90
    assert recent.source == "cache_lookup"


@pytest.mark.asyncio
async def test_shape_b_typed_macros_appends_to_recent():
    client = _FakeClient()
    ctx = ChatContext(chat_id=1)
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="Chia pudding", kcal=300, protein_g=12, fat_g=20, carbs_g=8),
    ])
    result = await orchestrate(msg, client, context=ctx)
    assert len(result.logged) == 1
    assert len(ctx.recent_entries) == 1
    recent = ctx.recent_entries[0]
    assert recent.name == "Chia pudding"
    assert recent.source == "text_estimate"
    # Per-unit for SHAPE B = totals (qty=1 by convention)
    assert recent.kcal_per_unit == 300


@pytest.mark.asyncio
async def test_recent_ring_bounded_at_10():
    """The ring's maxlen=10 should silently drop oldest entries."""
    client = _FakeClient(lookup_returns=[_hit("Food")])
    ctx = ChatContext(chat_id=1)
    for i in range(15):
        msg = ExtractedMessage(entries=[
            ExtractedEntry(name=f"food_{i}", qty=1, unit="serving"),
        ])
        # Change the lookup return per iteration so recent entries have distinct names.
        client.lookup_food = AsyncMock(return_value=[_hit(f"Food_{i}")])
        await orchestrate(msg, client, context=ctx)
    assert len(ctx.recent_entries) == 10   # oldest 5 dropped
