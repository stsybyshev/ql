#!/usr/bin/env python3
"""Generate data.json and self-contained HTML dashboard from monthly food logs.

Reads YYYY-MM.md files + config.yaml → writes data.json per DATA_CONTRACT.md.
Optionally injects data into the HTML template for a self-contained dashboard.

Usage:
    python3 dashboard/generate.py
    python3 dashboard/generate.py --data-dir /path/to/logs
    FOOD_LOG_DIR=/path/to/logs python3 dashboard/generate.py
"""

import argparse
import calendar
import glob
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import yaml

# Make scripts/ importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(SKILL_DIR, "scripts"))

from parse_foodlog import parse_log_rich, group_by_date_rich

# ── Portability: env var > CLI arg > default ──
DEFAULT_DATA_DIR = os.environ.get(
    "FOOD_LOG_DIR",
    os.path.expanduser("~/.openclaw/workspace/food-tracker"),
)
DEFAULT_CONFIG = os.path.join(SKILL_DIR, "..", "..", "portal", "assets", "config.yaml")
DEFAULT_OUTPUT_DIR = os.path.join(
    os.environ.get("FOOD_LOG_DIR", os.path.expanduser("~/.openclaw/workspace/food-tracker")),
    "dashboard",
)

MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_FULL = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]


# ── Widget builders (pure functions) ──


def build_summary_cards(this_month_days, prev_month_days):
    """Compute monthly average cards with delta from previous month."""
    def avg(days, key):
        if not days:
            return 0
        total = sum(d["totals"][key] for d in days.values())
        return total / len(days)

    def fmt(val):
        return f"{val:,.0f}"

    def delta(curr, prev):
        if prev == 0:
            return None, None
        pct = (curr - prev) / prev * 100
        sign = "+" if pct >= 0 else "\u2212"
        return round(pct, 1), f"{sign}{abs(pct):.1f}%"

    this_kcal = avg(this_month_days, "kcal")
    prev_kcal = avg(prev_month_days, "kcal")
    this_p = avg(this_month_days, "protein")
    prev_p = avg(prev_month_days, "protein")
    this_f = avg(this_month_days, "fat")
    prev_f = avg(prev_month_days, "fat")
    this_c = avg(this_month_days, "carbs")
    prev_c = avg(prev_month_days, "carbs")

    d_kcal_pct, d_kcal_disp = delta(this_kcal, prev_kcal)
    d_p_pct, d_p_disp = delta(this_p, prev_p)
    d_f_pct, d_f_disp = delta(this_f, prev_f)
    d_c_pct, d_c_disp = delta(this_c, prev_c)

    # Determine period labels from data
    this_period = _period_label(this_month_days)
    prev_period = _period_label(prev_month_days)

    return {
        "period": this_period,
        "prev_period": prev_period,
        "cards": [
            {
                "label": "Avg Daily Calories",
                "macro": "kcal",
                "value": round(this_kcal),
                "display": fmt(this_kcal),
                "unit": "kcal",
                "delta_pct": d_kcal_pct,
                "delta_display": d_kcal_disp,
                "trend_up": d_kcal_pct is not None and d_kcal_pct > 0,
            },
            {
                "label": "Avg Protein",
                "macro": "protein",
                "value": round(this_p),
                "display": fmt(this_p),
                "unit": "g/day",
                "delta_pct": d_p_pct,
                "delta_display": d_p_disp,
                "trend_up": d_p_pct is not None and d_p_pct > 0,
            },
            {
                "label": "Avg Fat",
                "macro": "fat",
                "value": round(this_f),
                "display": fmt(this_f),
                "unit": "g/day",
                "delta_pct": d_f_pct,
                "delta_display": d_f_disp,
                "trend_up": d_f_pct is not None and d_f_pct > 0,
            },
            {
                "label": "Avg Carbs",
                "macro": "carbs",
                "value": round(this_c),
                "display": fmt(this_c),
                "unit": "g/day",
                "delta_pct": d_c_pct,
                "delta_display": d_c_disp,
                "trend_up": d_c_pct is not None and d_c_pct > 0,
            },
        ],
    }


