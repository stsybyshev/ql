"""Per-turn model router.

Given an inbound event, decide which profile the meal agent should use for
this turn. Rules are ordered and first-match-wins; if nothing matches, the
default profile is used.

The router owns the built `BaseLlm` instances (LiteLLM constructs are lazy —
no API call happens at construction time), so we build each unique profile's
model once at startup rather than per turn.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from google.adk.models.base_llm import BaseLlm

from nutrition_clerk.channels import InboundEvent
from nutrition_clerk.config import ModelSettings, RoutingRule
from nutrition_clerk.model import build_model

log = logging.getLogger("nutrition_clerk.agents.router")


@dataclass(frozen=True)
class _CompiledRule:
    rule: RoutingRule
    pattern: re.Pattern | None


class ModelRouter:
    def __init__(self, settings: ModelSettings) -> None:
        self._default_profile = settings.default
        if self._default_profile not in settings.profiles:
            raise KeyError(
                f"default profile {self._default_profile!r} not defined; "
                f"available: {list(settings.profiles)}"
            )
        # Validate rules point at real profiles before we start swallowing turns.
        for r in settings.routing:
            if r.profile not in settings.profiles:
                raise KeyError(
                    f"routing rule {r.name!r} points at unknown profile "
                    f"{r.profile!r}; available: {list(settings.profiles)}"
                )
        self._rules: list[_CompiledRule] = [
            _CompiledRule(
                rule=r,
                pattern=re.compile(r.pattern, re.IGNORECASE) if r.pattern else None,
            )
            for r in settings.routing
        ]
        # Only build models for profiles actually referenced (default + any rule).
        needed = {self._default_profile} | {r.profile for r in settings.routing}
        self._models: dict[str, BaseLlm] = {
            name: build_model(settings.profiles[name]) for name in needed
        }

    def resolve(self, event: InboundEvent) -> tuple[str, BaseLlm]:
        """Return (route_name, model) for this turn.

        route_name is the matched rule's `name`, or "default" if no rule matched.
        """
        text = event.text or ""
        has_photo = bool(event.photos)
        for compiled in self._rules:
            r = compiled.rule
            if r.requires_photo is True and not has_photo:
                continue
            if r.requires_photo is False and has_photo:
                continue
            if compiled.pattern is not None and not compiled.pattern.search(text):
                continue
            return r.name, self._models[r.profile]
        return "default", self._models[self._default_profile]


def apply_model_recursively(agent, model: BaseLlm) -> None:
    """Swap `model` on the given agent and all descendants that have one.

    ADK's LlmAgent stores `model` as a mutable pydantic field, and the Runner
    reads it at invocation time — so an in-place swap propagates immediately.
    Sub-agent walking is defensive: some ADK agents don't expose sub_agents
    or expose it as None.
    """
    if hasattr(agent, "model") and agent.model is not None:
        agent.model = model
    for sub in (getattr(agent, "sub_agents", None) or []):
        apply_model_recursively(sub, model)
