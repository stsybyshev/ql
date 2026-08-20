"""Orchestrator — deterministic Python that turns ExtractedMessage into log rows.

N1 scope: SHAPE A cache-lookup only.
- Look up each entry by name in personal + popular caches.
- If a hit: log with `source="cache_lookup"`, `confidence=0.95`.
- If no hit: fall back to a stub "unknown food" reply (knowledge estimation
  arrives in N6).
- No photos, no user-typed macros, no save-to-cache, no clarify — those
  branches land in N2-N5.

Datetime handling: minimal for N1 — `_resolve_datetime` supports None/"now"
and DD-MM-YYYY HH:MM passthrough. Richer parsing ("this morning", "2 hours
ago", ...) lands in N6.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from google.adk.models.base_llm import BaseLlm
from rapidfuzz import fuzz

from nutrition_clerk.workflow import datetime_resolver
from nutrition_clerk.workflow.context import ChatContext
from nutrition_clerk.workflow.enrichers.knowledge import estimate_macros
from nutrition_clerk.workflow.enrichers.vision import analyse_photo
from nutrition_clerk.workflow.food_cache_client import FoodCacheClient
from nutrition_clerk.workflow.schemas import (
    ExtractedEntry,
    ExtractedMessage,
    LabelExtract,
    MealDish,
    PhotoExtract,
)


# rapidfuzz thresholds (from the old M5 rank tool — same values).
# top match must score ≥ MIN_TOP_SCORE AND beat the runner-up by ≥ MIN_MARGIN;
# otherwise we ask the user to disambiguate.
_MIN_TOP_SCORE = 90
_MIN_MARGIN = 15


def _disambiguate_cache_hits(
    query: str,
    hits: list[dict],
    *,
    force: bool = False,
) -> tuple[dict | None, str | None]:
    """Rank cache hits by fuzzy match against the user's query.

    Args:
        force: never return a question — take the best hit. Used when we are
            already resuming a clarification, so a second ambiguous result
            cannot put us in an ask-forever loop.

    Returns:
        (top_hit, None) if unambiguous — orchestrator proceeds with top_hit.
        (None, question) if ambiguous — orchestrator sets pending_clarification.
    """
    if len(hits) == 1:
        return hits[0], None

    # Exact-name match wins outright. Without this, answering a clarification
    # with one of the offered names ("cappuccino") stays "ambiguous" whenever
    # that name is a substring of another candidate ("small cappuccino"):
    # scores 100 vs 90, margin 10 < 15, so we would ask the same question
    # forever. If the user names a candidate exactly, that IS the answer.
    # Aliases count as names here: "spicy eggs" and "huevos" ARE what the
    # user calls "Spanish eggs", and are why the matcher returned that entry
    # at all. Matching on `name` alone let the generic "egg" beat it.
    exact = [h for h in hits if _names_match_exactly(query, h)]
    if len(exact) == 1:
        return exact[0], None
    # Case-insensitive: cache names are usually title-case ("Apple"), user
    # inputs are usually lowercase. WRatio is case-sensitive, so normalise
    # both sides — otherwise "apple" vs "Apple" scores 80 (in the ambiguity
    # band) instead of 100.
    q_norm = query.lower()

    def _score(hit: dict) -> int:
        """Best match across the entry's name AND its aliases.

        Ranking on `name` alone discarded the very information the lookup
        used to find the entry: "fried eggs with cumin" is a listed alias of
        "Spanish eggs" (alias score 100) but scores only 85.5 against that
        NAME, losing to the generic "egg" at 90. `fuzzy_lookup` has always
        scored over aliases too; this brings the two paths in line.
        """
        candidates = [hit.get("name") or ""] + list(hit.get("aliases") or [])
        return max(
            (int(fuzz.WRatio(q_norm, str(c).lower())) for c in candidates if c),
            default=0,
        )

    scored = sorted(
        ({"hit": h, "score": _score(h)} for h in hits),
        key=lambda x: x["score"],
        reverse=True,
    )
    top, second = scored[0], scored[1]
    ambiguous = top["score"] < _MIN_TOP_SCORE or (top["score"] - second["score"]) < _MIN_MARGIN
    if not ambiguous or force:
        if ambiguous and force:
            log.info(
                "still ambiguous for %r after a clarification; taking best match %r"
                " rather than asking again",
                query, top["hit"].get("name"),
            )
        return top["hit"], None
    # Build a short question listing the top 2-3 candidates by name.
    names = [s["hit"].get("name", "?") for s in scored[:3]]
    # Two entries can share a name and differ only in unit — personal
    # "buckwheat" is per-100g, popular "buckwheat" is per-cup. Offering the
    # bare names produced "Did you mean buckwheat or buckwheat?", which the
    # user cannot answer. Qualify with the unit when the labels collide.
    if len(set(names)) != len(names):
        names = [
            f'{s["hit"].get("name", "?")} (per {s["hit"].get("unit") or "serving"})'
            for s in scored[:3]
        ]
    if len(names) == 2:
        question = f"Did you mean {names[0]} or {names[1]}?"
    else:
        question = f"Did you mean {names[0]}, {names[1]}, or {names[2]}?"
    return None, question

log = logging.getLogger("nutrition_clerk.workflow.orchestrator")


@dataclass
class LoggedRow:
    """A row we successfully appended to the monthly MD."""

    food: str
    qty: float
    unit: str
    kcal_total: float
    protein_total: float
    fat_total: float
    carbs_total: float
    source: str
    today_totals: dict[str, float] = field(default_factory=dict)
    # N2: whether a save-to-cache was attempted and how it went.
    #   None           -> user didn't ask to save
    #   "saved"        -> add_personal_food succeeded
    #   "duplicate"    -> already in personal cache (add_personal_food error)
    #   "ineligible"   -> save requested but not applied (LLM-estimated etc.)
    save_status: str | None = None
    # N6: True when macros came from the knowledge enricher (world-knowledge
    # guess) rather than cache / user-typed / label. Formatter flags these
    # so the user knows to trust them less.
    estimated: bool = False


@dataclass
class UnknownEntry:
    """An entry we couldn't resolve — for N1 this means cache miss (N6 will
    replace this with a knowledge-enricher LLM call)."""

    name: str
    reason: str


@dataclass
class RecentEntry:
    """A snapshot of a just-logged row, kept in ChatContext.recent_entries
    so shape-6.4 requests ("save the pomegranate juice I had") can promote
    it into personal-foods without re-asking the user for macros."""

    name: str
    qty: float
    unit: str
    kcal_per_unit: float
    protein_per_unit: float
    fat_per_unit: float
    carbs_per_unit: float
    source: str


@dataclass
class SavedRecentEntry:
    """Result of a SHAPE 6.4 promotion attempt (formatter renders this)."""

    name: str
    unit: str
    kcal_per_unit: float
    protein_per_unit: float
    fat_per_unit: float
    carbs_per_unit: float
    status: str  # "saved" | "duplicate" | "not_found"


@dataclass
class OrchestratorResult:
    logged: list[LoggedRow] = field(default_factory=list)
    unknown: list[UnknownEntry] = field(default_factory=list)
    # N3: shape-6.4 promotions — items pulled from ChatContext.recent_entries
    # and sent to add_personal_food (no new log row).
    saved_recent: list[SavedRecentEntry] = field(default_factory=list)
    # N4: set when a cache lookup came back ambiguous (multiple close-match
    # hits). The graph writes it to ChatContext.pending_clarification and
    # appends the question to the reply. `logged`/`unknown` are still
    # populated for the OTHER entries in the same message.
    pending_clarification: str | None = None
    # Entries we could not resolve this turn, in message order. The graph
    # stores these so the next turn can resume ONLY these — never the whole
    # original message (which would re-log the entries that already worked).
    unresolved: list[ExtractedEntry] = field(default_factory=list)


def _resolve_datetime(hint: str | None) -> str:
    """Resolve a free-text time hint to "DD-MM-YYYY HH:MM".

    N6: delegates to `datetime_resolver.resolve`, which handles relative
    offsets ("2 hours ago"), day anchors ("yesterday"), named periods
    ("this morning"), and explicit clock times ("8pm"). Kept as a thin
    wrapper so existing call sites and tests stay unchanged.
    """
    return datetime_resolver.resolve(hint)


def _apply_100g_rule(entry: ExtractedEntry, cache_hit: dict[str, Any]) -> tuple[float, str]:
    """Normalise weight-based quantities per the CRITICAL 100g rule.

    If the user said grams (unit="g") AND the cache entry is per-100g
    (unit="100g"), we must convert: qty = grams/100, unit="100g". Passing
    unit="g" with per-100g rates produces values 100x too high — the exact
    production bug we've been burned by.

    Returns (qty, unit) to pass into log_food.
    """
    user_unit = (entry.unit or "").strip().lower()
    cache_unit = (cache_hit.get("unit") or "").strip().lower()
    qty = entry.qty if entry.qty is not None else float(cache_hit.get("qty_default", 1))

    if user_unit in ("g", "gram", "grams") and cache_unit == "100g":
        return qty / 100.0, "100g"
    if _units_compatible(user_unit, cache_unit, qty):
        # Use the cache's canonical unit — its per-unit rates match it.
        return qty, cache_unit or user_unit or "serving"
    # Units don't line up, so the user's COUNT cannot be carried over: their
    # number counts something the cache doesn't measure. Callers only reach
    # here for a hit whose name the user gave exactly (see `_usable_hits`), so
    # the food is right and only the quantity is unmappable — fall back to the
    # entry's own default serving rather than multiplying by a foreign number.
    #
    # This is what turned "3 egg omelette" into 3 servings of a per-serving
    # "3 eggs omelette" entry: nine eggs' worth of calories.
    fallback = float(cache_hit.get("qty_default", 1) or 1)
    log.info(
        "unit mismatch for %r (asked %s, cache %s) — using qty_default=%s %s",
        entry.name, user_unit or "-", cache_unit or "-", fallback, cache_unit,
    )
    return fallback, cache_unit or "serving"


async def _log_from_cache_hit(
    client: FoodCacheClient,
    entry: ExtractedEntry,
    hit: dict[str, Any],
    when: str,
    confidence: float = 0.95,
) -> LoggedRow:
    """Log one entry using cache-derived per-unit macros.

    `confidence` defaults to 0.95 for exact-substring matches. Fuzzy fallback
    (N4.5) passes 0.85 to signal "we matched despite a typo/formatting mismatch".
    """
    qty, unit = _apply_100g_rule(entry, hit)
    response = await client.log_food(
        datetime=when,
        food=hit["name"],
        qty=qty,
        unit=unit,
        kcal_per_unit=float(hit["kcal_per_unit"]),
        protein_per_unit=float(hit["protein_per_unit"]),
        fat_per_unit=float(hit["fat_per_unit"]),
        carbs_per_unit=float(hit["carbs_per_unit"]),
        source="cache_lookup",
        confidence=confidence,
    )
    row = response.get("entry", {})
    return LoggedRow(
        food=row.get("food", hit["name"]),
        qty=qty,
        unit=unit,
        kcal_total=float(row.get("kcal_total", qty * float(hit["kcal_per_unit"]))),
        protein_total=float(row.get("protein_total", qty * float(hit["protein_per_unit"]))),
        fat_total=float(row.get("fat_total", qty * float(hit["fat_per_unit"]))),
        carbs_total=float(row.get("carbs_total", qty * float(hit["carbs_per_unit"]))),
        source="cache_lookup",
        today_totals=response.get("today", {}),
    )


def _is_shape_b(entry: ExtractedEntry) -> bool:
    """User typed explicit macros — kcal is the marker; the rest may be 0."""
    return entry.kcal is not None


async def _log_shape_b(
    client: FoodCacheClient,
    entry: ExtractedEntry,
    when: str,
) -> LoggedRow:
    """Log SHAPE B: user-typed macros.

    Convention (matches the M4 behaviour in the archived prompt): treat the
    whole entry as ONE SERVING of the named food. This intentionally loses
    fine-grained gram info in favour of a simple, predictable schema for the
    personal cache. If the user wants gram-based tracking, they should send
    a label photo (SHAPE C, N5).
    """
    kcal = float(entry.kcal or 0)
    p = float(entry.protein_g or 0)
    f = float(entry.fat_g or 0)
    c = float(entry.carbs_g or 0)
    food_name = entry.name.strip()

    response = await client.log_food(
        datetime=when,
        food=food_name,
        qty=1.0,
        unit="serving",
        kcal_per_unit=kcal,
        protein_per_unit=p,
        fat_per_unit=f,
        carbs_per_unit=c,
        source="text_estimate",
        confidence=0.85,
    )
    row = response.get("entry", {})
    return LoggedRow(
        food=row.get("food", food_name),
        qty=1.0,
        unit="serving",
        kcal_total=kcal,
        protein_total=p,
        fat_total=f,
        carbs_total=c,
        source="text_estimate",
        today_totals=response.get("today", {}),
    )


# Volume measures a label photo can be scaled by, in millilitres. Only
# UNAMBIGUOUS ones belong here — "can" and "bottle" are deliberately absent
# because they vary by product, and inventing a number is what this table
# exists to stop.
#
# UK pint (568ml), not US (473ml). Getting that wrong is a silent 20% error.
_ML_PER_UNIT = {
    "ml": 1.0, "millilitre": 1.0, "milliliter": 1.0,
    "cl": 10.0,
    "l": 1000.0, "litre": 1000.0, "liter": 1000.0,
    "pint": 568.0, "pints": 568.0,
    "half pint": 284.0, "half-pint": 284.0,
}


def _label_quantity(entry: ExtractedEntry) -> tuple[float, str, bool]:
    """Scale a label's per-100 values by what the user actually said.

    Returns (qty, unit, assumed). `assumed` is True when we could not derive a
    quantity and fell back to a single 100g portion — the caller marks the row
    so the reply says so out loud.

    A silent fallback here is what logged "1 pint Lucky Saint" as 1 x 100g:
    the branch only understood grams, so every volume landed on the default.
    """
    qty = entry.qty
    unit = (entry.unit or "").strip().lower()

    if qty is not None:
        if unit in ("g", "gram", "grams"):
            return float(qty) / 100.0, "100g", False
        if unit in _ML_PER_UNIT:
            ml = float(qty) * _ML_PER_UNIT[unit]
            return ml / 100.0, "100ml", False

    log.info(
        "SHAPE C: cannot scale label by %s %r for %r — assuming one 100g portion",
        qty, unit or "-", entry.name,
    )
    return 1.0, "100g", True

async def _log_shape_c_label(
    client: FoodCacheClient,
    label: LabelExtract,
    entry: ExtractedEntry,
    when: str,
) -> LoggedRow:
    """Log SHAPE C: photo of nutrition label.

    Honours the CRITICAL 100g rule when the user's quantity is in grams:
    qty = grams/100, unit="100g", per-unit values as the enricher returned
    them. If the user did NOT specify grams, we assume one 100g portion —
    the user can correct with a follow-up.
    """
    # Choose display name: label's if it read one, else user's hint.
    food_name = (label.label_name or entry.name).strip()

    qty, unit, assumed_portion = _label_quantity(entry)

    response = await client.log_food(
        datetime=when,
        food=food_name,
        qty=qty,
        unit=unit,
        kcal_per_unit=float(label.kcal_per_100g),
        protein_per_unit=float(label.protein_per_100g),
        fat_per_unit=float(label.fat_per_100g),
        carbs_per_unit=float(label.carbs_per_100g),
        source="photo_label",
        # An assumed portion is a guess about quantity, not about the label.
        confidence=0.6 if assumed_portion else 0.85,
    )
    row = response.get("entry", {})
    return LoggedRow(
        food=row.get("food", food_name),
        qty=qty,
        unit=unit,
        kcal_total=float(row.get("kcal_total", qty * float(label.kcal_per_100g))),
        protein_total=float(row.get("protein_total", qty * float(label.protein_per_100g))),
        fat_total=float(row.get("fat_total", qty * float(label.fat_per_100g))),
        carbs_total=float(row.get("carbs_total", qty * float(label.carbs_per_100g))),
        source="photo_label",
        # Say so in the reply — the portion was invented, the macros were not.
        estimated=assumed_portion,
        today_totals=response.get("today", {}),
    )


async def _try_save_to_cache(
    client: FoodCacheClient,
    entry: ExtractedEntry,
    logged: LoggedRow,
) -> str:
    """Attempt add_personal_food using the just-logged per-unit values.

    Returns one of: "saved", "duplicate", "ineligible".
    """
    if logged.source != "text_estimate":
        # cache_lookup: food already exists in cache — no save needed.
        # (Photo/knowledge estimates are handled in later milestones with the
        # same 'ineligible' gate for LLM-estimated ones.)
        return "ineligible"
    resp = await client.add_personal_food(
        name=logged.food,
        unit=logged.unit,
        kcal_per_unit=logged.kcal_total,           # per-serving = totals for SHAPE B
        protein_per_unit=logged.protein_total,
        fat_per_unit=logged.fat_total,
        carbs_per_unit=logged.carbs_total,
        qty_default=logged.qty,
    )
    if isinstance(resp, dict) and resp.get("error"):
        log.info("add_personal_food rejected: %s", resp["error"])
        return "duplicate"
    return "saved"


# NOTE: food-name casing is normalised by the MCP server
# (food_cache.normalize_food_name), applied once in its write path so
# every client shares the convention. Deliberately NOT done here.


def _snapshot(name: str, qty: float, unit: str, per_unit: dict, source: str) -> RecentEntry:
    """Build a RecentEntry snapshot from a just-logged row's per-unit values."""
    return RecentEntry(
        name=name,
        qty=qty,
        unit=unit,
        kcal_per_unit=float(per_unit["kcal"]),
        protein_per_unit=float(per_unit["protein"]),
        fat_per_unit=float(per_unit["fat"]),
        carbs_per_unit=float(per_unit["carbs"]),
        source=source,
    )


