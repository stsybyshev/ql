"""Unit tests for workflow.datetime_resolver — deterministic, no LLM."""
from __future__ import annotations

from datetime import datetime

import pytest

from nutrition_clerk.workflow.datetime_resolver import resolve

# Fixed "now" so every assertion is exact: Sunday 16 Aug 2026, 14:30.
NOW = datetime(2026, 8, 16, 14, 30)


@pytest.mark.parametrize("hint", [None, "", "now", "just now", "just had", "right now"])
def test_now_phrases(hint):
    assert resolve(hint, now=NOW) == "16-08-2026 14:30"


def test_structured_passthrough():
    assert resolve("15-03-2026 08:30", now=NOW) == "15-03-2026 08:30"
    # Whitespace tolerated.
    assert resolve("  15-03-2026 08:30  ", now=NOW) == "15-03-2026 08:30"


@pytest.mark.parametrize("hint,expected", [
    ("2 hours ago",       "16-08-2026 12:30"),
    ("1 hour ago",        "16-08-2026 13:30"),
    ("an hour ago",       "16-08-2026 13:30"),
    ("half an hour ago",  "16-08-2026 14:00"),
    ("20 minutes ago",    "16-08-2026 14:10"),
    ("45 min ago",        "16-08-2026 13:45"),
    ("3 hrs ago",         "16-08-2026 11:30"),
])
def test_relative_offsets(hint, expected):
    assert resolve(hint, now=NOW) == expected


@pytest.mark.parametrize("hint,expected", [
    ("this morning",   "16-08-2026 09:00"),
    ("this afternoon", "16-08-2026 15:00"),
    ("this evening",   "16-08-2026 19:00"),
    ("at breakfast",   "16-08-2026 08:00"),
    ("for lunch",      "16-08-2026 13:00"),
    ("at dinner",      "16-08-2026 19:00"),
])
def test_today_periods(hint, expected):
    assert resolve(hint, now=NOW) == expected


@pytest.mark.parametrize("hint,expected", [
    ("yesterday morning",     "15-08-2026 09:00"),
    ("yesterday evening",     "15-08-2026 19:00"),
    ("yesterday for dinner",  "15-08-2026 19:00"),
    ("last night",            "15-08-2026 21:00"),
])
def test_yesterday_with_period(hint, expected):
    assert resolve(hint, now=NOW) == expected


def test_bare_yesterday_keeps_current_clock_time():
    """No period given -> same time of day, previous date."""
    assert resolve("yesterday", now=NOW) == "15-08-2026 14:30"


@pytest.mark.parametrize("hint,expected", [
    ("8pm",        "16-08-2026 20:00"),
    ("8 pm",       "16-08-2026 20:00"),
    ("8:30pm",     "16-08-2026 20:30"),
    ("at 7am",     "16-08-2026 07:00"),
    ("20:00",      "16-08-2026 20:00"),
    ("07:45",      "16-08-2026 07:45"),
    ("12am",       "16-08-2026 00:00"),
    ("12pm",       "16-08-2026 12:00"),
])
def test_clock_times_today(hint, expected):
    assert resolve(hint, now=NOW) == expected


@pytest.mark.parametrize("hint,expected", [
    ("yesterday at 8pm",   "15-08-2026 20:00"),
    ("yesterday 19:30",    "15-08-2026 19:30"),
])
def test_yesterday_with_clock(hint, expected):
    assert resolve(hint, now=NOW) == expected


def test_clock_beats_period_when_both_present():
    """'yesterday evening at 8:15pm' -> the explicit clock wins over 19:00."""
    assert resolve("yesterday evening at 8:15pm", now=NOW) == "15-08-2026 20:15"


def test_day_before_yesterday():
    assert resolve("day before yesterday", now=NOW) == "14-08-2026 14:30"


def test_unrecognised_falls_back_to_now(caplog):
    assert resolve("sometime around the heat death of the universe", now=NOW) == "16-08-2026 14:30"
    assert any("not recognised" in r.getMessage() for r in caplog.records)


def test_output_format_is_always_canonical():
    """Every branch must emit DD-MM-YYYY HH:MM — log_food depends on it."""
    for hint in [None, "now", "this morning", "yesterday", "2 hours ago",
                 "8pm", "last night", "nonsense"]:
        out = resolve(hint, now=NOW)
        assert len(out) == 16, f"{hint!r} -> {out!r}"
        assert out[2] == "-" and out[5] == "-" and out[10] == " " and out[13] == ":"
