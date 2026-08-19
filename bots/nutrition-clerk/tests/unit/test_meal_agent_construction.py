from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from google.adk.models.lite_llm import LiteLlm  # noqa: E402

from nutrition_clerk.agents import (  # noqa: E402
    build_meal_agent,
    build_polite_decline_agent,
    build_root_agent,
)


def _fake_model() -> LiteLlm:
    return LiteLlm(model="anthropic/claude-fake")


def test_meal_agent_construction_without_tools(tmp_path):
    agent = build_meal_agent(_fake_model(), toolsets=[], log_dir=tmp_path)
    assert agent.name == "meal_agent"
    assert agent.description
    assert agent.instruction
    # `now` is always attached — the LLM's own notion of the current date is
    # stale, so the agent must be able to observe wall-clock time.
    tool_names = {getattr(t, "name", None) or type(t).__name__ for t in agent.tools}
    assert "now" in tool_names, tool_names


def test_root_lists_meal_and_decline():
    model = _fake_model()
    meal = build_meal_agent(model, toolsets=[], log_dir=Path("/tmp"))
    polite = build_polite_decline_agent(model)
    root = build_root_agent(model, sub_agents=[meal, polite])
    # Router relies on the sub-agent names appearing verbatim in its instruction
    # so it can produce valid transfer_to_agent calls.
    assert "meal_agent" in root.instruction
    assert "polite_decline" in root.instruction
    assert {a.name for a in root.sub_agents} == {"meal_agent", "polite_decline"}


def test_meal_instruction_locks_source_enum():
    """Source is a semantic enum matching what's already in production MD files."""
    agent = build_meal_agent(_fake_model(), toolsets=[], log_dir=Path("/tmp"))
    # Production values: cache_lookup, text_estimate, photo_label, photo_estimate.
    # (No `llm_estimate` — LLM-guessed and user-typed both use text_estimate;
    # distinguished by confidence, not enum.)
    assert '"cache_lookup"' in agent.instruction
    assert '"text_estimate"' in agent.instruction
    assert "llm_estimate" not in agent.instruction, (
        "llm_estimate is not a production source value — align with Veda"
    )
    # M7 targets are referenced (declared but not implemented) so future readers
    # know the enum values reserved for photo shapes.
    assert "photo_label" in agent.instruction
    assert "photo_estimate" in agent.instruction
    # Must NOT stuff telegram msg_id into any tool arg.
    assert 'source="telegram:' not in agent.instruction
    assert "telegram:<N>" not in agent.instruction
    # But the SYSTEM_META header is still documented (metadata, not tool arg).
    assert "telegram_msg_id" in agent.instruction


def test_meal_instruction_carries_critical_rules_from_skill():
    """Guard the load-bearing rules imported from the production SKILL.md."""
    text = build_meal_agent(_fake_model(), toolsets=[], log_dir=Path("/tmp")).instruction

    # The 100g bug rule — production disaster protection.
    assert "100g" in text
    assert "100x too high" in text or "100× too high" in text or "100x" in text

    # Zero-kcal items still get logged.
    assert "zero-kcal" in text.lower() or "black coffee" in text.lower()

    # Intent classification categories from SKILL.md.
    assert "LOG_FOOD" in text
    assert "NOT_FOOD_TRACKING" in text

    # "My usual" recognised as a trigger.
    assert "my usual" in text.lower()

    # FASTING marker convention.
    assert "FASTING" in text or "fasting" in text.lower()

    # SHAPE A miss fallback still permits llm-guessed estimates (via text_estimate).
    assert "SHAPE A miss" in text
    assert "banana" in text.lower() or "olive oil" in text.lower()
    assert "branded" in text.lower() or "brand" in text.lower()

    # Multi-entry / conversational parsing acknowledged.
    assert "MULTIPLE entries" in text or "multiple entries" in text.lower()

    # LLM-guessed estimates must not be silently saved to personal cache.
    assert "DO NOT call add_personal_food" in text


def test_meal_instruction_covers_m4_shapes():
    """Guard against silent regressions of the M4 flow."""
    agent = build_meal_agent(_fake_model(), toolsets=[], log_dir=Path("/tmp"))
    text = agent.instruction
    # Shape classification is explicit.
    assert "SHAPE A" in text and "SHAPE B" in text
    # SHAPE B skips lookup_food and treats the entry as one serving.
    assert "SKIP lookup_food" in text
    # save-to-cache flow references add_personal_food and handles duplicates.
    assert "add_personal_food" in text
    assert "duplicate" in text.lower()
    # 6.3 (photo) is still declined at M4/M6; 6.4 is now supported.
    assert "photo" in text.lower()
    assert "not yet supported" in text


def test_meal_instruction_covers_m6_save_recent_flow():
    """Guard against silent regressions of the M6 SAVE_RECENT (shape 6.4) flow."""
    text = build_meal_agent(_fake_model(), toolsets=[], log_dir=Path("/tmp")).instruction
    # Intent classification category is present in the table.
    assert "SAVE_RECENT" in text
    # Flow step 6b is documented.
    assert "Step 6b" in text
    # recent_meals tool is referenced.
    assert "recent_meals" in text
    # SAVE_RECENT MUST NOT double-log — critical rule.
    assert "Do NOT call log_food" in text or "do not call log_food" in text.lower()
    # Empty-log branch: agent asks user to log first, not invent an entry.
    assert "log it first" in text.lower()
