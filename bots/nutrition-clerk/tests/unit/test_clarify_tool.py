from __future__ import annotations

from types import SimpleNamespace

from nutrition_clerk.tools import PENDING_CLARIFICATION_KEY
from nutrition_clerk.tools.clarify import build_clarify_tool


class _StateDict(dict):
    """A minimal stand-in for ADK ToolContext.state (dict-like)."""


def test_clarify_writes_question_to_state():
    tool = build_clarify_tool()
    state = _StateDict()
    ctx = SimpleNamespace(state=state)

    result = tool.func(question="Did you mean chia pudding or chia seeds?", tool_context=ctx)

    assert result == {"status": "pending"}
    assert state[PENDING_CLARIFICATION_KEY] == "Did you mean chia pudding or chia seeds?"


def test_clarify_overwrites_prior_pending():
    tool = build_clarify_tool()
    state = _StateDict({PENDING_CLARIFICATION_KEY: "stale question"})
    ctx = SimpleNamespace(state=state)

    tool.func(question="new question", tool_context=ctx)

    assert state[PENDING_CLARIFICATION_KEY] == "new question"