def _find_recent(name: str, recent: list[RecentEntry], min_score: int = 70) -> RecentEntry | None:
    """Fuzzy-find a recently-logged entry by name. Threshold is deliberately
    lower than the personal-foods fuzzy lookup (70 vs 85) because the recent
    ring is small (max 10) — false positives are rare and the user just
    typed the name from memory."""
    if not recent:
        return None
    q = name.lower().strip()
    if not q:
        return None
    scored = sorted(
        ((int(fuzz.WRatio(q, r.name.lower())), r) for r in recent),
        key=lambda x: -x[0],
    )
    top_score, top = scored[0]
    return top if top_score >= min_score else None


# Units whose per-unit rates are tied to a weight/volume basis. Substituting a
# cache hit for a vision estimate is only safe when both sides agree on the
# basis — "1 piece" logged against per-100g rates is meaningless.
_WEIGHT_UNITS = {"100g", "g", "gram", "grams", "100ml", "ml"}
# Per-100 bases. A quantity in g/ml must be DIVIDED by 100 against these —
# using it raw is the 100x bug in its original form.
_PER_100_UNITS = {"100g", "100ml"}
# Vision's stand-in for "one normal helping". Treated as compatible with any
# non-weight cache unit, since a cache entry's own unit IS its normal helping.
_GENERIC_UNITS = {"", "serving", "servings", "portion", "portions"}


