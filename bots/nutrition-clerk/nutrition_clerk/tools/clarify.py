"""Clarify tool: records that the agent is waiting on a user answer.

The mechanic:
- Agent calls `clarify(question=...)` — the tool writes `pending_clarification`
  to session state (keyed on the current chat), then returns.
- Agent produces the question text as its final response for the current turn.
- On the NEXT inbound message from the same chat, AgentPipeline.handle reads
  `pending_clarification`, prepends a `[PREV_CLARIFY: ...]` header to the
  message, and clears the state key. The instruction tells the agent how to
  interpret that header.

Persistence: `InMemorySessionService` only — state lives for the bot's lifetime.
A restart drops any in-flight clarification (see M5 discussion — acceptable).
"""
from __future__ import annotations

from google.adk.tools import FunctionTool, ToolContext

PENDING_CLARIFICATION_KEY = "pending_clarification"


def build_clarify_tool() -> FunctionTool:
    def clarify(question: str, tool_context: ToolContext) -> dict:
        """Record that the agent is waiting on a user reply to a specific question.

        Args:
            question: The exact question that will be surfaced to the user. It
                is stored so the next turn can inject it back as context.

        Returns:
            {"status": "pending"} — the agent should then produce the question
            as its final natural-language response for this turn.
        """
        tool_context.state[PENDING_CLARIFICATION_KEY] = question
        return {"status": "pending"}

    return FunctionTool(func=clarify)