def build_yearly_heatmap(all_days, config, year):
    """Build 12x31 heatmap grid with per-month stats."""
    elevated_max = config.get("heatmap", {}).get("elevated_max", 3000)
    months = []
    for month_idx in range(12):
        month_num = month_idx + 1
        days_in_month = calendar.monthrange(year, month_num)[1]
        has_data = False
        daily_kcals = []
        fasting_count = 0
        over_elevated = 0

        day_entries = []
        for day_num in range(1, 32):
            iso = f"{year}-{month_num:02d}-{day_num:02d}"
            if day_num > days_in_month:
                day_entries.append({"day": day_num, "kcal": None})
                continue
            if iso in all_days:
                has_data = True
                d = all_days[iso]
                kcal_val = round(d["totals"]["kcal"])
                if d["is_fasting"]:
                    kcal_val = 0
                    fasting_count += 1
                daily_kcals.append(kcal_val)
                if kcal_val >= elevated_max:
                    over_elevated += 1
                day_entries.append({"day": day_num, "kcal": kcal_val})
            else:
                day_entries.append({"day": day_num, "kcal": None})

        avg_kcal = round(sum(daily_kcals) / len(daily_kcals)) if daily_kcals else 0

        months.append({
            "name": MONTH_SHORT[month_idx],
            "days_in_month": days_in_month,
            "has_data": has_data,
            "avg_kcal": avg_kcal,
            "fasting_days": fasting_count,
            "over_elevated_days": over_elevated,
            "days": day_entries,
        })

    return {"year": year, "months": months}


def build_todays_macros(today_meals, config):
    """Build today's macro totals with targets and protein/kg."""
    protein = sum(m["protein"] for m in today_meals)
    fat = sum(m["fat"] for m in today_meals)
    carbs = sum(m["carbs"] for m in today_meals)
    kcal = sum(m["kcal"] for m in today_meals)
    bw = config.get("body_weight_kg", 90)

    today_iso = date.today().isoformat()

    return {
        "date": today_iso,
        "macros": [
            {
                "label": "Calories",
                "value": round(kcal),
                "display": f"{kcal:,.0f}",
                "target": config.get("calories_target", 2500),
                "unit": "",
            },
            {
                "label": "Protein",
                "value": round(protein),
                "display": f"{protein:.0f}g",
                "target": config.get("protein_target_g", 144),
                "unit": "g",
            },
            {
                "label": "Fat",
                "value": round(fat),
                "display": f"{fat:.0f}g",
                "target": config.get("fat_target_g", 78),
                "unit": "g",
            },
            {
                "label": "Carbs",
                "value": round(carbs),
                "display": f"{carbs:.0f}g",
                "target": config.get("carbs_target_g", 295),
                "unit": "g",
            },
        ],
        "protein_per_kg": {
            "value": round(protein / bw, 2) if bw > 0 else 0,
            "target": config.get("protein_per_kg_target", 1.6),
            "body_weight_kg": bw,
        },
    }


def build_todays_meals(today_meals):
    """Format today's meal list with totals row."""
    today_iso = date.today().isoformat()

    meals = []
    totals = {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0}
    for m in today_meals:
        meals.append({
            "time": m["time"],
            "dish": m["food"],
            "kcal": round(m["kcal"]),
            "protein": round(m["protein"]),
            "fat": round(m["fat"]),
            "carbs": round(m["carbs"]),
        })
        totals["kcal"] += m["kcal"]
        totals["protein"] += m["protein"]
        totals["fat"] += m["fat"]
        totals["carbs"] += m["carbs"]

    return {
        "date": today_iso,
        "meals": meals,
        "totals": {k: round(v) for k, v in totals.items()},
    }


def build_meal_timing(this_month_days, config):
    """Compute average kcal and protein by time-of-day slot for the month."""
    mt = config.get("meal_timing", {})
    morning_end = mt.get("morning_end", 11)
    midday_end = mt.get("midday_end", 15)

    slot_totals = {
        "Morning": {"kcal": 0, "protein": 0},
        "Midday": {"kcal": 0, "protein": 0},
        "Evening": {"kcal": 0, "protein": 0},
    }

    num_days = len(this_month_days) if this_month_days else 1

    for day_data in this_month_days.values():
        for meal in day_data["meals"]:
            try:
                hour = int(meal["time"].split(":")[0])
            except (ValueError, IndexError):
                hour = 20  # fallback to Evening
            if hour < morning_end:
                slot = "Morning"
            elif hour < midday_end:
                slot = "Midday"
            else:
                slot = "Evening"
            slot_totals[slot]["kcal"] += meal["kcal"]
            slot_totals[slot]["protein"] += meal["protein"]

    tl_start = mt.get("timeline_start", 6)
    slots = [
        {
            "label": "Morning",
            "time_range": f"{tl_start}\u2013{morning_end}am",
            "avg_kcal": round(slot_totals["Morning"]["kcal"] / num_days),
            "avg_protein_g": round(slot_totals["Morning"]["protein"] / num_days),
        },
        {
            "label": "Midday",
            "time_range": f"{morning_end}am\u2013{midday_end - 12}pm",
            "avg_kcal": round(slot_totals["Midday"]["kcal"] / num_days),
            "avg_protein_g": round(slot_totals["Midday"]["protein"] / num_days),
        },
        {
            "label": "Evening",
            "time_range": f"{midday_end - 12}\u2013{mt.get('timeline_end', 22) - 12}pm",
            "avg_kcal": round(slot_totals["Evening"]["kcal"] / num_days),
            "avg_protein_g": round(slot_totals["Evening"]["protein"] / num_days),
        },
    ]

    # Simple rule-based insight
    total_protein = sum(s["avg_protein_g"] for s in slots)
    if total_protein > 0:
        evening_pct = slots[2]["avg_protein_g"] / total_protein * 100
        if evening_pct < 30:
            before_pct = 100 - evening_pct
            insight = f"Evening protein low ({slots[2]['avg_protein_g']}g avg) \u2014 {before_pct:.0f}% consumed before {midday_end - 12}pm"
        else:
            insight = f"Protein spread across day \u2014 evening accounts for {evening_pct:.0f}%"
    else:
        insight = "No protein data available"

    this_period = _period_label(this_month_days)

    return {
        "period": this_period,
        "slots": slots,
        "insight": insight,
    }


