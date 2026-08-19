from __future__ import annotations

from collections.abc import Sequence

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.models.base_llm import BaseLlm

ROOT_INSTRUCTION_TEMPLATE = """\
You are the router for a personal clerical bot. You do NOT respond to the
user directly; you always transfer control to a specialist sub-agent.

Available sub-agents:
{sub_agent_descriptions}

Routing rules:
- If the message is about food, meals, macros, calories, eating, or logging
  something the user consumed -> transfer to `meal_agent` (if listed above).
- Anything else -> transfer to `polite_decline`.
- If in doubt -> transfer to `polite_decline`.

Never generate a natural-language reply yourself. Your only output is a
transfer_to_agent tool call.
"""


def _describe_sub_agents(agents: Sequence[BaseAgent]) -> str:
    lines = []
    for agent in agents:
        desc = (agent.description or "").strip() or "(no description)"
        lines.append(f"- `{agent.name}`: {desc}")
    return "\n".join(lines)


def build_root_agent(model: BaseLlm, sub_agents: Sequence[BaseAgent]) -> LlmAgent:
    return LlmAgent(
        name="root",
        model=model,
        description="Router: classifies inbound messages and transfers to a specialist.",
        instruction=ROOT_INSTRUCTION_TEMPLATE.format(
            sub_agent_descriptions=_describe_sub_agents(sub_agents),
        ),
        sub_agents=list(sub_agents),
    )
