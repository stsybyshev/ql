"""Deterministic natural-language datetime resolution.

Turns the extractor's free-text `datetime_hint` into the canonical
"DD-MM-YYYY HH:MM" that `log_food` expects. Pure Python — no LLM call.

Design: the extractor is told to pass the user's time phrasing through
verbatim rather than resolving it, because LLMs have no reliable notion of
"now". Resolution happens here where we can be exact and testable.

Supported (case-insensitive):
  - None / "" / "now" / "just now" / "just had"     -> now
  - "DD-MM-YYYY HH:MM"                              -> passthrough
  - "20 minutes ago", "2 hours ago", "an hour ago",
    "half an hour ago"                              -> offset from now
  - "this morning|afternoon|evening|..."            -> today at a period hour
  - "yesterday", "yesterday evening", "last night"  -> yesterday (+period)
  - "at 8pm", "8:30pm", "20:00", "8 am"             -> today at that clock time
  - "yesterday at 8pm"                              -> combined
Unrecognised phrasing falls back to now() and logs a warning so we can
extend the recogniser when real usage surfaces new patterns.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger("nutrition_clerk.workflow.datetime_resolver")

FMT = "%d-%m-%Y %H:%M"

_STRUCTURED = re.compile(r"^\d{2}-\d{2}-\d{4} \d{2}:\d{2}$")

_NOW_PHRASES = {
    "", "now", "just now", "just", "just had", "just ate",
    "right now", "moments ago", "a moment ago",
}

# Period name -> default hour. Used when the user names a meal/period but no
# clock time ("this morning", "yesterday evening").
_PERIOD_HOURS: dict[str, int] = {
    "breakfast": 8,
    "morning": 9,
    "brunch": 11,
    "midday": 12,
    "noon": 12,
    "lunch": 13,
    "lunchtime": 13,
    "afternoon": 15,
    "tea": 16,
    "evening": 19,
    "dinner": 19,
    "supper": 20,
    "tonight": 20,
    "night": 21,
}

# "2 hours ago", "20 min ago", "an hour ago", "half an hour ago"
_REL_NUMERIC = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m)\b\s*ago", re.IGNORECASE
)
_REL_AN_HOUR = re.compile(r"\b(an?|one)\s+(hour|hr)\b\s*ago", re.IGNORECASE)
_REL_HALF_HOUR = re.compile(r"\bhalf\s+an?\s+hour\b\s*ago", re.IGNORECASE)

# "8pm", "8:30 pm", "20:00", "8 am"
_CLOCK = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", re.IGNORECASE
)

_HOUR_UNITS = {"hour", "hours", "hr", "hrs", "h"}


def resolve(hint: str | None, now: datetime | None = None) -> str:
    """Resolve a free-text time hint to "DD-MM-YYYY HH:MM"."""
    now = now or datetime.now()

    if hint is None:
        return now.strftime(FMT)

    raw = hint.strip()
    if _STRUCTURED.match(raw):
        return raw

    h = raw.lower()
    if h in _NOW_PHRASES:
        return now.strftime(FMT)

    # ---- relative offsets ("2 hours ago") ----
    offset = _parse_relative(h)
    if offset is not None:
        return (now - offset).strftime(FMT)

    # ---- day anchor ----
    # Check the longer phrase first — "day before yesterday" contains
    # "yesterday" as a substring.
    day_delta = 0
    if "day before yesterday" in h:
        day_delta = -2
    elif "yesterday" in h or "last night" in h:
        day_delta = -1
    target_date = (now + timedelta(days=day_delta)).date()

    # ---- explicit clock time ("8pm", "20:00") ----
    clock = _parse_clock(h)
    if clock is not None:
        hour, minute = clock
        return datetime.combine(
            target_date, datetime.min.time()
        ).replace(hour=hour, minute=minute).strftime(FMT)

    # ---- named period ("this morning", "yesterday evening") ----
    period_hour = _parse_period(h)
    if period_hour is not None:
        # "last night" reads as ~21:00 the previous day; day_delta already set.
        return datetime.combine(
            target_date, datetime.min.time()
        ).replace(hour=period_hour, minute=0).strftime(FMT)

    # ---- bare day anchor with no time ("yesterday") ----
    if day_delta != 0:
        # No period given — use the current clock time on that day. Better
        # than picking an arbitrary hour: preserves "roughly this time".
        return datetime.combine(
            target_date, datetime.min.time()
        ).replace(hour=now.hour, minute=now.minute).strftime(FMT)

    # ---- "today" with no other signal ----
    if "today" in h or h.startswith("this "):
        return now.strftime(FMT)

    log.warning("datetime hint %r not recognised; falling back to now()", hint)
    return now.strftime(FMT)


def _parse_relative(h: str) -> timedelta | None:
    if _REL_HALF_HOUR.search(h):
        return timedelta(minutes=30)
    if _REL_AN_HOUR.search(h):
        return timedelta(hours=1)
    m = _REL_NUMERIC.search(h)
    if not m:
        return None
    amount = float(m.group(1))
    unit = m.group(2).lower()
    if unit in _HOUR_UNITS:
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def _parse_clock(h: str) -> tuple[int, int] | None:
    m = _CLOCK.search(h)
    if not m:
        return None
    if m.group(3):  # am/pm branch
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridiem = m.group(3).lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    else:  # 24h branch
        hour = int(m.group(4))
        minute = int(m.group(5))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _parse_period(h: str) -> int | None:
    # Longest key first so "lunchtime" wins over "lunch", "tonight" over "night".
    for period in sorted(_PERIOD_HOURS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(period)}\b", h):
            return _PERIOD_HOURS[period]
    return None
