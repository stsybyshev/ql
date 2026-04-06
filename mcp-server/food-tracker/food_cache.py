"""Food cache operations and food log management.

Wraps personal-foods.yaml, popular-foods.yaml, and monthly YYYY-MM.md log files.
"""

import fcntl
import re
from datetime import datetime
from pathlib import Path

import yaml


# --- Constants ---

_LOG_HEADER_TEMPLATE = """# Food Log — {month_name} {year}

| Datetime         | Food                   | Qty | Unit    | Protein/u | Fat/u | Carbs/u | Kcal/u | Protein | Fat   | Carbs | Kcal  | Source       | Confidence |
|:-----------------|:-----------------------|----:|:--------|----------:|------:|--------:|-------:|--------:|------:|------:|------:|:-------------|:-----------|
"""

_MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def load_yaml(path: str | Path) -> list[dict]:
    """Read a YAML food cache file and return list of food entries."""
    p = Path(path)
    if not p.exists():
        return []
    with open(p) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        return []
    return data


def _matches(query: str, entry: dict) -> bool:
    """Check if query matches entry name or any alias (case-insensitive substring)."""
    q = query.lower().strip()
    if not q:
        return False
    name = entry.get("name", "").lower()
    if q in name or name in q:
        return True
    for alias in entry.get("aliases", []):
        a = str(alias).lower()
        if q in a or a in q:
            return True
    return False


def _entry_to_result(entry: dict, source: str) -> dict:
    """Convert a YAML entry to a clean result dict."""
    return {
        "name": entry.get("name", ""),
        "aliases": entry.get("aliases", []),
        "qty_default": entry.get("qty_default", 1),
        "unit": entry.get("unit", "serving"),
        "kcal_per_unit": entry.get("kcal_per_unit", 0),
        "protein_per_unit": entry.get("protein_per_unit", 0),
        "fat_per_unit": entry.get("fat_per_unit", 0),
        "carbs_per_unit": entry.get("carbs_per_unit", 0),
        "notes": entry.get("notes", ""),
        "source": source,
    }


def search_foods(query: str, personal_path: str, popular_path: str) -> list[dict]:
    """Search both food caches. Personal matches come first.

    Returns all matching entries (usually 1-3).
    """
    results = []

    personal = load_yaml(personal_path)
    for entry in personal:
        if _matches(query, entry):
            results.append(_entry_to_result(entry, "personal"))

    popular = load_yaml(popular_path)
    for entry in popular:
        if _matches(query, entry):
            results.append(_entry_to_result(entry, "seed"))

    return results


