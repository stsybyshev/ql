"""Knowledge enricher — one LLM call per cache-missed food (N6).

Runs only after BOTH the MCP substring lookup and the in-process fuzzy
fallback come back empty. Asks the model for typical macros from world
knowledge, with an explicit refusal path for branded / regional / homemade
foods whose macros vary too much to guess.

Same primitive as the vision enricher: LiteLlm.generate_content_async, no
agent, no session, no tool loop.

IMPORTANT invariant: knowledge-estimated entries are never promoted into
personal-foods (`save_to_cache` is ignored for them) — an LLM guess must not
silently become a "verified" cache entry the user trusts later.
"""
from __future__ import annotations

import json
import logging
import time
import re

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from nutrition_clerk.workflow import trace
from nutrition_clerk.workflow.schemas import KnowledgeExtract

log = logging.getLogger("nutrition_clerk.workflow.enrichers.knowledge")


KNOWLEDGE_INSTRUCTION = """\
You estimate nutrition macros for a single named food from general world
knowledge. Return ONLY a single JSON object with EXACTLY these keys:

    {
      "refused": bool,
      "refusal_reason": string or null,
      "unit": string,
      "kcal_per_unit": number,
      "protein_per_unit": number,
      "fat_per_unit": number,
      "carbs_per_unit": number,
      "confidence": number,
      "note": string or null
    }

No prose, no explanation, no code fences — just the JSON object.

Decide first: CAN you estimate this within roughly 15%?

ESTIMATE (refused=false) for common, standardised foods whose macros are
widely documented:
  fruits and vegetables, plain dairy, eggs, whole grains, plain rice/pasta,
  common nuts and seeds, standard cooking oils, plain meats and fish,
  everyday drinks (coffee, tea, juice, beer), common staples (bread, honey).

REFUSE (refused=true) for anything whose macros genuinely vary a lot:
  branded/packaged products ("Trader Joe's Kimchi Bowl", "Gu Chocolate
  Melting Middle"), restaurant or takeaway dishes, homemade/family recipes
  ("grandma's meatloaf"), meal-kit boxes (HelloFresh), or names you don't
  actually recognise. Set a short `refusal_reason` — the user will be asked
  to send macros or a label photo instead.

When estimating:
- `unit`: use "100g" for foods normally weighed (nuts, cheese, grains, meat,
  oils) and give per-100g values. Otherwise use a natural unit the user
  would say: "serving", "cup", "slice", "egg", "banana", "tbsp".
- `confidence`: 0.4 to 0.7. Upper end for standardised whole foods with an
  obvious portion; lower end when preparation or portion is ambiguous.
- `note`: mention the assumption if you made one ("medium banana ~118g",
  "assumes cooked weight").
- Never return zero calories for a food that has calories. Zero is only
  correct for things like black coffee, water, or plain tea.
"""


async def estimate_macros(model: BaseLlm, food_name: str) -> KnowledgeExtract:
    """One LLM call. Returns a KnowledgeExtract (possibly refused=True)."""
    prompt = f"{KNOWLEDGE_INSTRUCTION}\n\nFood to estimate: {food_name!r}"
    request = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            # No response_schema / temperature — see extractor.py for why
            # (Ollama compat + Sonnet 5 rejects temperature != 1).
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
        "knowledge: in=%d out=%d food=%r", total_in, total_out, food_name
    )
    trace.record(
        "knowledge",
        ms=round((time.perf_counter() - _t0) * 1000, 1),
        model=getattr(model, "model", str(model)),
        food=food_name,
        tokens={"in": total_in, "out": total_out},
        prompt=prompt,
        response=final_text,
    )
    return _parse(final_text)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", re.DOTALL)


def _parse(text: str) -> KnowledgeExtract:
    """Extract a single JSON object from arbitrary model output.

    Same permissive strategy as the extractor / vision parsers: direct parse,
    then fenced block, then largest balanced object (Gemma emits prose).
    """
    text = text.strip()
    try:
        return KnowledgeExtract.model_validate_json(text)
    except Exception:
        pass
    fence = _JSON_FENCE.search(text)
    if fence:
        try:
            return KnowledgeExtract.model_validate_json(fence.group(1))
        except Exception:
            pass
    matches = _JSON_OBJECT.findall(text)
    if matches:
        return KnowledgeExtract.model_validate_json(max(matches, key=len))
    raise ValueError(f"no JSON object found in knowledge output: {text[:300]!r}")