def build_macro_comp(this_month_days, prev_month_days):
    """Compute P/F/C caloric percentage split for current vs previous month."""
    def macro_pcts(days):
        if not days:
            return None
        total_p = sum(d["totals"]["protein"] for d in days.values())
        total_f = sum(d["totals"]["fat"] for d in days.values())
        total_c = sum(d["totals"]["carbs"] for d in days.values())
        num_days = len(days)
        avg_p, avg_f, avg_c = total_p / num_days, total_f / num_days, total_c / num_days
        avg_kcal = sum(d["totals"]["kcal"] for d in days.values()) / num_days
        macro_kcal = avg_p * 4 + avg_f * 9 + avg_c * 4
        if macro_kcal == 0:
            return None
        return {
            "protein_pct": round(avg_p * 4 / macro_kcal * 100),
            "fat_pct": round(avg_f * 9 / macro_kcal * 100),
            "carbs_pct": round(avg_c * 4 / macro_kcal * 100),
            "avg_kcal": round(avg_kcal),
            "avg_kcal_display": f"{avg_kcal:,.0f}",
        }

    this_pcts = macro_pcts(this_month_days)
    prev_pcts = macro_pcts(prev_month_days)

    this_period = _period_label(this_month_days)
    prev_period = _period_label(prev_month_days)

    this_result = {
        "label": _month_name_from_period(this_period),
        "period": this_period,
        **(this_pcts or {"protein_pct": 0, "fat_pct": 0, "carbs_pct": 0, "avg_kcal": 0, "avg_kcal_display": "0"}),
    }

    prev_result = {
        "label": _month_name_from_period(prev_period),
        "period": prev_period,
        **(prev_pcts or {"protein_pct": None, "fat_pct": None, "carbs_pct": None, "avg_kcal": None, "avg_kcal_display": None}),
    }

    # Rule-based insight
    if this_pcts and prev_pcts:
        p_diff = this_pcts["protein_pct"] - prev_pcts["protein_pct"]
        f_diff = this_pcts["fat_pct"] - prev_pcts["fat_pct"]
        c_diff = this_pcts["carbs_pct"] - prev_pcts["carbs_pct"]
        parts = []
        if abs(p_diff) >= 2:
            parts.append(f"Protein {'up' if p_diff > 0 else 'down'} {abs(p_diff)}%")
        if abs(f_diff) >= 2:
            parts.append(f"fat {'up' if f_diff > 0 else 'down'} {abs(f_diff)}%")
        if abs(c_diff) >= 2:
            parts.append(f"carbs {'up' if c_diff > 0 else 'down'} {abs(c_diff)}%")
        if parts:
            insight = f"{' from '.join([parts[0], _month_name_from_period(prev_period)])} \u2014 {', '.join(parts[1:])}." if len(parts) > 1 else f"{parts[0]} from {_month_name_from_period(prev_period)}."
        else:
            insight = "Macro split stable month-over-month."
    else:
        insight = "Insufficient data for comparison."

    return {
        "prev_month": prev_result,
        "this_month": this_result,
        "insight": insight,
    }


def build_monthly_insights():
    """Placeholder — LLM insights deferred to a future feature."""
    return {
        "generated_at": None,
        "log_count": 0,
        "period_display": "",
        "insights": [
            {"icon": "\U0001f6a7", "text": "Coming soon \u2014 not implemented yet"}
        ],
    }


