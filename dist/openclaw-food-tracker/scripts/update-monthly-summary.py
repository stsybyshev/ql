#!/usr/bin/env python3
"""Insert/update a textual monthly summary at the top of each YYYY-MM.md food log.

Usage:
    python3 scripts/update-monthly-summary.py
    python3 scripts/update-monthly-summary.py --data-dir dist/openclaw-food-tracker/assets
    python3 scripts/update-monthly-summary.py --dry-run

Designed to run via cron (e.g. daily at midnight). Idempotent — safe to run repeatedly.
"""

import argparse, glob, os, re, sys
from datetime import datetime

# Allow imports from the scripts directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_foodlog import parse_log, group_by_date, compute_monthly_stats


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

    pattern = r'(# Food Log — .+\n)\n(?:>.*\n)+\n((?:\| Datetime))'
    if re.search(pattern, content):
        updated = re.sub(pattern, rf'\1\n{summary_block}\n\n\2', content)
    else:
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
        daily = group_by_date(entries)
        stats = compute_monthly_stats(daily)
        if stats is None:
            continue
        summary_block = format_summary(stats)
        update_file(filepath, summary_block, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
