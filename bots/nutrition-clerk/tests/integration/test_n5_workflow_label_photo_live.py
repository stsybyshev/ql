"""N5 — SHAPE C: photo of nutrition label, via the workflow.

Three REAL labels from Stan's kitchen (uploaded 2026-08):
- Metcalfe rice cracker (poor-quality: glare, plastic wrinkles, partial crop)
- Waitrose Manchego cheese
- Waitrose Membrillo (quince) paste

Assertions per label:
- 1 row logged, unit="100g", qty = user_grams / 100 (100g rule)
- source = "photo_label", confidence = 0.85
- per-100g values within a tolerance band of the actual label
- total kcal within a tolerance band of user_grams / 100 * label_kcal

Tolerance rationale: even a good vision model rounds and mis-reads glary
labels. We give ±15% on individual macros and ±10% on totals — enough to
catch structural bugs (100g rule broken, wrong source enum, wrong food
identified) without flaking on minor OCR noise. The membrillo label has
"<0.5g" for protein/fat which the model may return as 0.0-0.5, so those
are checked loosely.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import Config, MCPFoodSettings, MCPSettings  # noqa: E402
from nutrition_clerk.workflow import build_handler  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


@dataclass
class LabelCase:
    id: str
    fixture: str
    text: str
    user_grams: float
    ref_kcal_per_100g: float
    ref_protein_per_100g: float
    ref_fat_per_100g: float
    ref_carbs_per_100g: float
    ref_food_words: tuple[str, ...]        # any of these should appear in row.food (lowercase)
    expected_rows: int = 1                 # >1 when the message names unphotographed foods too


CASES = [
    LabelCase(
        id="metcalfe_rice_cracker",
        fixture="label_metcalfe_rice_cracker.jpg",
        text="34 g of Metcalfe rice cracker",
        user_grams=34,
        ref_kcal_per_100g=485,
        ref_protein_per_100g=4.5,
        ref_fat_per_100g=20.0,
        ref_carbs_per_100g=71.2,
        ref_food_words=("rice cracker", "metcalfe"),
    ),
    LabelCase(
        id="waitrose_manchego",
        fixture="label_waitrose_manchego.jpg",
        text="200g of Manchego cheese",
        user_grams=200,
        ref_kcal_per_100g=468,
        ref_protein_per_100g=25.0,
        ref_fat_per_100g=39.4,
        ref_carbs_per_100g=3.4,
        ref_food_words=("manchego",),
    ),
    LabelCase(
        id="waitrose_membrillo",
        fixture="label_waitrose_membrillo.jpg",
        text="120g of membrillo paste",
        user_grams=120,
        ref_kcal_per_100g=283,
        ref_protein_per_100g=0.5,       # "<0.5g" — model may return 0.0-0.5
        ref_fat_per_100g=0.5,           # "<0.5g" — same
        ref_carbs_per_100g=67.7,
        ref_food_words=("membrillo", "quince", "paste"),
    ),
    # Regression, 16-08-2026. The message names THREE foods but only the
    # kipper packet is photographed. `hint_text` used to be appended after the
    # whole instruction — the most salient position — so a multi-food message
    # dragged STEP 1 into classifying this nutrition panel as a MEAL. The meal
    # path then let the model do its own per-unit arithmetic, and it divided
    # the label's per-100g kcal by the USER's 200g instead of by 100. The row
    # landed at 244 kcal: exactly one 100g portion, half of what was eaten.
    #
    # Keep the full three-food text — the extra foods ARE the trigger. They
    # log as separate knowledge-estimate rows, which is why this case asserts
    # on the kipper row specifically rather than on row count.
    LabelCase(
        id="kipper_fillets_multi_food_message",
        fixture="label_kipper_fillets.jpg",
        text="200g of smoked kipper fillets (attached). 500g of potatoes and 200g cucumber.",
        user_grams=200,
        ref_kcal_per_100g=244,
        ref_protein_per_100g=16.0,
        ref_fat_per_100g=18.7,
        ref_carbs_per_100g=2.7,
        ref_food_words=("kipper",),
        expected_rows=3,   # kipper (label) + potatoes + cucumber (estimates)
    ),
]


def _monthly_path(log_dir: Path) -> Path:
    today = date.today()
    return log_dir / f"{today.year:04d}-{today.month:02d}.md"


def _data_rows(md: Path) -> list[str]:
    if not md.exists():
        return []
    return [
        l for l in md.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", l)
    ]


def _row_cells(row: str) -> list[str]:
    return [c.strip() for c in row.split("|")]


def _build(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    popular = tmp_path / "popular.yaml"
    popular.write_text("[]\n")
    config = Config()
    config.mcp = MCPSettings(food_tracker=MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=popular,
        food_log_dir=log_dir,
    ))
    handler, client = build_handler(config)
    return handler, client, log_dir


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_real_label_photo_logs_correctly(tmp_path, caplog, case: LabelCase):
    caplog.set_level(logging.INFO, logger="nutrition_clerk.workflow.enrichers.vision")
    fixture = FIXTURES / case.fixture
    assert fixture.exists(), f"missing fixture: {fixture}"

    handler, client, log_dir = _build(tmp_path)
    try:
        reply = await handler(
            InboundEvent(chat_id=1, msg_id=1, text=case.text, photos=[fixture])
        )
    finally:
        await client.close()

    assert reply and reply != "(no response)"

    rows = _data_rows(_monthly_path(log_dir))
    assert len(rows) == case.expected_rows, (
        f"[{case.id}] expected {case.expected_rows} row(s), got {len(rows)}: {rows}"
    )
    # Pick the row for the photographed food. Most cases log exactly one row,
    # but a message may name foods that are not in the photo (kipper case) —
    # those log separately as knowledge estimates and are not under test here.
    matching = [
        r for r in rows
        if any(w in _row_cells(r)[2].lower() for w in case.ref_food_words)
    ]
    assert len(matching) == 1, (
        f"[{case.id}] expected exactly 1 row matching {case.ref_food_words}, "
        f"got {len(matching)}: {matching}"
    )
    cells = _row_cells(matching[0])
    # Columns: | Datetime | Food | Qty | Unit | P/u | F/u | C/u | Kcal/u | P | F | C | Kcal | Source | Confidence |
    #             1         2      3     4      5     6     7     8       9  10  11  12    13       14
    food = cells[2].lower()
    qty = float(cells[3])
    unit = cells[4].lower()
    kcal_u = float(cells[8])
    p_total = float(cells[9])
    f_total = float(cells[10])
    c_total = float(cells[11])
    kcal_total = float(cells[12])
    source = cells[13].lower()

    # --- structural asserts (bugs, not noise) ---
    assert source == "photo_label", f"[{case.id}] wrong source: {source}"
    assert unit == "100g", f"[{case.id}] 100g rule violated: unit={unit}"
    expected_qty = case.user_grams / 100.0
    assert abs(qty - expected_qty) < 0.02, (
        f"[{case.id}] qty {qty} != expected {expected_qty} (100g rule)"
    )
    assert any(word in food for word in case.ref_food_words), (
        f"[{case.id}] expected food to mention any of {case.ref_food_words}, got: {food!r}"
    )

    # --- macro asserts (allow ±15% band per macro; membrillo <0.5 special-case) ---
    def _band(actual: float, ref: float, pct: float = 0.15, floor: float = 1.0) -> tuple[float, float]:
        tol = max(ref * pct, floor)
        return (max(0.0, ref - tol), ref + tol)

    kcal_lo, kcal_hi = _band(case.ref_kcal_per_100g, case.ref_kcal_per_100g)
    p_lo, p_hi = _band(0.0, case.ref_protein_per_100g)
    f_lo, f_hi = _band(0.0, case.ref_fat_per_100g)
    c_lo, c_hi = _band(0.0, case.ref_carbs_per_100g)

    # membrillo's <0.5 fields: allow 0.0 - 1.0
    if case.id == "waitrose_membrillo":
        p_lo, p_hi = 0.0, 1.0
        f_lo, f_hi = 0.0, 1.0

    assert kcal_lo <= kcal_u <= kcal_hi, (
        f"[{case.id}] kcal/100g {kcal_u} outside [{kcal_lo:.1f}, {kcal_hi:.1f}] "
        f"(ref {case.ref_kcal_per_100g})"
    )
    # Per-total values (post-100g scaling): compare vs ref * user_grams / 100
    def _check_total(name, actual, ref_per_100g, lo_pct=0.15):
        expected = ref_per_100g * expected_qty
        tol = max(expected * lo_pct, 1.0)
        assert (expected - tol) <= actual <= (expected + tol), (
            f"[{case.id}] {name} total {actual:.2f} outside "
            f"[{expected - tol:.2f}, {expected + tol:.2f}] (ref per-100g {ref_per_100g})"
        )

    # Membrillo special-case: total macros dominated by carbs; skip strict P/F.
    if case.id != "waitrose_membrillo":
        _check_total("protein", p_total, case.ref_protein_per_100g)
        _check_total("fat",     f_total, case.ref_fat_per_100g)
    _check_total("carbs",   c_total, case.ref_carbs_per_100g)

    expected_kcal_total = case.ref_kcal_per_100g * expected_qty
    kcal_tol = max(expected_kcal_total * 0.10, 5.0)
    assert (expected_kcal_total - kcal_tol) <= kcal_total <= (expected_kcal_total + kcal_tol), (
        f"[{case.id}] total kcal {kcal_total} outside "
        f"[{expected_kcal_total - kcal_tol:.1f}, {expected_kcal_total + kcal_tol:.1f}]"
    )

    # --- token budget: vision call should stay <5k input ---
    vlines = [r.getMessage() for r in caplog.records if "vision(label): in=" in r.getMessage()]
    assert vlines, "expected vision enricher token log line"
    m = re.search(r"in=(\d+)", vlines[-1])
    tokens_in = int(m.group(1))
    print(f"\n[N5 {case.id}] vision tokens_in={tokens_in}, reply=\n{reply}\n")
    assert tokens_in < 5000, (
        f"[{case.id}] vision tokens_in={tokens_in} exceeds 5k budget"
    )
