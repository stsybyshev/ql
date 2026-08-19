"""Compare vision-model quality + cost for the 3 real label fixtures.

Runs extract_label(...) once per (label, model) pair, records time, tokens,
and the parsed LabelExtract. Prints a compact table + a per-macro delta vs
the ground-truth reference.

Ground-truth values are read manually off each label (see LabelCase.ref_*).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from google.adk.models.lite_llm import LiteLlm

from nutrition_clerk.workflow.enrichers.vision import extract_label

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


@dataclass
class LabelCase:
    id: str
    fixture: str
    hint: str
    kcal: float
    protein: float
    fat: float
    carbs: float


CASES = [
    LabelCase("Metcalfe rice cracker", "label_metcalfe_rice_cracker.jpg",
              "Metcalfe rice cracker", 485, 4.5, 20.0, 71.2),
    LabelCase("Waitrose Manchego",     "label_waitrose_manchego.jpg",
              "Manchego cheese",       468, 25.0, 39.4, 3.4),
    LabelCase("Waitrose Membrillo",    "label_waitrose_membrillo.jpg",
              "Membrillo paste",       283, 0.5, 0.5, 67.7),
]


# (model_id, display_name, per-1M-input $, per-1M-output $)
MODELS = [
    ("anthropic/claude-sonnet-5",    "Sonnet 5",   3.00, 15.00),
    ("anthropic/claude-haiku-4-5",   "Haiku 4.5",  1.00,  5.00),
    ("ollama_chat/gemma4:e4b",       "Gemma4 e4b (local)", 0.00, 0.00),
]


def _pct_err(actual, ref):
    if ref == 0:
        return abs(actual) if actual else 0
    return (actual - ref) / ref * 100


async def run_one(model_id: str, case: LabelCase):
    kwargs = {}
    if model_id.startswith("ollama_chat/"):
        kwargs["api_base"] = "http://localhost:11434"
    llm = LiteLlm(model=model_id, **kwargs)
    t0 = time.time()
    try:
        result = await extract_label(llm, FIXTURES / case.fixture, hint_name=case.hint)
        elapsed = time.time() - t0
        return {"ok": True, "elapsed": elapsed, "result": result, "err": None}
    except Exception as e:
        return {"ok": False, "elapsed": time.time() - t0, "result": None, "err": str(e)[:120]}


async def main():
    print(f"\n{'Label':<24} {'Model':<24} {'Time':>7} {'kcal':>8} {'P':>7} {'F':>7} {'C':>7}  Δ vs ref")
    print("-" * 130)

    for case in CASES:
        print(f"\n{case.id:<24} {'(ground truth)':<24} {'—':>7} {case.kcal:>7.0f} {case.protein:>7.1f} {case.fat:>7.1f} {case.carbs:>7.1f}")
        for model_id, display, in_price, out_price in MODELS:
            r = await run_one(model_id, case)
            if not r["ok"]:
                print(f"{'':<24} {display:<24} {r['elapsed']:>6.1f}s FAILED: {r['err']}")
                continue
            ex = r["result"]
            dk = _pct_err(ex.kcal_per_100g, case.kcal)
            dp = _pct_err(ex.protein_per_100g, case.protein)
            df = _pct_err(ex.fat_per_100g, case.fat)
            dc = _pct_err(ex.carbs_per_100g, case.carbs)
            delta = f"kcal{dk:+.0f}%  P{dp:+.0f}%  F{df:+.0f}%  C{dc:+.0f}%"
            print(
                f"{'':<24} {display:<24} {r['elapsed']:>6.1f}s "
                f"{ex.kcal_per_100g:>7.0f} {ex.protein_per_100g:>7.1f} "
                f"{ex.fat_per_100g:>7.1f} {ex.carbs_per_100g:>7.1f}  {delta}"
            )
            if ex.confidence_note:
                print(f"{'':<24} {'':>24}   note: {ex.confidence_note[:100]}")


if __name__ == "__main__":
    asyncio.run(main())
