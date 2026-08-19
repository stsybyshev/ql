"""Deterministic clock exposed to the meal agent.

Without this, LLM-invented timestamps land months in the past — the model has
no reliable notion of the current date. Providing it as a tool call makes the
current time observable and deterministic (and easy to override in tests).
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from google.adk.tools import FunctionTool


def _default_clock() -> datetime:
    return datetime.now()


def build_now_tool(clock: Callable[[], datetime] = _default_clock) -> FunctionTool:
    """Return a FunctionTool `now()` returning current time as DD-MM-YYYY HH:MM.

    The format matches the food-tracker MCP server's expected `datetime`
    argument shape (see food_cache.py) so the LLM can pass the result straight
    into `log_food` without reformatting.
    """

    def now() -> str:
        """Return the current wall-clock time in "DD-MM-YYYY HH:MM" format."""
        return clock().strftime("%d-%m-%Y %H:%M")

    return FunctionTool(func=now)
