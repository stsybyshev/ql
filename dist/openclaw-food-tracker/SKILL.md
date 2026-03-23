---
name: openclaw-food-tracker
description: Track food intake and estimate nutrition. Use when the user reports eating something, describes a meal, mentions calories or macros, attaches a food photo or nutrition label, says "my usual", asks what they ate today, or requests a daily/weekly/monthly food summary. Do NOT activate for recipe requests, cooking advice, or general food conversation.
---

# Food Tracker

Log food intake, estimate calories and macros, and produce summaries. All data is stored in append-only markdown files.

## Quick Reference

| User intent | Operation | Files touched |
|---|---|---|
| "had 3 eggs and coffee" | Log food entry | `YYYY-MM.md` (append) |
| "had my usual breakfast" | Cache lookup → log | `popular-foods.yaml` (read) → `YYYY-MM.md` (append) |
| [photo of meal] + "lunch!" | Photo estimate → log | `YYYY-MM.md` (append) |
| [photo of nutrition label] | Label extraction → log | `YYYY-MM.md` (append) |
| "what did I eat today?" | Daily summary | `YYYY-MM.md` (read) |
| "weekly summary" | Aggregate summary | `YYYY-MM.md` (read) |
| "save eggs benedict for future" | Learn food | `personal-foods.yaml` (append) |

## File Locations

There are two directories. Do NOT confuse them:

1. **Skill directory** (READ-ONLY — never write here, never create files here):
   `skills/openclaw-food-tracker/`
   - `references/popular-foods.yaml` — seed food database (read-only, never modify)
   - `references/personal-foods.yaml` — initial personal foods (read-only copy)
   - `assets/monthly-template.md` — template for new month files (read-only)

2. **Workspace directory** (READ-WRITE — this is where you read and write user data):
   - `food-tracker/YYYY-MM.md` — monthly food logs (append-only, one file per month)
   - `food-tracker/personal-foods.yaml` — user's learned foods (read-write, checked FIRST before the seed database)

When logging food, ALWAYS:
- Read `food-tracker/personal-foods.yaml` first (workspace — user's learned foods)
- Then read `skills/openclaw-food-tracker/references/popular-foods.yaml` (skill dir — seed database)
- Write new entries ONLY to files in `food-tracker/` (workspace)
- NEVER write to or create files in `skills/` or `references/`

## Step 1: Recognise Intent

Classify the user message into exactly one category:

| Category | Trigger phrases | Action |
|---|---|---|
| **LOG_FOOD** | "had", "ate", "just eaten", "for breakfast/lunch/dinner", "grabbed a", "my usual" | → proceed to Step 2 |
| **LOG_FOOD_PHOTO** | Image attachment + food context (meal, plate, dish, restaurant) | → proceed to Step 4 |
| **LOG_FOOD_LABEL** | Image attachment + nutrition label visible (barcode, "Nutrition Facts", per-100g table), or user manually provides per-100g values | → proceed to Step 5 |
| **SUMMARY** | "what did I eat", "today's calories", "weekly summary", "how much protein" | → proceed to Step 6 |
| **LEARN_FOOD** | "save this", "remember this", "add to favourites", "for future reuse" | → proceed to Step 7 |
| **NOT_FOOD_TRACKING** | Recipe requests, cooking advice, restaurant recommendations, general food chat | → Do NOT activate this skill. Respond normally. |

CRITICAL: If unsure whether the message is food logging or general conversation, do NOT log. It is better to miss a log than to create a false entry.

## Step 2: Resolve Food from Cache

For each food item mentioned in the user message:

1. Read `food-tracker/personal-foods.yaml` first (workspace), then `skills/openclaw-food-tracker/references/popular-foods.yaml` (skill dir, read-only)
2. For each food mentioned, check if the food name or any alias matches (case-insensitive, partial match OK). A personal-foods match takes priority over a popular-foods match.
3. If match found:
   - Use `kcal_per_unit`, `protein_per_unit`, `fat_per_unit`, `carbs_per_unit` from the cache entry
   - If user specified a quantity, use it. Otherwise use `qty_default` from the cache entry
   - Set `source` = `cache_lookup`, `confidence` = `0.95`
   - Proceed to Step 3
4. If no match found:
   - Estimate nutrition from your general knowledge
   - If you know the per-100g values (common for packaged foods), use `unit` = `100g` and set `qty` to portions of 100g consumed. This avoids division and keeps per-unit columns human-readable.
   - Set `source` = `text_estimate`
   - Set `confidence` between `0.4` and `0.7` based on how common/well-known the food is
   - Proceed to Step 3

### Cache lookup examples

User says "3 scrambled eggs" → match `egg` (alias: "scrambled eggs") → qty=3, unit=egg, kcal_per_unit=72 → kcal_total=216

User says "a banana" → match `banana` → qty=1 (qty_default), unit=banana, kcal_per_unit=105 → kcal_total=105

User says "pad thai" → no cache match → estimate from general knowledge → source=text_estimate, confidence=0.5

## Step 3: Append to Monthly Log

1. Determine the current date and time (use message timestamp if available, otherwise current time). Format datetime as `DD-MM-YYYY HH:MM` (24h).
2. Determine the file path: `food-tracker/YYYY-MM.md` where YYYY-MM matches the current month
3. Read the file first. If it already exists, skip to step 4 — do NOT overwrite, recreate, or replace an existing file. Only if the file does not exist, create a new one using this exact template (replace the heading with the full month name and year, e.g. "March 2026", "August 2025"):

```markdown
# Food Log — March 2026

| Datetime         | Food                   | Qty | Unit    | Protein/u | Fat/u | Carbs/u | Kcal/u | Protein | Fat   | Carbs | Kcal  | Source       | Confidence |
|:-----------------|:-----------------------|----:|:--------|----------:|------:|--------:|-------:|--------:|------:|------:|------:|:-------------|:-----------|
```

Pad each cell with spaces so columns stay aligned when viewed as raw markdown. Match the column widths shown above.

4. Append one row per food item to the end of the table. Compute totals as: `*_total = qty × *_per_unit`. Capitalise the first letter of the food name (e.g. "Scrambled eggs", "Black coffee").

### Example row

For "3 scrambled eggs" resolved from cache (egg: 72 kcal, 6.3g protein, 4.8g fat, 0.4g carbs per egg):

```
| 15-03-2026 08:30 | Scrambled eggs         |   3 | egg     |       6.3 |   4.8 |     0.4 |     72 |    18.9 |  14.4 |   1.2 |   216 | cache_lookup | 0.95       |
```

5. After appending, respond to the user with:
   - What was logged (food, quantity, calories)
   - Today's running totals (sum all `*_total` columns where datetime starts with today's date)

