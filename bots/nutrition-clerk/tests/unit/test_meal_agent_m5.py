from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from google.adk.models.lite_llm import LiteLlm  # noqa: E402

from nutrition_clerk.agents import build_meal_agent  # noqa: E402


def _fake_model() -> LiteLlm:
    return LiteLlm(model="anthropic/claude-fake")


def test_meal_agent_includes_m5_tools():
    agent = build_meal_agent(_fake_model(), toolsets=[], log_dir=Path("/tmp"))
    names = {getattr(t, "name", None) or type(t).__name__ for t in agent.tools}
    # M3 baseline
    assert "now" in names
    # M5 additions
    assert "rank_matches" in names, names
    assert "clarify" in names, names


def test_meal_prompt_covers_m5_flow():
    agent = build_meal_agent(_fake_model(), toolsets=[], log_dir=Path("/tmp"))
    text = agent.instruction
    # New tools referenced by name in the instructions.
    assert "rank_matches" in text
    assert "clarify" in text
    # Resume header format is stable.
    assert "[PREV_CLARIFY:" in text
    # Explicit guard against re-asking on resume.
    assert "DO NOT re-ask" in text
    # The instruction still declines shapes 6.3/6.4.
    assert "not yet supported" in text