def _units_compatible(
    asked_unit: str | None,
    cache_unit: str | None,
    qty: float | None = None,
) -> bool:
    """Can a cache hit's per-unit macros be multiplied by `qty` of `asked_unit`?

    This is the guard against the whole class of "silently multiplied" bugs.
    A cache entry's macros are per ITS OWN unit; using them against a
    different unit multiplies by a meaningless number:

        "150g tomato juice" matched cache "tomato" (22 kcal per TOMATO)
        -> 150 x 22 = 3300 kcal for a glass of juice.

    Name similarity cannot catch this — measured on the real failures,
    "tomato juice"->"tomato" scores 90 while the CORRECT
    "dark chocolate bar"->"dark chocolate" scores 95. The unit separates
    them cleanly where the name does not.
    """
    d = (asked_unit or "").strip().lower()
    c = (cache_unit or "").strip().lower()
    if d == c:
        return True
    # Grams against a per-100g cache entry: _apply_100g_rule converts cleanly.
    if d in _WEIGHT_UNITS and c in _WEIGHT_UNITS:
        return True
    # A weight-based entry on one side needs a weight on the other. Guessing
    # the grams in "1 piece" or "1 tomato" is the 100x bug we keep meeting.
    if c in _WEIGHT_UNITS or d in _WEIGHT_UNITS:
        return False
    # A generic unit means "one normal helping", and a cache entry's own unit
    # IS its normal helping — so the two line up at qty=1 only. At qty>1 the
    # mapping is invented: "3 egg omelette" against a per-SERVING entry for
    # "3 eggs omelette" logged 3 servings, i.e. nine eggs.
    if d in _GENERIC_UNITS or c in _GENERIC_UNITS:
        return qty is None or abs(float(qty) - 1.0) < 1e-9
    return False