### Example response

> Logged: 3 scrambled eggs (216 kcal, 18.9g protein, 14.4g fat, 1.2g carbs)
>
> Today so far: 218 kcal | 19.2g protein | 14.4g fat | 1.2g carbs

## Step 4: Photo of Meal (No Cache Match)

When the user attaches a photo of food (not a nutrition label):

1. If you have vision capability, analyse the image:
   - Identify each distinct food item visible
   - Estimate portion sizes from visual cues (plate size, utensils for scale)
   - Estimate nutrition per item using your general knowledge
   - Set `source` = `photo_estimate`, `confidence` = `0.3` to `0.5`
2. If you do NOT have vision capability:
   - Ask the user to describe the meal in text
   - Once described, proceed to Step 2 (cache lookup) with the text description
3. Log each identified food item using Step 3
4. In your response, explicitly list what you identified and note the low confidence:

> From your photo I identified: burger (~550 kcal), french fries (~365 kcal)
> Confidence is low (0.35) — let me know if I should adjust portions.

## Step 5: Photo of Nutrition Label

When the user attaches a photo containing a nutrition label:

1. Extract from the label:
   - Serving size (grams or ml)
   - Calories per serving (or per 100g)
   - Protein per serving (or per 100g)
   - Fat per serving (or per 100g)
   - Carbohydrates per serving (or per 100g)
2. Ask the user (or infer from context) how much they consumed:
   - "How many servings did you have?" or "What was the total weight?"
   - If the user already specified (e.g. "had half the pack, 200g total"), use that
3. Calculate per-unit and total values for the log entry:
   - Use `unit` = `100g`
   - Copy the label's per-100g values directly into `kcal_unit`, `protein_unit`, `fat_unit`, `carbs_unit` — no division required
   - Set `qty` = total grams consumed ÷ 100 (e.g. 600g → qty = 6, 250g → qty = 2.5)
   - Compute `*_total` = qty × `*_unit`
4. Set `source` = `photo_label`, `confidence` = `0.85` (labels are reliable, portion estimate may vary)
5. Log using Step 3
6. In your response, show the extracted label data and the computed entry

### Label example

Label says: 55 kcal per 100g, 1.0g protein, 2.8g fat, 5.9g carbs. User ate 600g.

→ `unit` = `100g`, `qty` = 6 (600 ÷ 100), per-unit values copied straight from label:

