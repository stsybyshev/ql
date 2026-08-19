"""Extractor — one LLM call, structured output, no tools, no history.

Uses LiteLlm.generate_content_async directly. Skips the LlmAgent tool-use
loop entirely — the model sees the instruction + user message and returns a
JSON blob matching ExtractedMessage. Orchestrator + Python do everything else.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from nutrition_clerk.workflow import trace
from nutrition_clerk.workflow.prompts import EXTRACTOR_INSTRUCTION
from nutrition_clerk.workflow.schemas import ExtractedMessage

log = logging.getLogger("nutrition_clerk.workflow.extractor")


async def extract(model: BaseLlm, user_text: str) -> ExtractedMessage:
    """Run one extractor LLM call. Returns the parsed ExtractedMessage.

    Raises on unparseable output — the caller (orchestrator) is expected to
    let the exception bubble; main's handler-level try/except will keep the
    dedup semantics intact (Telegram will redeliver).
    """
    # Inline the instruction into the user message (same reason as the vision
    # enricher: LiteLLM's Ollama path silently drops `system_instruction`).
    # Anthropic works either way. Wraps the raw user text so the model still
    # sees a clear boundary.
    prompt = (
        EXTRACTOR_INSTRUCTION
        + "\n\n--- User message begins ---\n"
        + (user_text or "(empty message)")
        + "\n--- User message ends ---"
    )
    request = LlmRequest(
        contents=[
            types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            # NOTE: no `response_schema=` — LiteLLM doesn't forward Google-style
            # schemas to Ollama. Structure is enforced by describing the schema
            # in EXTRACTOR_INSTRUCTION and by `_parse` handling code fences +
            # "thinking" prose (Gemma pattern).
            # NOTE: no `temperature=` — Sonnet 5 rejects temperature != 1;
            # each provider picks its own default which is fine for our
            # deterministic-parsing use case.
        ),
    )

    final_text = ""
    total_in = 0
    total_out = 0
    _t0 = time.perf_counter()
    async for resp in model.generate_content_async(request, stream=False):
        usage = getattr(resp, "usage_metadata", None)
        if usage is not None:
            total_in += getattr(usage, "prompt_token_count", 0) or 0
            total_out += getattr(usage, "candidates_token_count", 0) or 0
        if resp.content:
            for part in resp.content.parts or []:
                if part.text:
                    final_text += part.text

    log.info(
        "extractor: in=%d out=%d text_len=%d",
        total_in,
        total_out,
        len(final_text),
    )
    trace.record(
        "extractor",
        ms=round((time.perf_counter() - _t0) * 1000, 1),
        model=getattr(model, "model", str(model)),
        tokens={"in": total_in, "out": total_out},
        prompt=prompt,
        response=final_text,
    )
    return _parse(final_text)


import re as _re

_JSON_FENCE = _re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", _re.DOTALL)
_JSON_OBJECT = _re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", _re.DOTALL)


def _parse(text: str) -> ExtractedMessage:
    """Extract a single JSON object from arbitrary model output.

    Robust to: plain JSON (Anthropic), code-fenced JSON, and "thinking" prose
    surrounding a JSON block (Gemma/Ollama pattern).
    """
    text = text.strip()

    # 1) Direct parse (Anthropic returns clean JSON when response_mime_type=json)
    try:
        return ExtractedMessage.model_validate_json(text)
    except Exception:
        pass

    # 2) Extract from ```json ... ``` fence
    fence = _JSON_FENCE.search(text)
    if fence:
        try:
            return ExtractedMessage.model_validate_json(fence.group(1))
        except Exception:
            pass

    # 3) Grab the largest balanced-looking {...} in the text (skips prose)
    matches = _JSON_OBJECT.findall(text)
    if matches:
        largest = max(matches, key=len)
        return ExtractedMessage.model_validate_json(largest)

    raise ValueError(f"no JSON object found in extractor output: {text[:300]!r}")
