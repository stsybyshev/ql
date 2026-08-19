"""Vision enricher — one LLM call per label photo.

Uses LiteLlm.generate_content_async directly (no LlmAgent, no session,
no tool loop). Given a photo path + a hint (the food name the user typed),
returns per-100g macros as a `LabelExtract`.

Called from the orchestrator, once per photo that lacks a cache/user-typed
alternative. If the label is unreadable, the LLM sets `confidence_note` and
the orchestrator may downgrade the row's confidence or refuse the log.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from nutrition_clerk.workflow import trace
from nutrition_clerk.workflow.schemas import LabelExtract, PhotoExtract

log = logging.getLogger("nutrition_clerk.workflow.enrichers.vision")


PHOTO_ANALYSE_INSTRUCTION = """\
You look at ONE photograph attached to a food-logging message and decide what
it is, then extract the right kind of data. Return ONLY a single JSON object
with EXACTLY these keys:

    {
      "kind": "label" | "meal" | "unclear",
      "label": {
        "label_name": string or null,
        "kcal_per_100g": number,
        "protein_per_100g": number,
        "fat_per_100g": number,
        "carbs_per_100g": number,
        "confidence_note": string or null
      } or null,
      "dishes": [
        {
          "name": string,
          "qty": number,
          "unit": string,
          "kcal_per_unit": number,
          "protein_per_unit": number,
          "fat_per_unit": number,
          "carbs_per_unit": number,
          "confidence": number,
          "note": string or null
        }
      ],
      "unclear_reason": string or null
    }

No prose, no explanation, no code fences — just the JSON object.

STEP 1 — classify the photo. Decide this from THE IMAGE ALONE.

The user's message is given above for context, but it MUST NOT influence
this step. In particular:
  - How many foods the user names has NO bearing on the classification. A
    message listing three foods alongside one packet photo is still a LABEL
    photo of one product. The other foods are simply not pictured — the
    orchestrator handles them separately, without your help.
  - Words like "dinner", "meal", "lunch" in the message do NOT make the
    image a meal photo.
  - Only what you can SEE decides `kind`.

Apply these tests IN ORDER:

