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


# --- Unit normalization (deterministic; owned by MCP, not the model) ---


def _parse_weight_unit(unit: str) -> tuple[float, str] | None:
    """Parse a weight/volume unit into (amount_in_base, base).

    '100g' -> (100, 'g'); 'g' -> (1, 'g'); 'kg' -> (1000, 'g')
    '100ml' -> (100, 'ml'); 'ml' -> (1, 'ml'); 'l' -> (1000, 'ml')
    Named units (cup, serving, egg, cortado, ...) -> None.
    """
    u = (unit or "").strip().lower()
    m = re.fullmatch(r"(\d*\.?\d*)\s*(kg|g|ml|l)", u)
    if not m:
        return None
    num = float(m.group(1)) if m.group(1) else 1.0
    base = m.group(2)
    if base == "kg":
        return (num * 1000, "g")
    if base == "l":
        return (num * 1000, "ml")
    return (num, base)


def resolve_log_units(
    food: str,
    qty: float,
    unit: str,
    kcal_per_unit: float,
    protein_per_unit: float,
    fat_per_unit: float,
    carbs_per_unit: float,
    source: str,
    personal_path: str,
    popular_path: str,
) -> dict:
    """Normalize qty/unit and per-unit rates before logging.

    The model only reliably reports (food, raw_qty, raw_unit). Unit conversion
    and per-unit nutrition are deterministic and owned here, not by the model.

    - source == 'cache_lookup': the cache is authoritative for known foods.
      Re-fetch its canonical rates and convert the user's qty into the cache
      entry's unit basis (e.g. 40g + cache '100g' -> qty 0.4, unit '100g').
    - other sources (text_estimate / photo_*) or cache miss: apply a physically
      grounded heuristic — a food cannot exceed 1g of macros or ~9 kcal per gram,
      so unit='g' with kcal>9 or macro-sum>1 is unambiguously per-100g data.

    Returns possibly-corrected values plus a 'note' describing any change.
    """
    out = {
        "qty": qty,
        "unit": unit,
        "kcal_per_unit": kcal_per_unit,
        "protein_per_unit": protein_per_unit,
        "fat_per_unit": fat_per_unit,
        "carbs_per_unit": carbs_per_unit,
        "note": "",
    }

    if source == "cache_lookup":
        matches = search_foods(food, personal_path, popular_path)
        if matches:
            entry = matches[0]
            cache_unit = str(entry.get("unit", ""))
            in_parsed = _parse_weight_unit(unit)
            cache_parsed = _parse_weight_unit(cache_unit)

            def _use_cache_rates():
                out["kcal_per_unit"] = entry.get("kcal_per_unit", kcal_per_unit)
                out["protein_per_unit"] = entry.get("protein_per_unit", protein_per_unit)
                out["fat_per_unit"] = entry.get("fat_per_unit", fat_per_unit)
                out["carbs_per_unit"] = entry.get("carbs_per_unit", carbs_per_unit)

            if in_parsed and cache_parsed and in_parsed[1] == cache_parsed[1]:
                # Same weight/volume base: convert qty to the cache's unit basis.
                total_base = qty * in_parsed[0]
                out["qty"] = round(total_base / cache_parsed[0], 4)
                out["unit"] = cache_unit
                _use_cache_rates()
                if out["unit"] != unit or out["qty"] != qty:
                    out["note"] = f"cache-normalized {qty}{unit} -> {out['qty']}x{cache_unit}"
                return out
            if unit.strip().lower() == cache_unit.strip().lower():
                # Same named unit (serving/cortado/egg/...): trust qty, cache rates.
                _use_cache_rates()
                return out
            # Incompatible units (e.g. input 'g' vs cache 'serving'): don't force
            # cache rates; fall through to the heuristic below.
            out["note"] = f"unit mismatch (input {unit!r} vs cache {cache_unit!r}); used heuristic"
        # no cache match -> fall through to heuristic

    # Heuristic (estimate/photo or cache miss): detect per-100g mislabeled as per-g.
    in_parsed = _parse_weight_unit(unit)
    if in_parsed and in_parsed[0] == 1.0:  # raw 'g' or 'ml'
        base = in_parsed[1]
        macro_sum = (protein_per_unit or 0) + (fat_per_unit or 0) + (carbs_per_unit or 0)
        if (kcal_per_unit or 0) > 9 or macro_sum > 1:
            out["qty"] = round(qty / 100, 4)
            out["unit"] = f"100{base}"
            prefix = out["note"] + "; " if out["note"] else ""
            out["note"] = f"{prefix}heuristic: per-100{base} values with unit {base!r} -> {out['qty']}x100{base}"
    return out


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


def normalize_food_name(food: str) -> str:
    """Normalise a food name for the monthly log to SENTENCE case.

    The log's established convention is sentence case (~1900 capitalised rows
    vs ~50 lowercase). Agents supply names in whatever case they happen to
    have: personal-foods.yaml stores keys lowercase, so a cache hit yields
    "espresso", while an LLM estimate yields "Espresso" or "Boiled Eggs".
    Left unnormalised, the same food splits into several names and anything
    grouping by name (dashboards, averages) double-counts it.

    Capitalise the FIRST character only and leave the rest untouched. Title-
    casing would mangle acronyms and brands ("MCT C8 oil" -> "Mct C8 Oil",
    "Pret nicoise salad" -> "Pret Nicoise Salad").

        "espresso"          -> "Espresso"
        "small cappuccino"  -> "Small cappuccino"
        "MCT C8 oil"        -> "MCT C8 oil"   (unchanged)

    Applied here, in the single write path, so every client (Veda, clerk,
    anything future) gets the same convention without having to remember.
    """
    food = (food or "").strip()
    if not food:
        return food
    return food[0].upper() + food[1:]


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

    # Single place the log's naming convention is enforced — see
    # normalize_food_name. Clients may pass any casing.
    food = normalize_food_name(food)

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
