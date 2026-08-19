"""Integration test — hits the real LLM. Skipped unless a provider key is set.

Runs the full ADK pipeline against Anthropic Haiku 4.5 by default.
- Enable: `export ANTHROPIC_API_KEY=...` then `uv run pytest tests/integration`.
- Skipped in CI-less local dev — no accidental spend.
"""
from __future__ import annotations

import os

import pytest

pytest.importorskip("google.adk")

from nutrition_clerk.agents import (  # noqa: E402
    AgentPipeline,
    build_polite_decline_agent,
    build_root_agent,
)
from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import ModelSettings  # noqa: E402
from nutrition_clerk.model import build_model  # noqa: E402


pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)


@pytest.mark.asyncio
async def test_off_domain_gets_declined_end_to_end():
    settings = ModelSettings()
    model = build_model(settings.profiles["cloud_haiku"])
    polite = build_polite_decline_agent(model)
    root = build_root_agent(model, sub_agents=[polite])
    pipeline = AgentPipeline(root_agent=root)

    reply = await pipeline.handle(
        InboundEvent(chat_id=1, msg_id=1, text="What's the weather in London?")
    )
    assert reply
    assert reply != "(no response)"
    # We deliberately don't assert wording — LLM output varies. The router
    # transferring to polite_decline and polite_decline producing text is
    # enough evidence the pipeline works end-to-end.
