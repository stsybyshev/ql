"""MCP server for food cache lookup and management.

Wraps personal-foods.yaml and popular-foods.yaml behind two tools:
  - lookup_food: fuzzy search across both caches
  - add_personal_food: append-only write to personal-foods.yaml
"""

import os

from mcp.server.fastmcp import FastMCP

from food_cache import append_personal_food, get_daily_totals, log_food_entry, search_foods

# --- Config from env vars ---
PERSONAL_FOODS_PATH = os.environ.get(
    "PERSONAL_FOODS_PATH",
    os.path.expanduser("~/.openclaw/workspace/food-tracker/personal-foods.yaml"),
)
POPULAR_FOODS_PATH = os.environ.get(
    "POPULAR_FOODS_PATH",
    os.path.expanduser(
        "~/.openclaw/workspace/skills/openclaw-food-tracker/references/popular-foods.yaml"
    ),
)
FOOD_LOG_DIR = os.environ.get(
    "FOOD_LOG_DIR",
    os.path.expanduser("~/.openclaw/workspace/food-tracker"),
)

mcp = FastMCP(
    "food-cache",
    instructions="Food cache lookup and management. Use lookup_food to find nutrition info for foods. Use add_personal_food to save new foods for future reuse.",
)


@mcp.tool()
def lookup_food(query: str) -> list[dict]:
    """Search for a food in the user's personal foods cache and the seed database.

    Returns matching entries with nutrition info (kcal, protein, fat, carbs per unit).
    Personal foods are returned first. Returns empty list if no match found.

    Args:
        query: Food name to search for (e.g. "omelette", "cortado", "salmon")
    """
    return search_foods(query, PERSONAL_FOODS_PATH, POPULAR_FOODS_PATH)


@mcp.tool()
def add_personal_food(
    name: str,
    unit: str,
    kcal_per_unit: float,
    protein_per_unit: float,
    fat_per_unit: float,
    carbs_per_unit: float,
    aliases: list[str] | None = None,
    qty_default: float = 1,
    notes: str = "",
) -> dict:
    """Save a new food to the user's personal foods cache for future reuse.

    Only call this when the user explicitly asks to save/remember a food.
    The food will be available via lookup_food in future queries.
    Rejects duplicates if the name or any alias already exists.

    Args:
        name: Canonical name for the food (e.g. "eggs benedict")
        unit: Serving unit (e.g. "serving", "100g", "cup", "slice")
        kcal_per_unit: Kilocalories per unit
        protein_per_unit: Grams of protein per unit
        fat_per_unit: Grams of fat per unit
        carbs_per_unit: Grams of carbs per unit
        aliases: Alternative names to match (e.g. ["eggs benny", "benny"])
        qty_default: Default quantity when user doesn't specify (default: 1)
        notes: Optional context (recipe, portion source, etc.)
    """
    entry = {
        "name": name,
        "aliases": aliases or [],
        "qty_default": qty_default,
        "unit": unit,
        "kcal_per_unit": kcal_per_unit,
        "protein_per_unit": protein_per_unit,
        "fat_per_unit": fat_per_unit,
        "carbs_per_unit": carbs_per_unit,
        "notes": notes,
    }
    return append_personal_food(entry, PERSONAL_FOODS_PATH)


@mcp.tool()
def log_food(
    datetime: str,
    food: str,
    qty: float,
    unit: str,
    kcal_per_unit: float,
    protein_per_unit: float,
    fat_per_unit: float,
    carbs_per_unit: float,
    source: str,
    confidence: float,
) -> dict:
    """Append a food entry to the monthly log and return today's running totals.

    Call this after resolving the food (via lookup_food or estimation).
    The tool handles file creation, row formatting, and total calculation.

    Args:
        datetime: When the food was consumed, in DD-MM-YYYY HH:MM format (24h)
        food: Food name, capitalised (e.g. "Scrambled eggs", "Black coffee")
        qty: Quantity consumed (e.g. 3, 0.5, 1)
        unit: Serving unit (e.g. "egg", "100g", "serving", "cup", "medium")
        kcal_per_unit: Kilocalories per unit
        protein_per_unit: Grams of protein per unit
        fat_per_unit: Grams of fat per unit
        carbs_per_unit: Grams of carbs per unit
        source: One of: cache_lookup, text_estimate, photo_estimate, photo_label
        confidence: Float 0-1 (cache_lookup=0.95, text_estimate=0.4-0.7, photo_estimate=0.3-0.5)
    """
    return log_food_entry(
        dt_str=datetime,
        food=food,
        qty=qty,
        unit=unit,
        protein_per_unit=protein_per_unit,
        fat_per_unit=fat_per_unit,
        carbs_per_unit=carbs_per_unit,
        kcal_per_unit=kcal_per_unit,
        source=source,
        confidence=confidence,
        log_dir=FOOD_LOG_DIR,
    )


@mcp.tool()
def get_todays_totals(date: str) -> dict:
    """Get all food log entries and total nutrition for a specific date.

    Use this for daily summaries or to check what was eaten on a given day.

    Args:
        date: Date to query in DD-MM-YYYY format (e.g. "04-04-2026")
    """
    return get_daily_totals(date_str=date, log_dir=FOOD_LOG_DIR)


if __name__ == "__main__":
    mcp.run()