1. Can you read PRINTED NUTRIENT ROWS anywhere in the image — lines pairing
   a nutrient name with a number, such as "Energy 2034kJ / 485kcal",
   "Fat 20.0g", "Carbohydrate 71.2g", "Protein 4.5g", "Salt 0.08g"?
   -> YES: kind="label". Populate `label`, leave `dishes` empty.

   Decisive notes:
   - The nutrient ROWS are what matter. A heading like "Nutrition" or
     "Typical values" is a helpful hint but is NOT required — headings are
     often lost to glare, wrinkles, or a tight crop. If you can read the
     rows, it is a label.
   - This holds EVEN IF packaging, branding, or the food itself is visible
     in the frame. A photo of a snack packet or a cheese wrapper showing its
     nutrition rows is a LABEL, not a meal.
   - Glare, plastic wrinkles, skew, and partial crops are NORMAL for label
     photos taken in a kitchen. Do not downgrade to "meal" or "unclear"
     just because the image is imperfect — read what you can and record any
     difficulty in `confidence_note`.
   - SELF-CHECK: if you are about to write a `note` or `confidence_note`
     that quotes figures read off a nutrition panel ("244 kcal per 100g as
     printed"), then you ARE reading a label and `kind` MUST be "label".
     Never report panel figures from inside a `dishes` entry.

2. Otherwise, does the image show actual food to eat — a plated dish, a
   bowl, a restaurant table, a takeaway container, food on a board?
   -> YES: kind="meal". Populate `dishes`, leave `label` null.

3. Otherwise (not food, or too blurry/dark/cropped to use):
   -> kind="unclear". Set `unclear_reason`, leave `label` null and
      `dishes` empty.

Never try to read a nutrition table off a plate of food, and never estimate
dish portions from a photo whose nutrition panel you can already read.

STEP 2a — when kind="label", fill `label` using these rules:
- ALWAYS return values PER 100G. If the label only shows per-serving values,
  convert to per-100g using the serving size printed on the label. Note the
  conversion in `confidence_note`.
- Read from the "Typical values" / "Nutritional information" / "Nutrition
  Facts" table only. Ignore ingredients list, allergy advice, storage, dates.
- Use `kcal`, NOT `kJ`. If both are printed, use `kcal`. If only `kJ` is
  visible, convert kcal ≈ kJ / 4.184 and note in `confidence_note`.
- For "less than" values like "<0.5g", record 0.5 (upper bound) and mention
  in `confidence_note`.
- If a field is illegible or missing (glare, blur, cutoff), record your best
  estimate and mention what was hard in `confidence_note`. Never invent a
  value that isn't at least partially visible.
- `label_name` should be the product name if visible in the crop; otherwise
  null. The user's hint may help but do NOT fabricate — return null if you
  can't actually see a name.
- Use total Fat (not "of which saturates") and total Carbohydrate (not "of
  which sugars"). If saturates is bigger than fat something is wrong — flag
  it in `confidence_note`.

STEP 2b — when kind="meal", fill `dishes`:
- The user's message (given below) names what they ate. Emit ONE dish entry
  per item they named, using THEIR wording for `name`.
- If the user named a dish you cannot see in the photo, still include it —
  the photo may be partial or the dish already eaten.
- Do NOT add dishes the user did not mention, even if visible. They chose
  what to track.
- Use the photo to judge PORTION: bowl size, plate coverage, piece count,
  cutlery for scale. Put the assumption in `note` ("~350g bowl", "3 slices").
- `unit`: a natural unit the user would say — "serving", "bowl", "plate",
  "slice", "piece", "cup". Use `qty` for counts ("2 slices" -> qty=2,
  unit="slice").
- `kcal_per_unit` and macros are PER UNIT, not totals. If you say qty=2 and
  unit="slice", the macros are for ONE slice.
- `confidence`: 0.3-0.5. Visual portion estimates are inherently rough —
  never claim more certainty than that. Use the low end for mixed/obscured
  dishes, the high end for a clearly-visible standard portion.
- Restaurant dishes are usually larger and oilier than home cooking; account
  for cooking oil, sauces, and dressings you can see.
"""


async def analyse_photo(
    model: BaseLlm,
    photo_path: Path,
    hint_text: str,
) -> PhotoExtract:
    """One vision call that classifies the photo AND extracts from it.

    `hint_text` is the user's message — for a label it disambiguates a glary
    crop; for a meal it tells the model which dishes to itemise.

    Classifying inside the same call (rather than a separate hop) keeps this
    at ONE LLM call per photo.
    """
    photo_bytes = photo_path.read_bytes()
    mime = _guess_mime(photo_path)

    # Inline the instruction into the user message rather than using
    # `system_instruction`. LiteLLM's Ollama path silently drops or misroutes
    # system instructions for some models, whereas the user message is always
    # respected. Anthropic works either way.
    #
    # The hint goes FIRST, before the instruction. It used to be appended last
    # — the most salient position — and that let it drive STEP 1: a message
    # naming three foods ("200g kipper fillets. 500g potatoes and 200g
    # cucumber") made the model classify a nutrition-panel photo as a MEAL and
    # estimate portions instead of reading the panel. Leading with the hint,
    # then instructing at length that classification comes from the image
    # alone, puts the rules after the bias rather than before it.
    user_text = (
        f"The user's message with this photo was: {hint_text!r}\n\n"
        + PHOTO_ANALYSE_INSTRUCTION
    )
    request = LlmRequest(
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(text=user_text),
                    types.Part.from_bytes(data=photo_bytes, mime_type=mime),
                ],
            ),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            # NOTE: no `response_schema=` — LiteLLM doesn't forward Google-style
            # schemas to Ollama, and Anthropic accepts the mime hint alone.
            # Structure enforced via prompt + `_parse` extracts the JSON blob.
            # NOTE: no `temperature=` — Sonnet 5 only supports temperature=1;
            # leaving it unset lets each provider pick its own default.
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

    result = _parse(final_text)
    log.info(
        "vision(%s): in=%d out=%d dishes=%d hint=%r",
        result.kind, total_in, total_out, len(result.dishes), hint_text[:60],
    )
    trace.record(
        "vision",
        ms=round((time.perf_counter() - _t0) * 1000, 1),
        model=getattr(model, "model", str(model)),
        photo=str(photo_path),
        kind=result.kind,
        dishes=len(result.dishes),
        tokens={"in": total_in, "out": total_out},
        prompt=user_text,
        response=final_text,
    )
    return result


async def extract_label(
    model: BaseLlm,
    photo_path: Path,
    hint_name: str,
) -> LabelExtract:
    """Back-compat shim: label-only view of `analyse_photo`.

    Used by scripts/compare_vision_models.py. Raises if the photo turns out
    not to be a label — callers in the workflow use `analyse_photo` directly
    so they can handle the meal / unclear cases.
    """
    result = await analyse_photo(model, photo_path, hint_text=hint_name)
    if result.kind != "label" or result.label is None:
        raise ValueError(
            f"photo classified as {result.kind!r}, not a nutrition label"
            + (f": {result.unclear_reason}" if result.unclear_reason else "")
        )
    return result.label


_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _guess_mime(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


import re as _re

_JSON_FENCE = _re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", _re.DOTALL)
_JSON_OBJECT = _re.compile(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", _re.DOTALL)


def _parse(text: str) -> PhotoExtract:
    """Extract a single JSON object from arbitrary model output.

    Robust to: plain JSON, code-fenced JSON (```json ... ```), and
    "thinking" prose surrounding a JSON block (Gemma/Ollama pattern).
    """
    text = text.strip()

    # 1) Try direct parse (Anthropic returns clean JSON when response_mime_type=json).
    try:
        return PhotoExtract.model_validate_json(text)
    except Exception:
        pass

    # 2) Try to extract from a ```json ... ``` fence.
    fence = _JSON_FENCE.search(text)
    if fence:
        try:
            return PhotoExtract.model_validate_json(fence.group(1))
        except Exception:
            pass

    # 3) Grab the largest balanced-looking {...} in the text (skips prose).
    #    Nested-object aware so a `label` sub-object doesn't truncate the match.
    matches = _JSON_OBJECT.findall(text)
    if matches:
        largest = max(matches, key=len)
        try:
            return PhotoExtract.model_validate_json(largest)
        except Exception:
            pass

    # 4) Never crash the turn on an unparseable photo response — a raised
    #    exception here means the Telegram offset is never committed and the
    #    message redelivers forever. Degrade to "unclear" instead.
    log.warning("could not parse vision output; treating as unclear: %r", text[:200])
    return PhotoExtract(
        kind="unclear",
        unclear_reason="I couldn't read that photo clearly.",
    )
