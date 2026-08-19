from __future__ import annotations

import logging
import os

from google.adk.models.lite_llm import LiteLlm

from nutrition_clerk.config import ModelProfile

log = logging.getLogger("nutrition_clerk.model")


def build_model(profile: ModelProfile) -> LiteLlm:
    """Turn a config ModelProfile into a ready-to-use LiteLlm instance.

    Note: LiteLLM discovers provider keys via well-known env vars (e.g.
    ANTHROPIC_API_KEY). The `api_key_env` field is informational — we just warn
    if it's referenced but not set.
    """
    if profile.api_key_env and not os.environ.get(profile.api_key_env):
        log.warning(
            "profile references env %s but it is not set; LiteLLM will fail on first call",
            profile.api_key_env,
        )
    kwargs: dict[str, object] = {}
    if profile.api_base:
        kwargs["api_base"] = profile.api_base
    return LiteLlm(model=profile.model, **kwargs)
