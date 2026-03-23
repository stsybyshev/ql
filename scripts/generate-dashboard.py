#!/usr/bin/env python3
"""Generate a self-contained HTML dashboard from monthly food log files.

Usage:
    python3 scripts/generate-dashboard.py --data-dir ~/.openclaw/workspace/food-tracker
    python3 scripts/generate-dashboard.py --data-dir dist/openclaw-food-tracker/assets --output /tmp/dashboard.html
"""

import argparse, glob, json, os, re, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parse_foodlog import parse_log, group_by_date, compute_monthly_stats

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "dashboard-template.html")

MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]
MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def parse_month_from_filename(filepath):
    """Extract (year, month_1indexed) from a YYYY-MM.md filename."""
    basename = os.path.splitext(os.path.basename(filepath))[0]
    parts = basename.split("-")
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    return None, None


def parse_date_str(date_str):
    """Parse DD-MM-YYYY to (day, month_1indexed, year)."""
    parts = date_str.split("-")
    if len(parts) == 3:
        return int(parts[0]), int(parts[1]), int(parts[2])
    return None, None, None


def build_data(data_dir):
    """Read all food log files and build dashboard data structures."""
    files = sorted(glob.glob(os.path.join(data_dir, "????-??.md")))
    if not files:
        print(f"No YYYY-MM.md files found in {data_dir}")
        sys.exit(1)

    # Collect all daily data across all months
    year_data = {}       # "monthIdx-day" -> {kcal, fasting}
    monthly_stats = []   # list of {month_label, year, month, stats, daily}
    all_years = set()

    for filepath in files:
        year, month = parse_month_from_filename(filepath)
        if year is None:
            continue
        all_years.add(year)

        entries = parse_log(filepath)
        if not entries:
            continue

        daily = group_by_date(entries)
        stats = compute_monthly_stats(daily)
        if stats is None:
            continue

        month_idx = month - 1  # 0-based for JS

        # Build year_data entries
        for date_str, day_data in daily.items():
            day, _, _ = parse_date_str(date_str)
            if day is not None:
                key = f"{month_idx}-{day}"
                year_data[key] = {
                    "kcal": round(day_data["kcal"]),
                    "fasting": day_data["fasting"]
                }

        days_in = DAYS_IN_MONTH[month_idx]
        month_label = f"{MONTH_SHORT[month_idx]} {year}"

        monthly_stats.append({
            "month_label": month_label,
            "year": year,
            "month": month,
            "month_idx": month_idx,
            "stats": stats,
            "daily": daily,
            "days_in_month": days_in,
        })

    if not monthly_stats:
        print("No data found in any files")
        sys.exit(1)

    # Current month = last in sorted order
    current = monthly_stats[-1]

    # Build current month daily array for trend chart
    current_month_data = []
    days_in = current["days_in_month"]
    for day in range(1, days_in + 1):
        date_str = f"{day:02d}-{current['month']:02d}-{current['year']}"
        if date_str in current["daily"]:
            d = current["daily"][date_str]
            entry = {"day": day, "kcal": round(d["kcal"])}
            if d["fasting"]:
                entry["fasting"] = True
            current_month_data.append(entry)
        else:
            current_month_data.append({"day": day, "kcal": None})

    current_month_name = f"{MONTH_NAMES[current['month_idx']]} {current['year']}"
    current_year = max(all_years)

    # Monthly table rows (most recent first)
    monthly_rows = []
    for m in reversed(monthly_stats):
        monthly_rows.append({
            "month": m["month_label"],
            "days_tracked": m["stats"]["days_tracked"],
            "days_in_month": m["days_in_month"],
            "avg_kcal": round(m["stats"]["avg_kcal"]),
            "avg_protein": round(m["stats"]["avg_protein"]),
            "avg_fat": round(m["stats"]["avg_fat"]),
            "avg_carbs": round(m["stats"]["avg_carbs"]),
            "fasting_days": m["stats"]["fasting_days"],
        })

    macros = {
        "protein": round(current["stats"]["avg_protein"]),
        "fat": round(current["stats"]["avg_fat"]),
        "carbs": round(current["stats"]["avg_carbs"]),
    }

    return {
        "year_data": year_data,
        "current_month": current_month_data,
        "macros": macros,
        "current_month_name": current_month_name,
        "current_year": current_year,
        "monthly_rows": monthly_rows,
    }


def generate_js_block(data):
    """Generate the JavaScript data block to inject into the template."""
    lines = []
    lines.append("// ── BEGIN GENERATED DATA ──")
    lines.append(f"const YEAR_DATA = {json.dumps(data['year_data'])};")
    lines.append(f"const CURRENT_MONTH = {json.dumps(data['current_month'])};")
    lines.append(f"const MACROS = {json.dumps(data['macros'])};")
    lines.append(f"const CURRENT_MONTH_NAME = {json.dumps(data['current_month_name'])};")
    lines.append(f"const CURRENT_YEAR = {data['current_year']};")
    lines.append(f"const MONTHLY_ROWS = {json.dumps(data['monthly_rows'])};")
    lines.append(f'const GENERATED_DATE = {json.dumps(datetime.now().strftime("%b %d, %Y %H:%M"))};')
    lines.append(f'const INSIGHTS_HTML = "";')
    lines.append("// ── END GENERATED DATA ──")
    return "\n".join(lines)


def render_dashboard(template_html, data):
    """Replace the generated data block in the template."""
    js_block = generate_js_block(data)

    # Replace the data block between markers
    pattern = r'// ── BEGIN GENERATED DATA ──.*?// ── END GENERATED DATA ──'
    result = re.sub(pattern, js_block, template_html, flags=re.DOTALL)

    return result


def main():
    parser = argparse.ArgumentParser(description="Generate food tracking dashboard")
    parser.add_argument("--data-dir", default="./food-tracker",
                        help="Directory containing YYYY-MM.md files")
    parser.add_argument("--output", default="./dashboard.html",
                        help="Output HTML file path")
    args = parser.parse_args()

    # Load template
    if not os.path.isfile(TEMPLATE_PATH):
        print(f"Template not found: {TEMPLATE_PATH}")
        sys.exit(1)

    with open(TEMPLATE_PATH) as f:
        template_html = f.read()

    # Build data from food logs
    data = build_data(args.data_dir)

    # Render
    html = render_dashboard(template_html, data)

    # Write output
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w") as f:
        f.write(html)

    stats = data["monthly_rows"][0] if data["monthly_rows"] else {}
    print(f"Dashboard generated: {args.output}")
    print(f"  Current month: {data['current_month_name']}")
    print(f"  Days tracked: {stats.get('days_tracked', 0)}")
    print(f"  Avg kcal: {stats.get('avg_kcal', 0):,}")
    print(f"  Months: {len(data['monthly_rows'])}")


if __name__ == "__main__":
    main()
