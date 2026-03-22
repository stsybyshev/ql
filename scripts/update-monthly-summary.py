#!/usr/bin/env python3
"""Insert/update a textual monthly summary at the top of each YYYY-MM.md food log.

Usage:
    python3 scripts/update-monthly-summary.py
    python3 scripts/update-monthly-summary.py --data-dir dist/openclaw-food-tracker/assets
    python3 scripts/update-monthly-summary.py --dry-run

Designed to run via cron (e.g. daily at midnight). Idempotent — safe to run repeatedly.
"""

import argparse, glob, os, re
from collections import defaultdict
from datetime import datetime

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
                continue  # separator row
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
                break  # end of table
    return entries


def compute_summary(entries):
    """Compute monthly summary stats from parsed entries."""
    daily = defaultdict(lambda: {"protein": 0, "fat": 0, "carbs": 0, "kcal": 0, "fasting": False})
    for date_str, protein, fat, carbs, kcal, is_fasting in entries:
        daily[date_str]["protein"] += protein
        daily[date_str]["fat"] += fat
        daily[date_str]["carbs"] += carbs
        daily[date_str]["kcal"] += kcal
        if is_fasting:
            daily[date_str]["fasting"] = True

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

    # Highest / lowest day
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
        "highest_date": highest_date[:5],  # DD-MM
        "lowest": lowest,
        "lowest_date": lowest_date[:5],
    }


def format_summary(stats):
    """Format summary stats as a markdown blockquote."""
    today = datetime.now().strftime("%d-%m-%Y")
    lines = [
        f'> **Monthly summary** (generated {today})',
        f'> Days tracked: {stats["days_tracked"]} | Fasting days: {stats["fasting_days"]}',
        f'> Avg daily: {stats["avg_kcal"]:,.0f} kcal | '
        f'{stats["avg_protein"]:.0f}g protein ({stats["pct_protein"]:.0f}%) | '
        f'{stats["avg_fat"]:.0f}g fat ({stats["pct_fat"]:.0f}%) | '
        f'{stats["avg_carbs"]:.0f}g carbs ({stats["pct_carbs"]:.0f}%)',
        f'> Highest: {stats["highest"]:,.0f} kcal ({stats["highest_date"]}) | '
        f'Lowest: {stats["lowest"]:,.0f} kcal ({stats["lowest_date"]})',
    ]
    return "\n".join(lines)


def update_file(filepath, summary_block, dry_run=False):
    """Insert or replace the summary block in a monthly log file."""
    with open(filepath) as f:
        content = f.read()

    # Remove any existing summary block (blockquote lines between heading and table)
    pattern = r'(# Food Log — .+\n)\n(?:>.*\n)+\n((?:\| Datetime))'
    if re.search(pattern, content):
        updated = re.sub(pattern, rf'\1\n{summary_block}\n\n\2', content)
    else:
        # Insert after the heading
        updated = re.sub(
            r'(# Food Log — .+\n)\n((?:\| Datetime))',
            rf'\1\n{summary_block}\n\n\2',
            content
        )

    if dry_run:
        print(f"\n--- {os.path.basename(filepath)} ---")
        print(summary_block)
        return

    with open(filepath, "w") as f:
        f.write(updated)
    print(f"  Updated: {filepath}")


def main():
    parser = argparse.ArgumentParser(description="Update monthly food log summaries")
    parser.add_argument("--data-dir", default="./food-tracker",
                        help="Directory containing YYYY-MM.md files")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summaries without modifying files")
    parser.add_argument("--current-month", action="store_true",
                        help="Only process the current month's file")
    args = parser.parse_args()

    if args.current_month:
        current = datetime.now().strftime("%Y-%m")
        files = [os.path.join(args.data_dir, f"{current}.md")]
        files = [f for f in files if os.path.isfile(f)]
    else:
        files = sorted(glob.glob(os.path.join(args.data_dir, "????-??.md")))
    if not files:
        print(f"No YYYY-MM.md files found in {args.data_dir}")
        return

    for filepath in files:
        entries = parse_log(filepath)
        if not entries:
            print(f"  Skipped (no entries): {filepath}")
            continue
        stats = compute_summary(entries)
        if stats is None:
            continue
        summary_block = format_summary(stats)
        update_file(filepath, summary_block, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