def _usable_hits(entry: ExtractedEntry, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop cache hits whose unit cannot carry the user's quantity.

    The MCP's `search_foods` is a substring matcher, so it returns confidently
    wrong food: "Tomato juice" -> "tomato", "Red grapefruit" -> "grapes" (via
    the alias "red grapes", since "grape" is a substring of "grapefruit").
    Nothing downstream questioned a single hit, so those logged at 3300 kcal
    and as a cup of grapes respectively.

    A mismatched unit is the tell. Rather than guess at names — measured
    fuzzy scores put a wrong match (90) below a right one (95) with no gap to
    threshold on — drop the hit and let the knowledge estimator handle the
    food properly.

    Kept: hits the user named EXACTLY. There the food is certainly right and
    only the count is unmappable, which `_apply_100g_rule` resolves with the
    entry's own `qty_default`.
    """
    # Priority, highest first:
    #
    #   1. A hit the user named EXACTLY (name or alias) is the food they meant.
    #      This has to outrank the unit: the MCP is a substring matcher, so
    #      "300g salmon traybake" returns both "salmon traybake" (per serving)
    #      and plain "salmon" (per 100g). The unit alone would pick salmon and
    #      log a traybake as fish.
    #   2. When the name cannot discriminate -- several hits matched exactly, or
    #      scores sit inside the ambiguity margin -- the user's UNIT decides.
    #      Personal "buckwheat" is per-100g and popular "buckwheat" is per-cup;
    #      "160g" means the first, "1 cup" the second.
    #   3. Whatever survives must still be able to carry the quantity.
    exact = [h for h in hits if _names_match_exactly(entry.name, h)]
    pool = exact if exact else hits

    if len(pool) > 1:
        compatible = [
            h for h in pool if _units_compatible(entry.unit, h.get("unit"), entry.qty)
        ]
        if compatible and len(compatible) < len(pool):
            log.info(
                "%r: %d of %d equally-named hit(s) match unit %r — taking those",
                entry.name, len(compatible), len(pool), entry.unit or "-",
            )
            pool = compatible

    kept = []
    for hit in pool:
        if _units_compatible(entry.unit, hit.get("unit"), entry.qty):
            kept.append(hit)
        elif _names_match_exactly(entry.name, hit) and not _is_weight(entry.unit):
            # Rescue only when the quantity counts something the cache cannot
            # measure ("3 eggs" against a per-SERVING omelette). A WEIGHT is
            # exact and must not be traded for qty_default -- that logged
            # "200g cucumber" as half a cucumber. Dropping sends it to the
            # estimator, which honours the grams.
            kept.append(hit)
        else:
            log.info(
                "dropping cache hit %r for %r: unit %r cannot hold %s %s",
                hit.get("name"), entry.name, hit.get("unit"),
                entry.qty, entry.unit or "-",
            )
    return kept


def _names_match_exactly(query: str, hit: dict[str, Any]) -> bool:
    """Does the query name the cache entry outright (by name or alias)?

    Used to rescue a good hit whose unit doesn't line up. If the user names
    the entry exactly, we trust the FOOD but not their count in a foreign
    unit, and fall back to the entry's own `qty_default`.
    """
    q = (query or "").strip().lower()
    if not q:
        return False
    candidates = [hit.get("name") or ""] + list(hit.get("aliases") or [])
    return any(q == str(c).strip().lower() for c in candidates)


async def _cache_override_for_dish(
    client: FoodCacheClient,
    dish: MealDish,
) -> tuple[dict[str, Any], ExtractedEntry] | None:
    """Prefer the user's own cached macros over a vision estimate.

    Vision is asked to itemise a plated meal, but it is handed the user's full
    message — so it will happily invent a dish for a food that is named in the
    text and absent from the photo ("cappuccino", confidence 0.4, note: "not
    visible in photo but mentioned by user"). That guess then overrode a
    calibrated cache entry.

    The cache is the better data whenever it applies: it is the user's own
    measured food, at 0.95 confidence, versus a ~0.4 visual estimate.

    Returns (cache_hit, synthetic_entry) to log via the cache path, or None to
    keep the vision estimate. Deliberately conservative — it never asks a
    clarification question (a photo turn should not stall on one) and never
    falls back to fuzzy matching (the name came from a model, not a typing
    user, so a near-miss is more likely a different food than a typo).
    """
    hits = await client.lookup_food(dish.name)
    if not hits:
        return None
    top, question = _disambiguate_cache_hits(dish.name, hits)
    if question is not None:
        log.info(
            "dish %r matches several cache entries; keeping the photo estimate",
            dish.name,
        )
        return None
    if not _units_compatible(dish.unit, top.get("unit")):
        log.info(
            "dish %r cache hit %r has incompatible unit (%s vs %s); "
            "keeping the photo estimate",
            dish.name, top.get("name"), dish.unit, top.get("unit"),
        )
        return None
    entry = ExtractedEntry(name=dish.name, qty=dish.qty, unit=dish.unit)
    return top, entry


# No food exceeds ~9 kcal per gram — that is pure fat (9), and nothing beats
# it. Anything above this claiming to be "per gram" is really a per-100g
# figure wearing the wrong unit.
_MAX_KCAL_PER_GRAM = 9.5
# Smallest weight a photographed DISH plausibly has. Below this, a "g" unit
# paired with per-100g rates is a mislabelled portion count, not a weight:
# "1 g of jungle curry" divided down to 4 kcal.
_MIN_PLAUSIBLE_DISH_GRAMS = 20


def _normalise_dish_weight(dish: MealDish) -> tuple[float, str, dict[str, float]]:
    """Put a vision dish on the house per-100g basis before logging.

    `_log_meal_dish` used to pass the model's `qty`/`unit`/`*_per_unit`
    straight through — the one path in the system that let the LLM do its own
    per-unit arithmetic. It got it wrong in both directions:

        jasmine rice  qty=100 unit="g"  kcal_per_unit=195  -> 19,500 kcal
        kipper        qty=200 unit="g"  kcal_per_unit=1.22 -> half the truth

    The physical bound above tells the two apart without trusting the model:
    195 kcal/g is impossible, so those are per-100g rates; 1.22 kcal/g is
    plausible, so those really are per-gram. Either way we end on unit="100g",
    which is what the cache and label paths already write.
    """
    qty = float(dish.qty or 1)
    unit = (dish.unit or "serving").strip().lower() or "serving"
    per_unit = {
        "kcal": float(dish.kcal_per_unit),
        "protein": float(dish.protein_per_unit),
        "fat": float(dish.fat_per_unit),
        "carbs": float(dish.carbs_per_unit),
    }
    if unit not in ("g", "gram", "grams", "ml", "millilitre", "millilitres"):
        return qty, unit or "serving", per_unit

    basis = "100ml" if unit.startswith("ml") or unit.startswith("milli") else "100g"
    if per_unit["kcal"] > _MAX_KCAL_PER_GRAM:
        # Rates are already per-100. The quantity may be a real gram amount
        # ("100 g" of rice) or a portion count that got the gram label by
        # mistake ("1 g" of jungle curry, which nobody eats) — the same
        # mislabelling, showing up in the other field.
        log.info(
            "dish %r: %.1f kcal per %r is above the physical maximum — "
            "treating the rates as per-%s",
            dish.name, per_unit["kcal"], unit, basis,
        )
        if qty >= _MIN_PLAUSIBLE_DISH_GRAMS:
            return qty / 100.0, basis, per_unit
        return qty, basis, per_unit
    # Genuinely per-gram: restate on the per-100 basis so the row matches
    # every other path.
    return qty / 100.0, basis, {k: v * 100.0 for k, v in per_unit.items()}


def _is_weight(unit: str | None) -> bool:
    return (unit or "").strip().lower() in _WEIGHT_UNITS


def _label_owners(
    entries: list[ExtractedEntry],
    label_slots: list[int],
    extracts: list["PhotoExtract"],
) -> tuple[dict[int, int] | None, list[int]]:
    """Decide which entry each LABEL photo belongs to.

    Photos used to be consumed positionally — the first entry to reach the
    photo branch took photos[0], whatever it depicted. On a real message:

        "Lunch: Spanish eggs, 150g of pomegranate juice, small orange,
         30g of Apricot yogurt (label attached)"        + one yogurt label

    "Spanish eggs" (qty=null) took the photo, so the yogurt's label was
    logged against the eggs' entry: 1 x 100g instead of 30g, the eggs never
    logged at all, and the yogurt logged again as an estimate.

    Three rules, in order:

    1. THE USER'S MARKER. "(label attached)" is an explicit statement of
       ownership, and the extractor records its ORDER as `photo_index`.
       The extractor is text-only — it never sees the images — so this says
       "the Nth thing I marked" and nothing about what is in the picture.
       Rule 3 checks it.

    2. WEIGHT QUANTITY. A label is per-100g, so the entry must carry a
       weight for there to be anything to scale. "Spanish eggs" with
       qty=null cannot own one; that alone rules out the failure above.
       Used only when the user gave no marker.

    3. CROSS-CHECK against what vision actually read. Compare the assignment
       to every reordering of the same photos, scoring each pairing on
       `label_name`, and take the best. This is RELATIVE on purpose: the
       user writes "super fancy cheese" and the label says "Manchego", so no
       absolute threshold survives contact with real wording. The correct
       pairing only has to beat the incorrect one, and a single recognisable
       name settles the whole set. Costs nothing — vision already returned
       these names.

    Anything still ambiguous asks. Guessing is what caused the bug.

    Returns (mapping entry_idx -> photo_idx, candidate entry indices).
    A None mapping means "ask the user"; the candidate list names the
    entries worth asking about.
    """
    if not label_slots:
        return {}, []

    weighted = [i for i, e in enumerate(entries) if _is_weight(e.unit) and e.qty is not None]

    # --- 1. the user's marker -------------------------------------------
    marked = sorted(
        (i for i, e in enumerate(entries) if e.photo_index is not None),
        key=lambda i: entries[i].photo_index,
    )
    if marked:
        if len(marked) != len(label_slots):
            log.info(
                "%d entries marked with a photo but %d label photo(s) attached",
                len(marked), len(label_slots),
            )
            return None, marked
        owners = dict(zip(marked, label_slots))
        return _verify_against_labels(entries, owners, extracts), marked

    # --- 2. deterministic fallbacks --------------------------------------
    if len(label_slots) == 1:
        if len(entries) == 1:
            return {0: label_slots[0]}, [0]
        if len(weighted) == 1:
            return {weighted[0]: label_slots[0]}, weighted

    if len(weighted) == len(label_slots) and weighted:
        owners = dict(zip(weighted, label_slots))
        return _verify_against_labels(entries, owners, extracts), weighted

    return None, weighted


def _verify_against_labels(
    entries: list[ExtractedEntry],
    owners: dict[int, int],
    extracts: list["PhotoExtract"],
) -> dict[int, int]:
    """Reorder `owners` if another pairing matches the read label names better.

    Only meaningful for 2+ photos: with one photo there is nothing to swap.
    `label_name` is null on roughly a third of real photos, so a pairing
    scores 0 for those and the comparison quietly falls back to the order
    the user implied.
    """
    from itertools import permutations

    if len(owners) < 2 or len(owners) > 4:      # 4! = 24, still trivial
        return owners

    entry_idxs = list(owners.keys())
    slots = list(owners.values())

    def _name_of(slot: int) -> str:
        extract = extracts[slot]
        label = getattr(extract, "label", None)
        return ((getattr(label, "label_name", None) or "")).strip().lower()

    def _score(pairing: tuple[int, ...]) -> int:
        total = 0
        for entry_idx, slot in zip(entry_idxs, pairing):
            name = _name_of(slot)
            if name:
                total += int(fuzz.WRatio(entries[entry_idx].name.lower(), name))
        return total

    as_given = _score(tuple(slots))
    best = max(permutations(slots), key=_score)
    best_score = _score(best)

    # Only reorder on a clear win — OCR noise should not shuffle photos.
    if tuple(best) != tuple(slots) and best_score - as_given >= 15:
        log.info(
            "reordering label photos: read names match %s better than %s "
            "(%d vs %d)", best, slots, best_score, as_given,
        )
        return dict(zip(entry_idxs, best))
    return owners


async def _log_meal_dish(
    client: FoodCacheClient,
    dish: MealDish,
    when: str,
) -> LoggedRow:
    """Log SHAPE D: one dish estimated from a photo of a plated meal."""
    qty, unit, per_unit = _normalise_dish_weight(dish)
    response = await client.log_food(
        datetime=when,
        food=dish.name,
        qty=qty,
        unit=unit,
        kcal_per_unit=per_unit["kcal"],
        protein_per_unit=per_unit["protein"],
        fat_per_unit=per_unit["fat"],
        carbs_per_unit=per_unit["carbs"],
        source="photo_estimate",
        confidence=float(dish.confidence),
    )
    row_data = response.get("entry", {})
    return LoggedRow(
        food=row_data.get("food", dish.name),
        qty=qty,
        unit=unit,
        kcal_total=float(row_data.get("kcal_total", qty * float(dish.kcal_per_unit))),
        protein_total=float(row_data.get("protein_total", qty * float(dish.protein_per_unit))),
        fat_total=float(row_data.get("fat_total", qty * float(dish.fat_per_unit))),
        carbs_total=float(row_data.get("carbs_total", qty * float(dish.carbs_per_unit))),
        source="photo_estimate",
        today_totals=response.get("today", {}),
        estimated=True,
    )


async def _log_knowledge_estimate(
    client: FoodCacheClient,
    knowledge_model: BaseLlm,
    entry: ExtractedEntry,
    when: str,
) -> tuple[LoggedRow | None, str | None]:
    """N6: estimate macros from world knowledge for a cache-missed food.

    Returns (row, None) on success, or (None, refusal_reason) when the model
    declines to guess (branded / homemade / unrecognised food).
    """
    est = await estimate_macros(knowledge_model, entry.name)
    if est.refused:
        return None, est.refusal_reason or "macros vary too much to estimate"

    # Honour the CRITICAL 100g rule. This branch used to test `est_unit ==
    # "100g"` exactly, so an estimate returned per **100ml** — which is what
    # the model gives for a drink — skipped the conversion entirely and
    # multiplied by the raw quantity: "150g tomato juice" logged 150 x 18 =
    # 2700 kcal. Any per-100 basis has to convert, not just grams.
    user_unit = (entry.unit or "").strip().lower()
    est_unit = (est.unit or "serving").strip().lower()
    qty_in = float(entry.qty) if entry.qty is not None else 1.0

    if user_unit in _WEIGHT_UNITS and est_unit in _PER_100_UNITS:
        qty, unit = qty_in / 100.0, est_unit
    elif _units_compatible(user_unit, est_unit, qty_in):
        qty, unit = qty_in, est_unit
    else:
        # The user's count measures something the estimate doesn't ("1
        # grapefruit" against per-100g). Log a single unit of the estimate's
        # own basis rather than scaling by a number that means nothing here.
        log.info(
            "knowledge estimate for %r: cannot map %s %s onto %s — logging 1 %s",
            entry.name, qty_in, user_unit or "-", est_unit, est_unit,
        )
        qty, unit = 1.0, est_unit

    response = await client.log_food(
        datetime=when,
        food=entry.name,
        qty=qty,
        unit=unit,
        kcal_per_unit=float(est.kcal_per_unit),
        protein_per_unit=float(est.protein_per_unit),
        fat_per_unit=float(est.fat_per_unit),
        carbs_per_unit=float(est.carbs_per_unit),
        source="text_estimate",
        confidence=float(est.confidence),
    )
    row_data = response.get("entry", {})
    row = LoggedRow(
        food=row_data.get("food", entry.name),
        qty=qty,
        unit=unit,
        kcal_total=float(row_data.get("kcal_total", qty * float(est.kcal_per_unit))),
        protein_total=float(row_data.get("protein_total", qty * float(est.protein_per_unit))),
        fat_total=float(row_data.get("fat_total", qty * float(est.fat_per_unit))),
        carbs_total=float(row_data.get("carbs_total", qty * float(est.carbs_per_unit))),
        source="text_estimate",
        today_totals=response.get("today", {}),
        # Knowledge estimates must never seed the personal cache — an LLM
        # guess must not become a "verified" entry the user trusts later.
        save_status="ineligible" if entry.save_to_cache else None,
        estimated=True,
    )
    return row, None


async def _promote_recent(
    client: FoodCacheClient,
    recent: RecentEntry,
) -> SavedRecentEntry:
    """Push a recent-entry snapshot into personal-foods via add_personal_food."""
    resp = await client.add_personal_food(
        name=recent.name,
        unit=recent.unit,
        kcal_per_unit=recent.kcal_per_unit,
        protein_per_unit=recent.protein_per_unit,
        fat_per_unit=recent.fat_per_unit,
        carbs_per_unit=recent.carbs_per_unit,
        qty_default=recent.qty,
    )
    status = "saved"
    if isinstance(resp, dict) and resp.get("error"):
        log.info("add_personal_food rejected recent promotion: %s", resp["error"])
        status = "duplicate"
    return SavedRecentEntry(
        name=recent.name, unit=recent.unit,
        kcal_per_unit=recent.kcal_per_unit,
        protein_per_unit=recent.protein_per_unit,
        fat_per_unit=recent.fat_per_unit,
        carbs_per_unit=recent.carbs_per_unit,
        status=status,
    )


async def orchestrate(
    msg: ExtractedMessage,
    client: FoodCacheClient,
    *,
    photos: list[Path] | None = None,
    vision_model: BaseLlm | None = None,
    knowledge_model: BaseLlm | None = None,
    context: ChatContext | None = None,
    message_text: str = "",
    resuming_clarification: bool = False,
) -> OrchestratorResult:
    """Per-entry dispatch on SHAPE (A cache-lookup, B typed macros, C label photo).

    Photo assignment: naive one-to-one in message order. First entry that
    lacks user-typed macros consumes photos[0], second consumes photos[1],
    etc. Extra photos beyond entry count are ignored (logged at INFO).
    N5 keeps this simple; multi-photo semantic matching lands later.
    """
    result = OrchestratorResult()
    if not msg.is_food_related or not msg.entries:
        return result

    photos = photos or []

    # Classify every photo ONCE, before the loop, so ownership can be decided
    # up front. Same number of vision calls as consuming them inside the loop
    # — the cursor meant each photo was analysed once either way — but the
    # results are now available before any entry is processed.
    photo_extracts: list[PhotoExtract] = []
    label_slots: list[int] = []
    owners: dict[int, int] = {}
    if vision_model is not None and photos and _any_entry_can_use_a_photo(msg.entries):
        for photo in photos:
            photo_extracts.append(
                await analyse_photo(vision_model, photo, hint_text=message_text or "")
            )
        label_slots = [i for i, e in enumerate(photo_extracts) if e.kind == "label"]
        meal_slots = [i for i, e in enumerate(photo_extracts) if e.kind == "meal"]

        if meal_slots:
            # A meal photo describes the whole message, so attribution does
            # not arise — log its dishes and stop, as before.
            return await _log_meal_photo(
                client, photo_extracts[meal_slots[0]], msg, result, context,
                _resolve_datetime(msg.entries[0].datetime_hint),
            )

        resolved, candidates = _label_owners(msg.entries, label_slots, photo_extracts)
        if resolved is None:
            names = [msg.entries[i].name for i in candidates] or ["that photo"]
            result.pending_clarification = (
                "Which item is the photo for — " + " or ".join(names) + "?"
                if len(names) > 1
                else f"I couldn't tell which item {names[0]} belongs to — which is it?"
            )
            log.info("label attribution ambiguous between %s", names)
        else:
            owners = resolved

    for entry_index, entry in enumerate(msg.entries):
        when = _resolve_datetime(entry.datetime_hint)

        # ---- SHAPE 6.4: reference to a recently-logged meal (N3) ----
        # Highest priority — this is a metadata request, no cache/photo work.
        # No log_food fired; only add_personal_food.
        if entry.reference_recent:
            recent_list = list(context.recent_entries) if context else []
            match = _find_recent(entry.name, recent_list)
            if match is None:
                log.info(
                    "reference_recent %r not found in recent ring (%d entries)",
                    entry.name, len(recent_list),
                )
                result.saved_recent.append(SavedRecentEntry(
                    name=entry.name, unit="",
                    kcal_per_unit=0, protein_per_unit=0,
                    fat_per_unit=0, carbs_per_unit=0,
                    status="not_found",
                ))
                continue
            saved = await _promote_recent(client, match)
            result.saved_recent.append(saved)
            log.info(
                "shape 6.4: promoted recent %r -> personal_foods (status=%s)",
                match.name, saved.status,
            )
            continue

        # ---- SHAPE B: user typed macros (takes precedence over any photo) ----
        if _is_shape_b(entry):
            row = await _log_shape_b(client, entry, when)
            if entry.save_to_cache:
                row.save_status = await _try_save_to_cache(client, entry, row)
            result.logged.append(row)
            log.info(
                "logged (text_estimate) %r kcal=%s save=%s",
                row.food, row.kcal_total, row.save_status,
            )
            if context is not None:
                context.recent_entries.append(_snapshot(
                    name=row.food, qty=row.qty, unit=row.unit,
                    per_unit={
                        "kcal": row.kcal_total, "protein": row.protein_total,
                        "fat": row.fat_total, "carbs": row.carbs_total,
                    },
                    source=row.source,
                ))
            continue

        # ---- SHAPE C: a label photo THIS entry owns ----
        # Ownership is decided before the loop by _label_owners. An entry that
        # owns no photo falls straight through to the cache path below —
        # previously it consumed whatever photo was next and then `continue`d,
        # so it never logged itself at all.
        slot = owners.get(entry_index)
        if slot is not None:
            extract = photo_extracts[slot]
            if extract.kind == "unclear":
                log.info("photo unclear for %r: %s", entry.name, extract.unclear_reason)
                result.unknown.append(UnknownEntry(
                    name=entry.name,
                    reason=extract.unclear_reason or "couldn't read that photo",
                ))
                continue

            row = await _log_shape_c_label(client, extract.label or LabelExtract(), entry, when)
            if entry.save_to_cache:
                row.save_status = "ineligible"  # photo_label save is out-of-scope for N5
            result.logged.append(row)
            log.info(
                "logged (photo_label) %r qty=%s unit=%s kcal_total=%.1f",
                row.food, row.qty, row.unit, row.kcal_total,
            )
            if context is not None:
                # For label rows the per-unit values ARE per-100g (unit=100g).
                context.recent_entries.append(_snapshot(
                    name=row.food, qty=row.qty, unit=row.unit,
                    per_unit={
                        "kcal": row.kcal_total / row.qty if row.qty else row.kcal_total,
                        "protein": row.protein_total / row.qty if row.qty else row.protein_total,
                        "fat": row.fat_total / row.qty if row.qty else row.fat_total,
                        "carbs": row.carbs_total / row.qty if row.qty else row.carbs_total,
                    },
                    source=row.source,
                ))
            continue

        # ---- SHAPE A: cache lookup ----
        hits = _usable_hits(entry, await client.lookup_food(entry.name))
        via_fuzzy = False
        if not hits:
            # N4.5: fuzzy fallback before giving up. Handles typos / formatting
            # variants (dash-vs-space, missing/extra letters) that the MCP's
            # substring-only matcher misses.
            fuzzy_hits = _usable_hits(entry, client.fuzzy_lookup(entry.name))
            if fuzzy_hits:
                log.info(
                    "fuzzy fallback for %r -> %d hit(s), top=%r",
                    entry.name, len(fuzzy_hits), fuzzy_hits[0]["name"],
                )
                hits = fuzzy_hits
                via_fuzzy = True
            elif knowledge_model is not None:
                # N6: last resort — ask the model for typical macros. It may
                # refuse for branded / homemade / unrecognised foods.
                row, refusal = await _log_knowledge_estimate(
                    client, knowledge_model, entry, when
                )
                if row is not None:
                    result.logged.append(row)
                    log.info(
                        "logged (knowledge estimate) %r qty=%s unit=%s kcal_total=%.1f",
                        row.food, row.qty, row.unit, row.kcal_total,
                    )
                    if context is not None:
                        context.recent_entries.append(_snapshot(
                            name=row.food, qty=row.qty, unit=row.unit,
                            per_unit={
                                "kcal": row.kcal_total / row.qty if row.qty else row.kcal_total,
                                "protein": row.protein_total / row.qty if row.qty else row.protein_total,
                                "fat": row.fat_total / row.qty if row.qty else row.fat_total,
                                "carbs": row.carbs_total / row.qty if row.qty else row.carbs_total,
                            },
                            source=row.source,
                        ))
                    continue
                log.info("knowledge estimator refused %r: %s", entry.name, refusal)
                result.unknown.append(UnknownEntry(name=entry.name, reason=refusal or "unknown"))
                continue
            else:
                log.info("cache miss for %r and no knowledge model wired", entry.name)
                result.unknown.append(UnknownEntry(
                    name=entry.name,
                    reason="not in personal or popular cache (no fuzzy match either)",
                ))
                continue

        # N4: disambiguate multi-hit results via rapidfuzz thresholds.
        top, question = _disambiguate_cache_hits(
            entry.name, hits, force=resuming_clarification
        )
        if question is not None:
            log.info("ambiguous cache result for %r: %s", entry.name, question)
            # Collect and CONTINUE — do not abandon the rest of the message.
            # An earlier version broke out of the loop here, which silently
            # dropped every remaining item in a multi-item message.
            result.unresolved.append(entry)
            if result.pending_clarification is None:
                result.pending_clarification = question
            continue

        # Fuzzy matches: lower confidence (0.85) to signal "matched despite
        # a typo/formatting difference" — user can spot-check the row.
        confidence = 0.85 if via_fuzzy else 0.95
        row = await _log_from_cache_hit(client, entry, top, when, confidence=confidence)
        if entry.save_to_cache:
            row.save_status = "ineligible"  # already in cache
        result.logged.append(row)
        log.info(
            "logged (cache_lookup) %r qty=%s unit=%s kcal_total=%.1f",
            row.food, row.qty, row.unit, row.kcal_total,
        )
        if context is not None:
            context.recent_entries.append(_snapshot(
                name=row.food, qty=row.qty, unit=row.unit,
                per_unit={
                    "kcal": float(top["kcal_per_unit"]),
                    "protein": float(top["protein_per_unit"]),
                    "fat": float(top["fat_per_unit"]),
                    "carbs": float(top["carbs_per_unit"]),
                },
                source=row.source,
            ))

    unclaimed = [i for i in label_slots if i not in owners.values()]
    if unclaimed:
        log.info("%d label photo(s) matched no entry", len(unclaimed))

    return result


def _any_entry_can_use_a_photo(entries: list[ExtractedEntry]) -> bool:
    """Would any entry actually reach the photo path?

    Guards the vision call: a message whose entries all carry typed macros
    or are "save the X I had" requests never consults a photo, and analysing
    one anyway would spend an LLM call for nothing.
    """
    return any(
        not e.reference_recent and not _is_shape_b(e) for e in entries
    )


async def _log_meal_photo(
    client: FoodCacheClient,
    extract: "PhotoExtract",
    msg: ExtractedMessage,
    result: OrchestratorResult,
    context: ChatContext | None,
    when: str,
) -> OrchestratorResult:
    """SHAPE D: one photo of a plated meal accounts for the whole message.

    Attribution does not arise here — the model itemises the plate, so the
    text entries are the same dishes by another name. Lifted out of the entry
    loop when ownership moved in front of it.
    """
    if not extract.dishes:
        result.unknown.append(UnknownEntry(
            name=msg.entries[0].name if msg.entries else "that photo",
            reason="couldn't identify dishes in that photo",
        ))
        return result

    for dish in extract.dishes:
        # Cache beats a visual estimate for a food the user has
        # already measured — including foods vision invented from
        # the message text but could not actually see.
        override = await _cache_override_for_dish(client, dish)
        if override is not None:
            hit, synth = override
            dish_row = await _log_from_cache_hit(client, synth, hit, when)
            per_unit = {
                "kcal": float(hit["kcal_per_unit"]),
                "protein": float(hit["protein_per_unit"]),
                "fat": float(hit["fat_per_unit"]),
                "carbs": float(hit["carbs_per_unit"]),
            }
            log.info(
                "dish %r resolved from cache (%r) instead of the photo "
                "estimate: %.0f kcal/unit vs %.0f",
                dish.name, hit.get("name"),
                per_unit["kcal"], dish.kcal_per_unit,
            )
        else:
            dish_row = await _log_meal_dish(client, dish, when)
            per_unit = {
                "kcal": dish.kcal_per_unit,
                "protein": dish.protein_per_unit,
                "fat": dish.fat_per_unit,
                "carbs": dish.carbs_per_unit,
            }
        result.logged.append(dish_row)
        log.info(
            "logged (%s) %r qty=%s unit=%s kcal_total=%.1f",
            dish_row.source,
            dish_row.food, dish_row.qty, dish_row.unit, dish_row.kcal_total,
        )
        if context is not None:
            context.recent_entries.append(_snapshot(
                name=dish_row.food, qty=dish_row.qty, unit=dish_row.unit,
                per_unit=per_unit,
                source=dish_row.source,
            ))
    return result
