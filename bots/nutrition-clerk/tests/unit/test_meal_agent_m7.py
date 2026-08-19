from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from google.adk.models.lite_llm import LiteLlm  # noqa: E402

from nutrition_clerk.agents import build_meal_agent  # noqa: E402


def _fake_model() -> LiteLlm:
    return LiteLlm(model="anthropic/claude-fake")


def test_meal_prompt_covers_m7_shape_c():
    text = build_meal_agent(_fake_model(), toolsets=[], log_dir=Path("/tmp")).instruction
    # New shape declared in the taxonomy.
    assert "SHAPE C" in text
    # Step 5c documents the flow.
    assert "Step 5c" in text
    # Photo source is nailed to the production enum value.
    assert '"photo_label"' in text
    # 100g rule still governs weight-based logging (label case).
    assert "100g" in text
    # If quantity is unclear, MUST ask via clarify — never guess grams.
    assert "clarify" in text
    # Multi-photo matching is at least acknowledged.
    assert "Multi-photo" in text or "multi-photo" in text.lower()


def test_meal_prompt_covers_shape_d_meal_photo():
    text = build_meal_agent(_fake_model(), toolsets=[], log_dir=Path("/tmp")).instruction
    # SHAPE D declared and distinguished from C.
    assert "SHAPE D" in text
    # Flow step present.
    assert "Step 5d" in text
    # photo_estimate is the source for meal-photo estimates.
    assert '"photo_estimate"' in text
    # Confidence guidance for meal-photo estimates.
    assert "0.3-0.5" in text or "0.3 to 0.5" in text.lower()
    # C-vs-D disambiguation rule: default to D when unclear.
    assert "default to d" in text.lower()
    # Per-dish clarify anti-pattern warning — no 4-way interrogations.
    assert "Do NOT call clarify() per dish" in text or "do not call clarify() per dish" in text.lower()
    # Meal-photo WITHOUT text remains ambiguous — must ask for a dish list.
    assert "name the dishes" in text.lower()
