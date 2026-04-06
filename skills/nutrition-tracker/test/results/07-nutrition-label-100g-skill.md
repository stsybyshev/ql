# Food Tracker — Logging

I'll extract the label data and log your granola intake.

## Label Extraction
- **Per 100g:** 450 kcal | 8g protein | 18g fat | 65g carbs
- **Amount consumed:** 300g
- **Portions of 100g:** 3

## Appending to Log

Creating entry with:
- `unit` = `100g`
- `qty` = 3 (300g ÷ 100)
- Per-unit values copied directly from label
- `source` = `photo_label`, `confidence` = `0.85`

✅ **Logged: Granola (300g)** — 1,350 kcal | 24g protein | 54g fat | 195g carbs

---

## Today's Summary (2026-03-15)

| Meal | Calories | Protein | Fat | Carbs |
|---|---|---|---|---|
| Breakfast | 218 | 19.2g | 14.4g | 1.2g |
| Snack | 1,350 | 24.0g | 54.0g | 195.0g |
| **Daily Total** | **1,568** | **43.2g** | **68.4g** | **196.2g** |

---

**Note:** Granola is calorie-dense and carb-heavy. The 195g of carbs is high for a single intake — if this seems off, let me know and I can adjust the portion.