# ── Helpers ──


def _period_label(days):
    """Extract YYYY-MM period from a days dict."""
    if not days:
        return None
    first_date = sorted(days.keys())[0]
    return first_date[:7]  # YYYY-MM


def _month_name_from_period(period):
    """Convert YYYY-MM to month name (e.g. '2026-04' → 'April')."""
    if not period:
        return ""
    month_idx = int(period[5:7]) - 1
    return MONTH_FULL[month_idx]


# ── Main pipeline ──


def run_pipeline(data_dir, config_path, output_dir, template_path=None):
    """Core pipeline: read logs + config → write data.json + optional HTML."""
    # Load config
    config_path = os.path.abspath(config_path)
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Find and parse all monthly log files
    files = sorted(glob.glob(os.path.join(data_dir, "????-??.md")))
    if not files:
        print(f"No YYYY-MM.md files found in {data_dir}")
        sys.exit(1)

    all_entries = []
    for filepath in files:
        all_entries.extend(parse_log_rich(filepath))

    all_days = group_by_date_rich(all_entries)

    # Partition by month
    today = date.today()
    this_month_prefix = today.strftime("%Y-%m")
    prev_month_date = today.replace(day=1) - timedelta(days=1)
    prev_month_prefix = prev_month_date.strftime("%Y-%m")

    this_month_days = {k: v for k, v in all_days.items() if k.startswith(this_month_prefix)}
    prev_month_days = {k: v for k, v in all_days.items() if k.startswith(prev_month_prefix)}

    today_iso = today.isoformat()
    # Exclude today from averages — partial day skews the numbers.
    # Fall back to including today if it's the only day in the month (e.g. 1st of month).
    this_month_days_avg = {k: v for k, v in this_month_days.items() if k != today_iso} or this_month_days
    today_meals = all_days.get(today_iso, {"meals": []})["meals"]

    # Build all widgets
    data = {
        "skill_id": "nutrition",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_range": {
            "first_log": sorted(all_days.keys())[0] if all_days else None,
            "last_log": sorted(all_days.keys())[-1] if all_days else None,
            "total_days_logged": len(all_days),
        },
        "config": config,
        "widgets": {
            "summary_cards": build_summary_cards(this_month_days_avg, prev_month_days),
            "yearly_heatmap": build_yearly_heatmap(all_days, config, today.year),
            "todays_macros": build_todays_macros(today_meals, config),
            "todays_meals": build_todays_meals(today_meals),
            "meal_timing": build_meal_timing(this_month_days_avg, config),
            "macro_comp": build_macro_comp(this_month_days_avg, prev_month_days),
            "monthly_insights": build_monthly_insights(),
        },
    }

    # Write data.json
    data_json_path = os.path.join(output_dir, "data.json")
    os.makedirs(output_dir, exist_ok=True)
    with open(data_json_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Generated {data_json_path} ({len(all_days)} days)")

    # Inject into HTML template if available
    if template_path is None:
        template_path = os.path.normpath(
            os.path.join(SKILL_DIR, "..", "..", "portal", "assets", "quantified-life.html")
        )
    if os.path.isfile(template_path):
        with open(template_path) as f:
            html = f.read()
        begin_marker = "// \u2500\u2500 BEGIN GENERATED DATA \u2500\u2500"
        end_marker = "// \u2500\u2500 END GENERATED DATA \u2500\u2500"
        start_idx = html.find(begin_marker)
        end_idx = html.find(end_marker)
        if start_idx >= 0 and end_idx >= 0:
            js_block = f"{begin_marker}\nconst DATA = {json.dumps(data)};\n{end_marker}"
            html_out = html[:start_idx] + js_block + html[end_idx + len(end_marker):]
        else:
            print("WARNING: data markers not found in HTML template")
            html_out = html
        html_path = os.path.join(output_dir, "quantified-life.html")
        with open(html_path, "w") as f:
            f.write(html_out)
        print(f"Generated {html_path}")
    else:
        print(f"HTML template not found at {template_path}, skipping HTML generation")

    return data


def main():
    parser = argparse.ArgumentParser(description="Generate nutrition dashboard data")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                        help="Directory containing YYYY-MM.md log files")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="Path to config.yaml")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help="Output directory for data.json and HTML")
    parser.add_argument("--template", default=None,
                        help="Path to HTML template (default: portal/assets/quantified-life.html)")
    args = parser.parse_args()

    run_pipeline(args.data_dir, args.config, args.output_dir, args.template)


if __name__ == "__main__":
    main()
