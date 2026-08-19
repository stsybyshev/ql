"""Unit tests for workflow.formatter — reply text shape."""
from __future__ import annotations

from nutrition_clerk.workflow.formatter import format_reply
from nutrition_clerk.workflow.orchestrator import LoggedRow, OrchestratorResult, UnknownEntry


def _row(**kw) -> LoggedRow:
    defaults = dict(
        food="Apple", qty=1, unit="apple", kcal_total=95,
        protein_total=0.5, fat_total=0.3, carbs_total=25,
        source="cache_lookup",
        today_totals={"kcal": 95, "protein": 0.5, "fat": 0.3, "carbs": 25},
    )
    defaults.update(kw)
    return LoggedRow(**defaults)


def test_single_cache_lookup_row():
    result = OrchestratorResult(logged=[_row()])
    reply = format_reply(result)
    assert "logged: 1 apple" in reply.lower()
    assert "today:" in reply.lower()
    assert "95 kcal" in reply.lower()


def test_single_text_estimate_row_shows_macros_inline():
    result = OrchestratorResult(logged=[_row(
        food="Chia pudding", qty=1, unit="serving",
        kcal_total=300, protein_total=12, fat_total=20, carbs_total=8,
        source="text_estimate",
        today_totals={"kcal": 300, "protein": 12, "fat": 20, "carbs": 8},
    )])
    reply = format_reply(result)
    assert "logged: chia pudding" in reply.lower()
    assert "300 kcal" in reply and "12P" in reply


def test_saved_note_appears_on_saved_row():
    result = OrchestratorResult(logged=[_row(
        food="Chia pudding", source="text_estimate", kcal_total=300,
        protein_total=12, fat_total=20, carbs_total=8,
        save_status="saved",
    )])
    reply = format_reply(result)
    assert "saved to cache" in reply.lower()


def test_duplicate_note_appears_on_duplicate_save():
    result = OrchestratorResult(logged=[_row(save_status="duplicate")])
    reply = format_reply(result)
    assert "already in cache" in reply.lower()


def test_multi_entry_bulleted():
    result = OrchestratorResult(logged=[
        _row(food="Apple", kcal_total=95),
        _row(food="Coffee", qty=1, unit="cup",
             kcal_total=2, protein_total=0.3, fat_total=0, carbs_total=0.2),
    ])
    reply = format_reply(result)
    lines = reply.splitlines()
    assert lines[0].lower().startswith("logged:")
    assert any("· 1 apple" in l.lower() for l in lines)
    assert any("· 1 cup coffee" in l.lower() for l in lines)


def test_unknown_appended_when_present():
    result = OrchestratorResult(
        logged=[_row()],
        unknown=[UnknownEntry(name="obscure food", reason="not in cache")],
    )
    reply = format_reply(result)
    assert "couldn't resolve" in reply.lower()
    assert "obscure food" in reply.lower()


def test_empty_result_gives_placeholder():
    reply = format_reply(OrchestratorResult())
    assert reply == "(nothing logged)"
