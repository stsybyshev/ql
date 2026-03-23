"""Shared parser for monthly food log markdown files.

Used by update-monthly-summary.py and generate-dashboard.py.
"""

from collections import defaultdict

# Column indices after splitting row on "|" (0-based, first element is empty)
COL_DATETIME = 1
COL_FOOD = 2
COL_PROTEIN_TOTAL = 9
COL_FAT_TOTAL = 10
COL_CARBS_TOTAL = 11
COL_KCAL_TOTAL = 12


def parse_log(filepath):
    """Parse a monthly food log, return list of (date_str, protein, fat, carbs, kcal, is_fasting)."""
    entries = []
    in_table = False
    with open(filepath) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("| Datetime"):
                in_table = True
                continue
            if in_table and stripped.startswith("|:--"):
                continue
            if in_table and stripped.startswith("|"):
                cols = [c.strip() for c in stripped.split("|")]
                if len(cols) < 13:
                    continue
                try:
                    date_str = cols[COL_DATETIME][:10]  # DD-MM-YYYY
                    food = cols[COL_FOOD]
                    protein = float(cols[COL_PROTEIN_TOTAL])
                    fat = float(cols[COL_FAT_TOTAL])
                    carbs = float(cols[COL_CARBS_TOTAL])
                    kcal = float(cols[COL_KCAL_TOTAL])
                    is_fasting = "FASTING" in food.upper()
                    entries.append((date_str, protein, fat, carbs, kcal, is_fasting))
                except (ValueError, IndexError):
                    continue
            elif in_table and not stripped.startswith("|"):
                break
    return entries


def group_by_date(entries):
    """Group parsed entries by date, summing totals per day."""
    daily = defaultdict(lambda: {"protein": 0, "fat": 0, "carbs": 0, "kcal": 0, "fasting": False})
    for date_str, protein, fat, carbs, kcal, is_fasting in entries:
        daily[date_str]["protein"] += protein
        daily[date_str]["fat"] += fat
        daily[date_str]["carbs"] += carbs
        daily[date_str]["kcal"] += kcal
        if is_fasting:
            daily[date_str]["fasting"] = True
    return dict(daily)


def compute_monthly_stats(daily):
    """Compute monthly summary stats from daily totals dict."""
    if not daily:
        return None

    days_tracked = len(daily)
    fasting_days = sum(1 for d in daily.values() if d["fasting"])

    total_kcal = sum(d["kcal"] for d in daily.values())
    total_protein = sum(d["protein"] for d in daily.values())
    total_fat = sum(d["fat"] for d in daily.values())
    total_carbs = sum(d["carbs"] for d in daily.values())

    avg_kcal = total_kcal / days_tracked
    avg_protein = total_protein / days_tracked
    avg_fat = total_fat / days_tracked
    avg_carbs = total_carbs / days_tracked

    # Macro % by caloric contribution (protein 4, fat 9, carbs 4 kcal/g)
    macro_kcal = avg_protein * 4 + avg_fat * 9 + avg_carbs * 4
    if macro_kcal > 0:
        pct_protein = avg_protein * 4 / macro_kcal * 100
        pct_fat = avg_fat * 9 / macro_kcal * 100
        pct_carbs = avg_carbs * 4 / macro_kcal * 100
    else:
        pct_protein = pct_fat = pct_carbs = 0

    sorted_days = sorted(daily.items(), key=lambda x: x[1]["kcal"])
    lowest_date, lowest = sorted_days[0][0], sorted_days[0][1]["kcal"]
    highest_date, highest = sorted_days[-1][0], sorted_days[-1][1]["kcal"]

    return {
        "days_tracked": days_tracked,
        "fasting_days": fasting_days,
        "avg_kcal": avg_kcal,
        "avg_protein": avg_protein,
        "avg_fat": avg_fat,
        "avg_carbs": avg_carbs,
        "pct_protein": pct_protein,
        "pct_fat": pct_fat,
        "pct_carbs": pct_carbs,
        "highest": highest,
        "highest_date": highest_date[:5],
        "lowest": lowest,
        "lowest_date": lowest_date[:5],
    }
