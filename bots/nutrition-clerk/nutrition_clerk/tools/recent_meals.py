"""`recent_meals` tool — return the last N logged entries from the monthly MD.

Powers shape 6.4 ("save the pomegranate juice I had"). The tool needs to
return full per-unit values so the LLM can construct a valid
`add_personal_food` call — MCP's `get_todays_totals` only returns totals,
not per-unit breakdown, so we bypass it and parse the MD directly.

We reuse `food_cache._parse_log_rows` via a sys.path shim rather than
copying the parser locally. Rationale: the parser is 30 lines of format-
specific regex; duplicating would drift, and food-tracker's MCP server is
the single source of truth for the log schema. The private-underscore
prefix is a Python convention warning ("internal"), not a hard boundary —
we're in the same repo, we own both sides, and any format change will be
committed together.

The tool crosses the current-month boundary transparently: if the current
month's file doesn't have enough entries, it reads the prior month too.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from google.adk.tools import FunctionTool

log = logging.getLogger("nutrition_clerk.tools.recent_meals")

_FOOD_TRACKER_DIR = Path(
    "/home/stan/dev/ql/skills/nutrition-tracker/mcp-server/food-tracker"
).resolve()


def _import_parser():
    """Import the parser lazily so this module still loads if the food-tracker
    directory is missing at process start (tests, docs builds)."""
    if str(_FOOD_TRACKER_DIR) not in sys.path:
        sys.path.insert(0, str(_FOOD_TRACKER_DIR))
    from food_cache import _parse_log_rows  # type: ignore

    return _parse_log_rows


def _monthly_file(log_dir: Path, when: date) -> Path:
    return log_dir / f"{when.year:04d}-{when.month:02d}.md"


def _read_month(log_dir: Path, when: date) -> list[dict]:
    path = _monthly_file(log_dir, when)
    if not path.exists():
        return []
    parser = _import_parser()
    return parser(path.read_text())


def _first_of_previous_month(today: date) -> date:
    first_of_this_month = today.replace(day=1)
    return first_of_this_month - timedelta(days=1)


def build_recent_meals_tool(log_dir: Path) -> FunctionTool:
    def recent_meals(n: int = 10) -> dict:
        """Return the N most recent logged food entries, newest last.

        Fields per entry match what `log_food` accepts: datetime, food, qty,
        unit, kcal_per_unit, protein_per_unit, fat_per_unit, carbs_per_unit,
        plus source/confidence for context. Enough to identify the entry AND
        to construct an `add_personal_food` call from it.

        Args:
            n: how many recent entries to return (default 10; hard-capped at 50).

        Returns:
            {"entries": [<entry>, ...]} — chronological (oldest first). Empty
            list if the log directory has no entries yet.
        """
        n = max(1, min(int(n), 50))
        entries: list[dict] = []
        today = date.today()
        entries.extend(_read_month(log_dir, today))
        if len(entries) < n:
            prev = _first_of_previous_month(today)
            entries = _read_month(log_dir, prev) + entries
        return {"entries": entries[-n:]}

    return FunctionTool(func=recent_meals)
