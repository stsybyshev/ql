"""Unit tests for workflow.orchestrator — no LLM, mocked food_cache_client."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nutrition_clerk.workflow.orchestrator import (
    _apply_100g_rule,
    _resolve_datetime,
    orchestrate,
)
from nutrition_clerk.workflow.schemas import ExtractedEntry, ExtractedMessage


class _FakeClient:
    """AsyncMock-based food_cache_client that captures call kwargs."""

    def __init__(self, *, lookup_returns=None, log_returns=None, add_returns=None,
                 fuzzy_returns=None):
        self.lookup_food = AsyncMock(return_value=lookup_returns or [])
        self.log_food = AsyncMock(return_value=log_returns or {"entry": {}, "today": {}})
        self.add_personal_food = AsyncMock(return_value=add_returns or {"status": "ok"})
        # N4.5: fuzzy fallback runs whenever lookup_food misses.
        self.fuzzy_lookup = lambda q, **kw: (fuzzy_returns or [])


# -----------------------------------------------------------------------------
# _resolve_datetime — N1 minimal
# -----------------------------------------------------------------------------

def test_resolve_datetime_none_returns_now_format():
    got = _resolve_datetime(None)
    assert len(got) == 16 and got[2] == "-" and got[5] == "-" and got[10] == " "


def test_resolve_datetime_structured_passthrough():
    assert _resolve_datetime("15-03-2026 08:30") == "15-03-2026 08:30"


def test_resolve_datetime_handles_natural_phrasing():
    """N6 replaced the minimal N1 parser with the full resolver — natural
    phrasing now resolves instead of falling back to now().
    See tests/unit/test_datetime_resolver.py for the exhaustive matrix."""
    got = _resolve_datetime("this morning")
    assert len(got) == 16
    assert got.endswith("09:00")


def test_resolve_datetime_unrecognised_falls_back(caplog):
    got = _resolve_datetime("sometime around the heat death of the universe")
    assert len(got) == 16
    assert any("not recognised" in r.getMessage() for r in caplog.records)


# -----------------------------------------------------------------------------
# _apply_100g_rule
# -----------------------------------------------------------------------------

def test_100g_rule_converts_grams_to_100g():
    entry = ExtractedEntry(name="kefir", qty=300, unit="g")
    hit = {"unit": "100g", "qty_default": 1}
    qty, unit = _apply_100g_rule(entry, hit)
    assert qty == 3.0
    assert unit == "100g"


def test_100g_rule_pass_through_when_units_align():
    entry = ExtractedEntry(name="apple", qty=1, unit="apple")
    hit = {"unit": "apple", "qty_default": 1}
    qty, unit = _apply_100g_rule(entry, hit)
    assert (qty, unit) == (1, "apple")


def test_100g_rule_uses_qty_default_when_missing():
    entry = ExtractedEntry(name="cashews", qty=None, unit=None)
    hit = {"unit": "g", "qty_default": 50}
    qty, unit = _apply_100g_rule(entry, hit)
    assert qty == 50


# -----------------------------------------------------------------------------
# orchestrate — SHAPE A (cache lookup)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shape_a_cache_hit_logs_row():
    client = _FakeClient(
        lookup_returns=[{
            "name": "Apple",
            "unit": "apple",
            "qty_default": 1,
            "kcal_per_unit": 95,
            "protein_per_unit": 0.5,
            "fat_per_unit": 0.3,
            "carbs_per_unit": 25,
        }],
        log_returns={"entry": {"food": "Apple", "kcal_total": 95},
                     "today": {"kcal": 95, "protein": 0.5, "fat": 0.3, "carbs": 25}},
    )
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="apple", qty=1, unit="apple"),
    ])
    result = await orchestrate(msg, client)
    assert len(result.logged) == 1
    assert result.unknown == []
    client.lookup_food.assert_awaited_once_with("apple")
    client.log_food.assert_awaited_once()
    kwargs = client.log_food.call_args.kwargs
    assert kwargs["source"] == "cache_lookup"
    assert kwargs["confidence"] == 0.95
    assert kwargs["food"] == "Apple"


@pytest.mark.asyncio
async def test_shape_a_cache_miss_yields_unknown():
    client = _FakeClient(lookup_returns=[])
    msg = ExtractedMessage(entries=[ExtractedEntry(name="obscure food")])
    result = await orchestrate(msg, client)
    assert result.logged == []
    assert len(result.unknown) == 1
    assert result.unknown[0].name == "obscure food"
    client.log_food.assert_not_called()


# -----------------------------------------------------------------------------
# orchestrate — SHAPE B (typed macros)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shape_b_skips_cache_and_logs_text_estimate():
    client = _FakeClient(
        log_returns={"entry": {"food": "Chia pudding"},
                     "today": {"kcal": 300, "protein": 12, "fat": 20, "carbs": 8}},
    )
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="Chia pudding", kcal=300, protein_g=12, fat_g=20, carbs_g=8),
    ])
    result = await orchestrate(msg, client)
    client.lookup_food.assert_not_called()   # cache skipped for SHAPE B
    client.log_food.assert_awaited_once()
    kwargs = client.log_food.call_args.kwargs
    assert kwargs["source"] == "text_estimate"
    assert kwargs["confidence"] == 0.85
    assert kwargs["unit"] == "serving"
    assert kwargs["qty"] == 1.0
    assert kwargs["kcal_per_unit"] == 300
    assert kwargs["protein_per_unit"] == 12
    assert result.logged[0].source == "text_estimate"
    assert result.logged[0].save_status is None   # save_to_cache=False (default)


@pytest.mark.asyncio
async def test_shape_b_with_save_calls_add_personal_food():
    client = _FakeClient(
        log_returns={"entry": {}, "today": {}},
        add_returns={"status": "ok"},
    )
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="Chia pudding", kcal=300, protein_g=12, fat_g=20,
                       carbs_g=8, save_to_cache=True),
    ])
    result = await orchestrate(msg, client)
    client.add_personal_food.assert_awaited_once()
    kwargs = client.add_personal_food.call_args.kwargs
    assert kwargs["name"] == "Chia pudding"
    assert kwargs["kcal_per_unit"] == 300
    assert kwargs["unit"] == "serving"
    assert result.logged[0].save_status == "saved"


@pytest.mark.asyncio
async def test_shape_b_save_duplicate_recorded_as_duplicate():
    client = _FakeClient(
        add_returns={"error": "Alias 'chia pudding' already exists in entry 'Chia pudding'"},
    )
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="Chia pudding", kcal=300, protein_g=12, fat_g=20,
                       carbs_g=8, save_to_cache=True),
    ])
    result = await orchestrate(msg, client)
    assert result.logged[0].save_status == "duplicate"


@pytest.mark.asyncio
async def test_shape_a_with_save_marked_ineligible():
    """Cache-hit + save_to_cache: no-op (food already saved). save_status=ineligible."""
    client = _FakeClient(
        lookup_returns=[{
            "name": "Apple", "unit": "apple", "qty_default": 1,
            "kcal_per_unit": 95, "protein_per_unit": 0.5,
            "fat_per_unit": 0.3, "carbs_per_unit": 25,
        }],
    )
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="apple", qty=1, unit="apple", save_to_cache=True),
    ])
    result = await orchestrate(msg, client)
    assert result.logged[0].save_status == "ineligible"
    client.add_personal_food.assert_not_called()


# -----------------------------------------------------------------------------
# Non-food short-circuit
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_not_food_related_empty_result():
    client = _FakeClient()
    msg = ExtractedMessage(is_food_related=False, entries=[])
    result = await orchestrate(msg, client)
    assert result.logged == [] and result.unknown == []
    client.lookup_food.assert_not_called()
    client.log_food.assert_not_called()
