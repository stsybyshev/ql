from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nutrition_clerk.agents.pipeline import (
    PREV_CLARIFY_TEMPLATE,
    AgentPipeline,
    _wrap_with_meta,
)
from nutrition_clerk.channels.base import InboundEvent
from nutrition_clerk.tools import PENDING_CLARIFICATION_KEY


def test_prev_clarify_template_carries_question_and_message():
    """Guard the header shape the meal agent's instruction parses."""
    rendered = PREV_CLARIFY_TEMPLATE.format(question="Chia pudding or seeds?", text="seeds")
    assert "[PREV_CLARIFY: Chia pudding or seeds?]" in rendered
    assert rendered.endswith("seeds")
    assert "Resume the meal-log flow" in rendered


@pytest.mark.asyncio
async def test_read_pending_returns_none_when_no_session():
    """Fresh chats have no session yet — must not raise."""
    pipeline = AgentPipeline.__new__(AgentPipeline)
    pipeline._app_name = "nutrition-clerk"
    svc = AsyncMock()
    svc.get_session.side_effect = Exception("session missing")
    pipeline._session_service = svc

    got = await pipeline._read_pending_clarification("1", "1")
    assert got is None


@pytest.mark.asyncio
async def test_read_pending_returns_none_when_state_empty():
    pipeline = AgentPipeline.__new__(AgentPipeline)
    pipeline._app_name = "nutrition-clerk"
    svc = AsyncMock()
    svc.get_session.return_value = type("S", (), {"state": {}})()
    pipeline._session_service = svc

    got = await pipeline._read_pending_clarification("1", "1")
    assert got is None


@pytest.mark.asyncio
async def test_read_pending_returns_question_when_set():
    pipeline = AgentPipeline.__new__(AgentPipeline)
    pipeline._app_name = "nutrition-clerk"
    svc = AsyncMock()
    svc.get_session.return_value = type(
        "S", (), {"state": {PENDING_CLARIFICATION_KEY: "Chia pudding or seeds?"}}
    )()
    pipeline._session_service = svc

    got = await pipeline._read_pending_clarification("1", "1")
    assert got == "Chia pudding or seeds?"


def test_wrap_with_meta_precedes_body():
    event = InboundEvent(chat_id=1, msg_id=42, text="1 apple")
    wrapped = _wrap_with_meta(event)
    assert wrapped.startswith("[SYSTEM_META:")
    assert wrapped.endswith("1 apple")
