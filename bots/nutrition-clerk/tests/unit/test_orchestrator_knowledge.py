"""Unit tests for N6: knowledge-enricher branch in the orchestrator."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from nutrition_clerk.workflow.context import ChatContext
from nutrition_clerk.workflow.orchestrator import orchestrate
from nutrition_clerk.workflow.schemas import ExtractedEntry, ExtractedMessage, KnowledgeExtract


class _FakeClient:
    """Cache always misses (both substring and fuzzy) so the knowledge
    enricher is the only remaining path."""

    def __init__(self):
        self.lookup_food = AsyncMock(return_value=[])
        self.add_personal_food = AsyncMock(return_value={"status": "ok"})
        self.fuzzy_lookup = lambda q, **kw: []

        async def _echo_log(**kw):
            qty = kw.get("qty", 1)
            # Mirror the real MCP server, which sentence-cases the food name
            # in its write path (food_cache.normalize_food_name). Keeping the
            # fake faithful means these tests assert production behaviour.
            name = kw.get("food", "") or ""
            name = (name[0].upper() + name[1:]) if name else name
            return {
                "entry": {
                    "food": name,
                    "kcal_total": qty * kw.get("kcal_per_unit", 0),
                    "protein_total": qty * kw.get("protein_per_unit", 0),
                    "fat_total": qty * kw.get("fat_per_unit", 0),
                    "carbs_total": qty * kw.get("carbs_per_unit", 0),
                },
                "today": {"kcal": 105, "protein": 1.3, "fat": 0.4, "carbs": 27},
            }
        self.log_food = AsyncMock(side_effect=_echo_log)


_SENTINEL_MODEL = object()   # only needs to be non-None; estimate_macros is patched


def _banana_estimate(**overrides) -> KnowledgeExtract:
    data = dict(
        refused=False, unit="banana",
        kcal_per_unit=105, protein_per_unit=1.3,
        fat_per_unit=0.4, carbs_per_unit=27,
        confidence=0.65, note="medium banana ~118g",
    )
    data.update(overrides)
    return KnowledgeExtract(**data)


@pytest.mark.asyncio
async def test_cache_miss_logs_knowledge_estimate():
    client = _FakeClient()
    msg = ExtractedMessage(entries=[ExtractedEntry(name="banana", qty=1, unit="banana")])

    with patch(
        "nutrition_clerk.workflow.orchestrator.estimate_macros",
        AsyncMock(return_value=_banana_estimate()),
    ):
        result = await orchestrate(msg, client, knowledge_model=_SENTINEL_MODEL)

    assert len(result.logged) == 1
    assert result.unknown == []
    row = result.logged[0]
    assert row.source == "text_estimate"
    assert row.estimated is True
    # log_food called with the model's confidence, not a hardcoded one.
    kwargs = client.log_food.call_args.kwargs
    assert kwargs["source"] == "text_estimate"
    assert kwargs["confidence"] == 0.65
    assert kwargs["kcal_per_unit"] == 105


@pytest.mark.asyncio
async def test_refusal_becomes_unknown_entry():
    client = _FakeClient()
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="Trader Joe's Kimchi Bowl", qty=1),
    ])
    refusal = KnowledgeExtract(
        refused=True,
        refusal_reason="branded product; macros vary by manufacturer",
    )
    with patch(
        "nutrition_clerk.workflow.orchestrator.estimate_macros",
        AsyncMock(return_value=refusal),
    ):
        result = await orchestrate(msg, client, knowledge_model=_SENTINEL_MODEL)

    assert result.logged == []
    assert len(result.unknown) == 1
    assert "branded" in result.unknown[0].reason
    client.log_food.assert_not_called()


@pytest.mark.asyncio
async def test_knowledge_estimate_never_saved_to_cache():
    """CRITICAL invariant: an LLM guess must not seed personal-foods."""
    client = _FakeClient()
    msg = ExtractedMessage(entries=[
        ExtractedEntry(name="banana", qty=1, unit="banana", save_to_cache=True),
    ])
    with patch(
        "nutrition_clerk.workflow.orchestrator.estimate_macros",
        AsyncMock(return_value=_banana_estimate()),
    ):
        result = await orchestrate(msg, client, knowledge_model=_SENTINEL_MODEL)

    client.add_personal_food.assert_not_called()
    assert result.logged[0].save_status == "ineligible"


@pytest.mark.asyncio
async def test_grams_plus_per_100g_estimate_applies_100g_rule():
    """User says '300g kefir', model returns per-100g -> qty=3, unit=100g."""
    client = _FakeClient()
    msg = ExtractedMessage(entries=[ExtractedEntry(name="kefir", qty=300, unit="g")])
    est = KnowledgeExtract(
        refused=False, unit="100g",
        kcal_per_unit=57, protein_per_unit=3.3,
        fat_per_unit=3.3, carbs_per_unit=4.7, confidence=0.6,
    )
    with patch(
        "nutrition_clerk.workflow.orchestrator.estimate_macros",
        AsyncMock(return_value=est),
    ):
        result = await orchestrate(msg, client, knowledge_model=_SENTINEL_MODEL)

    kwargs = client.log_food.call_args.kwargs
    assert kwargs["unit"] == "100g"
    assert kwargs["qty"] == 3.0
    assert kwargs["kcal_per_unit"] == 57
    # Guard against the 100x bug: total must be ~171, not 17100.
    assert 170 <= result.logged[0].kcal_total <= 172


@pytest.mark.asyncio
async def test_knowledge_row_appended_to_recent_ring():
    client = _FakeClient()
    ctx = ChatContext(chat_id=1)
    msg = ExtractedMessage(entries=[ExtractedEntry(name="banana", qty=1, unit="banana")])
    with patch(
        "nutrition_clerk.workflow.orchestrator.estimate_macros",
        AsyncMock(return_value=_banana_estimate()),
    ):
        await orchestrate(msg, client, knowledge_model=_SENTINEL_MODEL, context=ctx)

    assert len(ctx.recent_entries) == 1
    # Names are sentence-cased for display before logging (see _display_name).
    assert ctx.recent_entries[0].name == "Banana"


@pytest.mark.asyncio
async def test_no_knowledge_model_falls_back_to_unknown():
    """Backwards-compatible: without a knowledge model, cache miss -> unknown."""
    client = _FakeClient()
    msg = ExtractedMessage(entries=[ExtractedEntry(name="banana", qty=1)])
    result = await orchestrate(msg, client)   # no knowledge_model
    assert result.logged == []
    assert len(result.unknown) == 1
    client.log_food.assert_not_called()
