from __future__ import annotations

from pathlib import Path
from typing import Sequence

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset

from nutrition_clerk.agents.meal.prompts import MEAL_AGENT_INSTRUCTION
from nutrition_clerk.tools import (
    build_clarify_tool,
    build_now_tool,
    build_rank_matches_tool,
    build_recent_meals_tool,
)


def build_meal_agent(
    model: BaseLlm,
    toolsets: Sequence[BaseToolset],
    *,
    log_dir: Path,
    extra_tools: Sequence[BaseTool] | None = None,
) -> LlmAgent:
    """Meal-logging specialist. `toolsets` is typically [McpToolset(food-tracker)].

    Always includes:
      - `now()` — deterministic clock (LLM's date knowledge is stale)
      - `rank_matches(query, candidates)` — rapidfuzz scoring for disambiguation
      - `clarify(question)` — records a pending clarification on session state
      - `recent_meals(n)` — reads the last N logged entries for shape 6.4
        (save-a-recently-logged-meal-to-personal-cache)

    Extra tools (M7 `read_label_photo`) can be added via `extra_tools`.
    """
    tools: list = [
        build_now_tool(),
        build_rank_matches_tool(),
        build_clarify_tool(),
        build_recent_meals_tool(log_dir=log_dir),
    ]
    if extra_tools:
        tools.extend(extra_tools)
    tools.extend(toolsets)
    return LlmAgent(
        name="meal_agent",
        model=model,
        description=(
            "Logs meals to the daily food-tracker file. Handles plain-text "
            "entries like '1 apple' or 'cashews 50g' by looking them up in "
            "the personal/popular food cache and appending a row."
        ),
        instruction=MEAL_AGENT_INSTRUCTION,
        tools=tools,
    )