```
| 15-03-2026 12:30 | Tomato soup with cream |   6 | 100g    |       1.0 |   2.8 |     5.9 |     55 |     6.0 |  16.8 |  35.4 |   330 | photo_label  | 0.85       |
```

## Step 6: Summaries

When the user asks for a summary:

1. Read the relevant `YYYY-MM.md` file(s)
2. Filter rows by datetime:
   - "today" → rows where datetime starts with today's `YYYY-MM-DD`
   - "this week" → rows from the last 7 days
   - "this month" → all rows in the current month's file
3. Sum `kcal_total`, `protein_total`, `fat_total`, `carbs_total` across matching rows
4. Count the number of entries
5. Respond with the totals:

> **Today (2026-03-15):** 3 entries
> - Calories: 1,245 kcal
> - Protein: 68g | Fat: 42g | Carbs: 156g

For weekly/monthly summaries, also show daily averages. Calculate daily average as:
- Denominator = number of **distinct dates that have at least one log entry** (including FASTING markers)
- Days with no entries at all are **excluded** from the denominator (assumed untracked, not fasted)
- This means fasting days (with a FASTING marker) correctly pull the average down, while missed tracking days do not

CRITICAL: Summaries are read-only. Do NOT append any new rows to the log file when producing a summary.

## Step 7: Learn a New Food

When the user explicitly asks to save a food for future reuse:

1. Confirm with the user: what name, typical quantity, and unit to use
2. Determine nutrition values from the most recent log entry for that food, or ask the user
3. Append a new entry to `food-tracker/personal-foods.yaml` (in the workspace, NOT in the skill directory):

```yaml
- name: eggs benedict
  aliases: [eggs benny, benny]
  qty_default: 1
  unit: serving
  kcal_per_unit: 290
  protein_per_unit: 17.0
  fat_per_unit: 18.0
  carbs_per_unit: 15.0
  notes: "learned from user on 2026-03-15"
  source: learned
```

4. CRITICAL rules:
   - Only save after explicit user confirmation — never save speculatively
   - Always write to `food-tracker/personal-foods.yaml` (workspace) — never modify `popular-foods.yaml` in the skill directory
   - If an alias already matches an existing entry in personal-foods.yaml, update that entry instead of creating a duplicate
   - Always set `source: learned`
5. Confirm to the user:

> Saved "eggs benedict" to your food cache. Next time just say "had eggs benny" and I'll log it automatically.

## Edge Cases

### Multiple foods in one message

"Had eggs, toast, and coffee" → create three separate rows in the log, one per food item. Sum them in the response.

### Ambiguous portions

"Big bowl of pasta" → estimate a reasonable portion (e.g. 300g cooked, roughly 1.5× default serving). Set confidence to `0.4`–`0.5`. Mention the assumed portion in your response so the user can correct it.

### "My usual" without cache match

If the user says "my usual" or "the regular" but no matching entry exists in the cache → do NOT guess. Respond: "I don't have your usual saved yet. What did you have? I can save it for next time."

### Alcoholic drinks

Beer, wine, spirits: the `kcal_per_unit` includes alcohol calories which are NOT reflected in protein/fat/carbs. Do not try to reconcile the macro sum with total calories for alcoholic beverages.

### Corrections

If the user says "that's wrong" or "I actually had 2, not 3" → append a new corrected row with the correct values. Do NOT delete or modify existing rows. The log is append-only.

### Fasting days

If the user says "I'm fasting today", "fasting day", "water fast", or similar → log a single FASTING marker row for that date with all macros and kcal = 0. Do NOT log water, tea, or black coffee separately on fasting days (they are ~0 kcal and the marker already covers the day). The FASTING marker ensures the day is counted in average calculations as a 0-calorie day rather than being skipped as an untracked day.

```
| 20-03-2026 08:00 | FASTING                |   1 | day     |       0.0 |   0.0 |     0.0 |      0 |     0.0 |   0.0 |   0.0 |     0 | cache_lookup | 1.0        |
```

### Time in the past

"Yesterday I had pizza for dinner" → use yesterday's date, estimate dinner time (e.g. 19:00). Set the datetime accordingly. The entry still goes in the correct month's file based on the stated date.

## Constraints

- **Append-only:** Never delete, overwrite, or modify existing rows in the monthly log. Never replace or recreate a monthly file that already exists. Only append new rows to the end of the existing table. Corrections are new rows.
- **One shot:** This skill may run in a subagent without conversation history. Produce a complete result (log entry + response) from a single user message.
- **No external APIs:** Resolve from cache or estimate from general knowledge. Do not call external nutrition APIs.
- **File format:** The monthly log table must be valid markdown. Every row must have exactly 14 pipe-separated columns matching the header.
