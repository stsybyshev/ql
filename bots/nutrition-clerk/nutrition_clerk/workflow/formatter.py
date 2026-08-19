"""Formatter — deterministic Python that builds the user-facing reply.

Matches the compact reply format the existing tests + prompts have baked in
so behaviour reads the same to the user.
"""
from __future__ import annotations

from nutrition_clerk.workflow.orchestrator import OrchestratorResult, SavedRecentEntry


def format_reply(result: OrchestratorResult) -> str:
    """Build the reply string from an orchestrator result.

    Single-entry:
        Logged: 1 apple
        Today: 1734 kcal · 68P · 108F · 142C

    Multi-entry:
        Logged:
        · 1 apple (95 kcal · 0.5P · 0.3F · 25.1C)
        · 50g cashews (275 kcal · 9P · 22F · 15C)
        Today: 2159 kcal · 78P · 135F · 158C

    Unknown items are appended as an "I don't have <X> yet" note.
    """
    logged, unknown = result.logged, result.unknown
    lines: list[str] = []

    if len(logged) == 1:
        row = logged[0]
        # For estimate-based rows (text_estimate, photo_label, photo_estimate),
        # include macros inline so the user can spot-check what got logged.
        if row.source in ("text_estimate", "photo_label", "photo_estimate"):
            prefix = "Logged from label" if row.source == "photo_label" else "Logged"
            qty_prefix = ""
            if row.source == "photo_label" and row.unit == "100g":
                # Show "200g" instead of "2 × 100g" in the user-facing label case
                qty_prefix = f"{_fmt(row.qty * 100)}g "
            lines.append(
                f"{prefix}: {qty_prefix}{row.food.lower()} "
                f"({int(round(row.kcal_total))} kcal · "
                f"{_fmt(row.protein_total)}P · "
                f"{_fmt(row.fat_total)}F · "
                f"{_fmt(row.carbs_total)}C"
                + (", estimate" if row.estimated else "")
                + ")"
                + _save_note(row.save_status)
            )
        else:
            lines.append(
                f"Logged: {_qty_str(row.qty, row.unit, row.food)} {row.food.lower()}"
                + _save_note(row.save_status)
            )
    elif len(logged) > 1:
        lines.append("Logged:")
        for row in logged:
            lines.append(
                f"· {_qty_str(row.qty, row.unit, row.food)} {row.food.lower()} "
                f"({int(round(row.kcal_total))} kcal · "
                f"{_fmt(row.protein_total)}P · "
                f"{_fmt(row.fat_total)}F · "
                f"{_fmt(row.carbs_total)}C"
                + (", estimate" if row.estimated else "")
                + ")"
                + _save_note(row.save_status)
            )

    if unknown:
        if not logged:
            lines.append("I couldn't resolve any items in your message:")
        else:
            lines.append("")
            lines.append("Couldn't resolve:")
        for u in unknown:
            lines.append(
                f"· {u.name} — not in your cache yet. "
                "Send it with macros (`<food> 200 kcal 10P 5F 20C`) or a label photo."
            )

    if logged:
        totals = logged[-1].today_totals or {}
        lines.append(
            "Today: "
            f"{int(round(totals.get('kcal', 0)))} kcal · "
            f"{_fmt(totals.get('protein', 0))}P · "
            f"{_fmt(totals.get('fat', 0))}F · "
            f"{_fmt(totals.get('carbs', 0))}C"
        )

    # Items still awaiting disambiguation. Listed explicitly so a multi-item
    # message never looks like it silently dropped things.
    if result.unresolved:
        if lines:
            lines.append("")
        names = ", ".join(e.name for e in result.unresolved)
        lines.append(f"Still to sort out: {names}")

    # N3: SHAPE 6.4 promotions — one line per saved-recent entry.
    if result.saved_recent:
        if lines:
            lines.append("")   # blank separator when also logging in same turn
        for s in result.saved_recent:
            lines.append(_render_saved_recent(s))

    return "\n".join(lines) if lines else "(nothing logged)"


def _render_saved_recent(s: "SavedRecentEntry") -> str:
    if s.status == "saved":
        return (
            f'Saved "{s.name}" to your cache '
            f"({int(round(s.kcal_per_unit))} kcal · "
            f"{_fmt(s.protein_per_unit)}P · "
            f"{_fmt(s.fat_per_unit)}F · "
            f"{_fmt(s.carbs_per_unit)}C per {s.unit})."
        )
    if s.status == "duplicate":
        return f'"{s.name}" is already in your cache — no change needed.'
    # not_found
    return (
        f"I don't see \"{s.name}\" in your recent log — please log it "
        "first, then ask me to save it."
    )


def _qty_str(qty: float, unit: str, food: str = "") -> str:
    """'1 apple', '50g', '2.5 × 100g' — natural to read.

    When the unit is a count-noun already implied by the food name
    ("1 banana banana", "2 egg boiled eggs"), the unit is dropped so the
    food name alone carries it: "1 banana", "2 boiled eggs".
    """
    qty_txt = f"{qty:g}"  # 1, 2.5, 0.4 — no trailing .0
    u = unit.lower()
    if u == "100g":
        return f"{qty_txt} × 100g"
    if u in ("g", "ml"):
        return f"{qty_txt}{unit}"
    # Drop redundant count-noun units (singular or plural) already in the name.
    food_l = food.lower()
    if u and (u in food_l or f"{u}s" in food_l):
        return qty_txt
    return f"{qty_txt} {unit}"


def _fmt(value: float) -> str:
    """Compact number: 1 → '1', 1.3 → '1.3', 22.0 → '22'."""
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def _save_note(save_status: str | None) -> str:
    """Inline suffix noting whether the entry was saved to personal cache."""
    if save_status == "saved":
        return "  ·  saved to cache"
    if save_status == "duplicate":
        return "  ·  already in cache"
    return ""
