from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from google.adk.models.lite_llm import LiteLlm  # noqa: E402

from nutrition_clerk.agents.router import ModelRouter, apply_model_recursively  # noqa: E402
from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import ModelProfile, ModelSettings, RoutingRule  # noqa: E402


def _make_settings(rules: list[RoutingRule]) -> ModelSettings:
    return ModelSettings(
        default="cloud_haiku",
        profiles={
            "cloud_haiku": ModelProfile(model="anthropic/claude-haiku-4-5"),
            "cloud_sonnet": ModelProfile(model="anthropic/claude-sonnet-5"),
            "local_gemma": ModelProfile(
                model="ollama_chat/gemma3n:e4b",
                api_base="http://localhost:11434",
            ),
        },
        routing=rules,
    )


def _ev(text: str = "", photos: list[Path] | None = None) -> InboundEvent:
    return InboundEvent(chat_id=1, msg_id=1, text=text, photos=photos or [])


def test_empty_rules_uses_default():
    router = ModelRouter(_make_settings([]))
    name, model = router.resolve(_ev(text="whatever"))
    assert name == "default"
    assert model.model == "anthropic/claude-haiku-4-5"


def test_photo_only_rule_matches_when_photo_present():
    router = ModelRouter(_make_settings([
        RoutingRule(name="any_photo", profile="cloud_sonnet", requires_photo=True),
    ]))
    name, model = router.resolve(_ev(text="", photos=[Path("/tmp/x.jpg")]))
    assert name == "any_photo"
    assert model.model == "anthropic/claude-sonnet-5"


def test_photo_only_rule_skipped_when_no_photo():
    router = ModelRouter(_make_settings([
        RoutingRule(name="any_photo", profile="cloud_sonnet", requires_photo=True),
    ]))
    name, model = router.resolve(_ev(text="1 apple"))
    assert name == "default"


def test_requires_photo_false_forbids_photo():
    router = ModelRouter(_make_settings([
        RoutingRule(name="text_only", profile="local_gemma", requires_photo=False),
    ]))
    # No photo -> matches
    name, _ = router.resolve(_ev(text="1 apple"))
    assert name == "text_only"
    # Has photo -> skipped, falls to default
    name, _ = router.resolve(_ev(text="1 apple", photos=[Path("/tmp/x.jpg")]))
    assert name == "default"


def test_pattern_matches_case_insensitively():
    router = ModelRouter(_make_settings([
        RoutingRule(
            name="eating_out",
            profile="cloud_sonnet",
            pattern=r"\b(restaurant|cafe)\b",
        ),
    ]))
    for text in ["Thai RESTAURANT dinner", "little cafe on the corner", "Cafe Nero"]:
        name, _ = router.resolve(_ev(text=text))
        assert name == "eating_out", text


def test_pattern_and_photo_both_required():
    router = ModelRouter(_make_settings([
        RoutingRule(
            name="eating_out_photo",
            profile="cloud_sonnet",
            pattern=r"\brestaurant\b",
            requires_photo=True,
        ),
    ]))
    # text-only "restaurant" -> skipped (needs photo)
    assert router.resolve(_ev(text="restaurant"))[0] == "default"
    # photo without keyword -> skipped
    assert router.resolve(_ev(text="", photos=[Path("/x.jpg")]))[0] == "default"
    # both -> matched
    assert router.resolve(_ev(text="Thai restaurant dinner", photos=[Path("/x.jpg")]))[0] == "eating_out_photo"


def test_first_match_wins():
    """Later rules must NOT override earlier matches."""
    router = ModelRouter(_make_settings([
        RoutingRule(name="specific", profile="cloud_sonnet", pattern=r"\bcurry\b"),
        RoutingRule(name="catchall", profile="local_gemma"),
    ]))
    assert router.resolve(_ev(text="Thai curry"))[0] == "specific"
    assert router.resolve(_ev(text="1 apple"))[0] == "catchall"


def test_unknown_default_profile_raises():
    settings = ModelSettings(
        default="does_not_exist",
        profiles={"cloud_haiku": ModelProfile(model="anthropic/claude-haiku-4-5")},
        routing=[],
    )
    with pytest.raises(KeyError, match="does_not_exist"):
        ModelRouter(settings)


def test_rule_referencing_unknown_profile_raises():
    settings = ModelSettings(
        default="cloud_haiku",
        profiles={"cloud_haiku": ModelProfile(model="anthropic/claude-haiku-4-5")},
        routing=[RoutingRule(name="bad", profile="phantom")],
    )
    with pytest.raises(KeyError, match="phantom"):
        ModelRouter(settings)


def test_apply_model_recursively_swaps_root_and_subagents():
    """The recursive swap must propagate through sub_agents."""
    from google.adk.agents import LlmAgent

    haiku = LiteLlm(model="anthropic/claude-haiku-4-5")
    sonnet = LiteLlm(model="anthropic/claude-sonnet-5")

    child = LlmAgent(name="child", model=haiku, instruction="child")
    parent = LlmAgent(name="parent", model=haiku, instruction="parent", sub_agents=[child])

    apply_model_recursively(parent, sonnet)
    assert parent.model.model == "anthropic/claude-sonnet-5"
    assert child.model.model == "anthropic/claude-sonnet-5"


def test_router_only_builds_needed_profiles():
    """Profiles not referenced by default or any rule should NOT be built.

    Guards against silently constructing a LiteLlm for `local_gemma` (Ollama
    might not be running) when the user hasn't opted in via a rule.
    """
    settings = _make_settings([
        RoutingRule(name="p", profile="cloud_sonnet", requires_photo=True),
    ])
    router = ModelRouter(settings)
    # cloud_haiku (default) + cloud_sonnet (rule) built; local_gemma should NOT be.
    assert set(router._models.keys()) == {"cloud_haiku", "cloud_sonnet"}
