from __future__ import annotations

import pytest

pytest.importorskip("google.adk")

from google.adk.models.lite_llm import LiteLlm  # noqa: E402

from nutrition_clerk.agents import build_polite_decline_agent, build_root_agent  # noqa: E402
from nutrition_clerk.agents.pipeline import AgentPipeline  # noqa: E402
from nutrition_clerk.config import ModelSettings  # noqa: E402
from nutrition_clerk.model import build_model  # noqa: E402


def _fake_model() -> LiteLlm:
    """LiteLlm construction is lazy — no API call happens until generate is invoked."""
    return LiteLlm(model="anthropic/claude-fake")


def test_polite_decline_agent_shape():
    agent = build_polite_decline_agent(_fake_model())
    assert agent.name == "polite_decline"
    assert agent.description
    assert agent.instruction
    assert not agent.sub_agents


def test_root_agent_lists_sub_agents_in_instruction():
    polite = build_polite_decline_agent(_fake_model())
    root = build_root_agent(_fake_model(), sub_agents=[polite])
    assert root.name == "root"
    assert len(root.sub_agents) == 1
    # polite_decline should be referenced by name in root's instruction so the
    # router LLM can produce a valid transfer_to_agent call.
    assert "polite_decline" in root.instruction


def test_pipeline_builds_without_api_calls():
    polite = build_polite_decline_agent(_fake_model())
    root = build_root_agent(_fake_model(), sub_agents=[polite])
    pipeline = AgentPipeline(root_agent=root)
    assert pipeline is not None  # construction alone should not raise


def test_build_model_from_default_profile():
    settings = ModelSettings()
    llm = build_model(settings.active_profile())
    assert isinstance(llm, LiteLlm)
    assert llm.model == "anthropic/claude-haiku-4-5"