def append_personal_food(
    entry: dict,
    path: str,
) -> dict:
    """Append a new food entry to personal-foods.yaml.

    Validates required fields, checks for alias collisions, and appends atomically.
    Returns the saved entry on success, or an error dict.
    """
    required = ["name", "unit", "kcal_per_unit", "protein_per_unit", "fat_per_unit", "carbs_per_unit"]
    missing = [f for f in required if f not in entry or entry[f] is None]
    if missing:
        return {"error": f"Missing required fields: {', '.join(missing)}"}

    new_aliases = [str(a).lower() for a in entry.get("aliases", [])]
    new_name = str(entry["name"]).lower()

    existing = load_yaml(path)
    for ex in existing:
        ex_name = str(ex.get("name", "")).lower()
        ex_aliases = [str(a).lower() for a in ex.get("aliases", [])]
        all_existing = [ex_name] + ex_aliases
        all_new = [new_name] + new_aliases

        for n in all_new:
            if n in all_existing:
                return {"error": f"Alias '{n}' already exists in entry '{ex.get('name')}'"}

    record = {
        "name": entry["name"],
        "aliases": entry.get("aliases", []),
        "qty_default": entry.get("qty_default", 1),
        "unit": entry["unit"],
        "kcal_per_unit": entry["kcal_per_unit"],
        "protein_per_unit": entry["protein_per_unit"],
        "fat_per_unit": entry["fat_per_unit"],
        "carbs_per_unit": entry["carbs_per_unit"],
        "notes": entry.get("notes", ""),
        "source": "learned",
    }

    yaml_block = "\n" + yaml.dump([record], default_flow_style=False, allow_unicode=True)

    p = Path(path)
    with open(p, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(yaml_block)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    return {"status": "ok", "entry": record}


# --- Food log operations ---


def _parse_log_rows(text: str) -> list[dict]:
    """Parse markdown table rows from a monthly log file into dicts."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.split("|")]
        # Skip header/separator rows (cells[0] is empty due to leading |)
        if len(cells) < 15:
            continue
        dt_str = cells[1]
        if not re.match(r"\d{2}-\d{2}-\d{4}", dt_str):
            continue
        try:
            rows.append({
                "datetime": dt_str,
                "food": cells[2],
                "qty": float(cells[3]),
                "unit": cells[4],
                "protein_per_unit": float(cells[5]),
                "fat_per_unit": float(cells[6]),
                "carbs_per_unit": float(cells[7]),
                "kcal_per_unit": float(cells[8]),
                "protein_total": float(cells[9]),
                "fat_total": float(cells[10]),
                "carbs_total": float(cells[11]),
                "kcal_total": float(cells[12]),
                "source": cells[13],
                "confidence": cells[14],
            })
        except (ValueError, IndexError):
            continue
    return rows


def _format_row(
    dt: str, food: str, qty: float, unit: str,
    protein_u: float, fat_u: float, carbs_u: float, kcal_u: float,
    protein_t: float, fat_t: float, carbs_t: float, kcal_t: float,
    source: str, confidence: float,
) -> str:
    """Format a single markdown table row with aligned columns."""
    return (
        f"| {dt:<16} "
        f"| {food:<22} "
        f"| {qty:>3g} "
        f"| {unit:<7} "
        f"| {protein_u:>9.1f} "
        f"| {fat_u:>5.1f} "
        f"| {carbs_u:>7.1f} "
        f"| {kcal_u:>6g} "
        f"| {protein_t:>7.1f} "
        f"| {fat_t:>5.1f} "
        f"| {carbs_t:>5.1f} "
        f"| {kcal_t:>5g} "
        f"| {source:<12} "
        f"| {confidence:<10} |"
    )


def log_food_entry(
    dt_str: str,
    food: str,
    qty: float,
    unit: str,
    protein_per_unit: float,
    fat_per_unit: float,
    carbs_per_unit: float,
    kcal_per_unit: float,
    source: str,
    confidence: float,
    log_dir: str,
) -> dict:
    """Append a food entry to the monthly log and return today's running totals.

    Args:
        dt_str: Datetime string in DD-MM-YYYY HH:MM format.
        food: Food name (e.g. "Scrambled eggs").
        qty: Quantity consumed.
        unit: Serving unit (e.g. "egg", "100g", "serving").
        protein_per_unit: Grams of protein per unit.
        fat_per_unit: Grams of fat per unit.
        carbs_per_unit: Grams of carbs per unit.
        kcal_per_unit: Kilocalories per unit.
        source: One of cache_lookup, text_estimate, photo_estimate, photo_label.
        confidence: Float 0-1.
        log_dir: Directory containing monthly log files.

    Returns:
        Dict with logged entry details and today's running totals.
    """
    # Validate datetime format
    m = re.match(r"(\d{2})-(\d{2})-(\d{4}) (\d{2}:\d{2})", dt_str)
    if not m:
        return {"error": f"Invalid datetime format: '{dt_str}'. Expected DD-MM-YYYY HH:MM"}

    day, month, year = m.group(1), m.group(2), m.group(3)
    date_prefix = f"{day}-{month}-{year}"

    # Determine file path
    file_path = Path(log_dir) / f"{year}-{month}.md"

    # Compute totals
    protein_total = round(qty * protein_per_unit, 1)
    fat_total = round(qty * fat_per_unit, 1)
    carbs_total = round(qty * carbs_per_unit, 1)
    kcal_total = round(qty * kcal_per_unit)

    # Format the row
    row = _format_row(
        dt_str, food, qty, unit,
        protein_per_unit, fat_per_unit, carbs_per_unit, kcal_per_unit,
        protein_total, fat_total, carbs_total, kcal_total,
        source, confidence,
    )

    # Create file if it doesn't exist
    if not file_path.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)
        month_int = int(month)
        header = _LOG_HEADER_TEMPLATE.format(
            month_name=_MONTH_NAMES[month_int],
            year=year,
        )
        file_path.write_text(header)

    # Append row atomically
    with open(file_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(row + "\n")
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

    # Read back today's totals
    text = file_path.read_text()
    all_rows = _parse_log_rows(text)
    today_rows = [r for r in all_rows if r["datetime"].startswith(date_prefix)]

    today_totals = {
        "kcal": round(sum(r["kcal_total"] for r in today_rows)),
        "protein": round(sum(r["protein_total"] for r in today_rows), 1),
        "fat": round(sum(r["fat_total"] for r in today_rows), 1),
        "carbs": round(sum(r["carbs_total"] for r in today_rows), 1),
        "entries": len(today_rows),
    }

    return {
        "status": "ok",
        "entry": {
            "food": food,
            "qty": qty,
            "unit": unit,
            "kcal_total": kcal_total,
            "protein_total": protein_total,
            "fat_total": fat_total,
            "carbs_total": carbs_total,
        },
        "today": today_totals,
    }


def get_daily_totals(date_str: str, log_dir: str) -> dict:
    """Get food log entries and totals for a specific date.

    Args:
        date_str: Date in DD-MM-YYYY format.
        log_dir: Directory containing monthly log files.

    Returns:
        Dict with entries list and summed totals.
    """
    m = re.match(r"(\d{2})-(\d{2})-(\d{4})", date_str)
    if not m:
        return {"error": f"Invalid date format: '{date_str}'. Expected DD-MM-YYYY"}

    day, month, year = m.group(1), m.group(2), m.group(3)
    file_path = Path(log_dir) / f"{year}-{month}.md"

    if not file_path.exists():
        return {
            "date": date_str,
            "entries": [],
            "totals": {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0},
            "count": 0,
        }

    text = file_path.read_text()
    all_rows = _parse_log_rows(text)
    date_prefix = f"{day}-{month}-{year}"
    day_rows = [r for r in all_rows if r["datetime"].startswith(date_prefix)]

    entries = [
        {"food": r["food"], "kcal": r["kcal_total"], "protein": r["protein_total"],
         "fat": r["fat_total"], "carbs": r["carbs_total"]}
        for r in day_rows
    ]

    totals = {
        "kcal": round(sum(r["kcal_total"] for r in day_rows)),
        "protein": round(sum(r["protein_total"] for r in day_rows), 1),
        "fat": round(sum(r["fat_total"] for r in day_rows), 1),
        "carbs": round(sum(r["carbs_total"] for r in day_rows), 1),
    }

    return {
        "date": date_str,
        "entries": entries,
        "totals": totals,
        "count": len(day_rows),
    }
