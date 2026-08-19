"""Tests for food cache MCP server."""

import tempfile
import shutil
from pathlib import Path

import pytest

from food_cache import (
    search_foods, append_personal_food, load_yaml, log_food_entry, get_daily_totals,
    resolve_log_units, _parse_weight_unit, normalize_food_name,
)

# Use real YAML files for search tests
PERSONAL = "/home/stan/.openclaw/workspace/food-tracker/personal-foods.yaml"
POPULAR = "/home/stan/.openclaw/workspace/skills/openclaw-food-tracker/references/popular-foods.yaml"


class TestSearchFoods:
    """Test lookup_food logic against real YAML files."""

    def test_personal_match_omelette(self):
        results = search_foods("omelette", PERSONAL, POPULAR)
        assert len(results) >= 1
        names = [r["name"].lower() for r in results]
        assert any("omelette" in n for n in names)
        # Personal match should come first
        assert results[0]["source"] == "personal"

    def test_personal_match_cortado(self):
        results = search_foods("cortado", PERSONAL, POPULAR)
        assert len(results) >= 1
        assert results[0]["name"].lower() == "cortado"
        assert results[0]["kcal_per_unit"] == 30

    def test_popular_match_rice(self):
        results = search_foods("rice", PERSONAL, POPULAR)
        assert len(results) >= 1
        # Should find rice in popular-foods (seed)
        seed_results = [r for r in results if r["source"] == "seed"]
        assert len(seed_results) >= 1

    def test_no_match(self):
        results = search_foods("xyznonexistent123", PERSONAL, POPULAR)
        assert results == []

    def test_empty_query(self):
        results = search_foods("", PERSONAL, POPULAR)
        assert results == []

    def test_partial_match(self):
        results = search_foods("cappuccino", PERSONAL, POPULAR)
        assert len(results) >= 1
        names = [r["name"].lower() for r in results]
        assert any("cappuccino" in n for n in names)

    def test_alias_match(self):
        # "mct" is an alias for "MCT 8 oil"
        results = search_foods("mct", PERSONAL, POPULAR)
        assert len(results) >= 1
        assert any("mct" in r["name"].lower() for r in results)

    def test_personal_priority(self):
        # "cashew" exists in personal-foods — should come before any seed match
        results = search_foods("cashew", PERSONAL, POPULAR)
        assert len(results) >= 1
        assert results[0]["source"] == "personal"

    def test_result_has_all_fields(self):
        results = search_foods("omelette", PERSONAL, POPULAR)
        assert len(results) >= 1
        entry = results[0]
        for field in ["name", "aliases", "qty_default", "unit", "kcal_per_unit",
                      "protein_per_unit", "fat_per_unit", "carbs_per_unit", "source"]:
            assert field in entry, f"Missing field: {field}"


class TestAppendPersonalFood:
    """Test add_personal_food logic using temp files."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = Path(self.tmpdir) / "personal-foods.yaml"
        # Seed with one entry
        self.path.write_text(
            '- name: test eggs\n'
            '  aliases: [eggs, test egg]\n'
            '  qty_default: 1\n'
            '  unit: egg\n'
            '  kcal_per_unit: 72\n'
            '  protein_per_unit: 6.3\n'
            '  fat_per_unit: 4.8\n'
            '  carbs_per_unit: 0.4\n'
            '  notes: "test"\n'
            '  source: learned\n'
        )

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_append_new_food(self):
        entry = {
            "name": "test burrito",
            "aliases": ["burrito"],
            "unit": "serving",
            "kcal_per_unit": 500,
            "protein_per_unit": 25,
            "fat_per_unit": 20,
            "carbs_per_unit": 50,
        }
        result = append_personal_food(entry, str(self.path))
        assert result["status"] == "ok"

        # Verify it's in the file
        data = load_yaml(str(self.path))
        names = [d["name"] for d in data]
        assert "test burrito" in names

    def test_reject_duplicate_alias(self):
        entry = {
            "name": "duplicate food",
            "aliases": ["eggs"],  # "eggs" already exists as alias of "test eggs"
            "unit": "serving",
            "kcal_per_unit": 100,
            "protein_per_unit": 5,
            "fat_per_unit": 3,
            "carbs_per_unit": 10,
        }
        result = append_personal_food(entry, str(self.path))
        assert "error" in result
        assert "eggs" in result["error"].lower()

    def test_reject_duplicate_name(self):
        entry = {
            "name": "test eggs",  # same name as existing
            "aliases": [],
            "unit": "egg",
            "kcal_per_unit": 72,
            "protein_per_unit": 6.3,
            "fat_per_unit": 4.8,
            "carbs_per_unit": 0.4,
        }
        result = append_personal_food(entry, str(self.path))
        assert "error" in result

    def test_missing_required_field(self):
        entry = {
            "name": "incomplete food",
            # missing unit, macros
        }
        result = append_personal_food(entry, str(self.path))
        assert "error" in result
        assert "missing" in result["error"].lower()

    def test_append_preserves_existing(self):
        entry = {
            "name": "new food",
            "aliases": ["new"],
            "unit": "serving",
            "kcal_per_unit": 200,
            "protein_per_unit": 10,
            "fat_per_unit": 8,
            "carbs_per_unit": 20,
        }
        append_personal_food(entry, str(self.path))

        data = load_yaml(str(self.path))
        assert len(data) == 2
        assert data[0]["name"] == "test eggs"  # original still there
        assert data[1]["name"] == "new food"

    def test_source_always_learned(self):
        entry = {
            "name": "sourced food",
            "aliases": [],
            "unit": "serving",
            "kcal_per_unit": 100,
            "protein_per_unit": 5,
            "fat_per_unit": 3,
            "carbs_per_unit": 10,
        }
        result = append_personal_food(entry, str(self.path))
        assert result["entry"]["source"] == "learned"


class TestLogFoodEntry:
    """Test log_food MCP tool logic using temp directories."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_creates_file_and_logs(self):
        result = log_food_entry(
            dt_str="15-04-2026 08:30",
            food="Scrambled eggs",
            qty=3,
            unit="egg",
            protein_per_unit=6.3,
            fat_per_unit=4.8,
            carbs_per_unit=0.4,
            kcal_per_unit=72,
            source="cache_lookup",
            confidence=0.95,
            log_dir=self.tmpdir,
        )
        assert result["status"] == "ok"
        assert result["entry"]["kcal_total"] == 216
        assert result["entry"]["protein_total"] == 18.9
        assert result["today"]["kcal"] == 216
        assert result["today"]["entries"] == 1
        # Verify file was created with correct name
        log_file = Path(self.tmpdir) / "2026-04.md"
        assert log_file.exists()
        content = log_file.read_text()
        assert "Food Log — April 2026" in content
        assert "Scrambled eggs" in content

    def test_appends_to_existing_file(self):
        # Log first entry
        log_food_entry(
            dt_str="15-04-2026 08:30", food="Eggs", qty=3, unit="egg",
            protein_per_unit=6.3, fat_per_unit=4.8, carbs_per_unit=0.4,
            kcal_per_unit=72, source="cache_lookup", confidence=0.95,
            log_dir=self.tmpdir,
        )
        # Log second entry same day
        result = log_food_entry(
            dt_str="15-04-2026 12:00", food="Rice", qty=2, unit="100g",
            protein_per_unit=2.7, fat_per_unit=0.3, carbs_per_unit=28,
            kcal_per_unit=130, source="cache_lookup", confidence=0.95,
            log_dir=self.tmpdir,
        )
        assert result["status"] == "ok"
        assert result["today"]["entries"] == 2
        assert result["today"]["kcal"] == 216 + 260  # 3*72 + 2*130

    def test_different_days_same_month(self):
        log_food_entry(
            dt_str="01-04-2026 08:00", food="Coffee", qty=1, unit="cup",
            protein_per_unit=0.3, fat_per_unit=0, carbs_per_unit=0,
            kcal_per_unit=5, source="text_estimate", confidence=0.8,
            log_dir=self.tmpdir,
        )
        result = log_food_entry(
            dt_str="02-04-2026 08:00", food="Tea", qty=1, unit="cup",
            protein_per_unit=0, fat_per_unit=0, carbs_per_unit=0,
            kcal_per_unit=2, source="text_estimate", confidence=0.8,
            log_dir=self.tmpdir,
        )
        # Today's totals should only include the 2nd day
        assert result["today"]["kcal"] == 2
        assert result["today"]["entries"] == 1

    def test_invalid_datetime(self):
        result = log_food_entry(
            dt_str="2026-04-15 08:30", food="Eggs", qty=1, unit="egg",
            protein_per_unit=6.3, fat_per_unit=4.8, carbs_per_unit=0.4,
            kcal_per_unit=72, source="cache_lookup", confidence=0.95,
            log_dir=self.tmpdir,
        )
        assert "error" in result

    def test_file_header_correct_month(self):
        log_food_entry(
            dt_str="15-12-2026 08:30", food="Toast", qty=1, unit="slice",
            protein_per_unit=2.5, fat_per_unit=0.8, carbs_per_unit=14,
            kcal_per_unit=75, source="text_estimate", confidence=0.7,
            log_dir=self.tmpdir,
        )
        log_file = Path(self.tmpdir) / "2026-12.md"
        content = log_file.read_text()
        assert "Food Log — December 2026" in content


class TestGetDailyTotals:
    """Test get_daily_totals MCP tool logic."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        # Pre-populate with some entries
        log_food_entry(
            dt_str="15-04-2026 08:30", food="Eggs", qty=3, unit="egg",
            protein_per_unit=6.3, fat_per_unit=4.8, carbs_per_unit=0.4,
            kcal_per_unit=72, source="cache_lookup", confidence=0.95,
            log_dir=self.tmpdir,
        )
        log_food_entry(
            dt_str="15-04-2026 12:00", food="Rice", qty=2, unit="100g",
            protein_per_unit=2.7, fat_per_unit=0.3, carbs_per_unit=28,
            kcal_per_unit=130, source="cache_lookup", confidence=0.95,
            log_dir=self.tmpdir,
        )
        log_food_entry(
            dt_str="16-04-2026 09:00", food="Toast", qty=1, unit="slice",
            protein_per_unit=2.5, fat_per_unit=0.8, carbs_per_unit=14,
            kcal_per_unit=75, source="text_estimate", confidence=0.7,
            log_dir=self.tmpdir,
        )

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_returns_correct_totals(self):
        result = get_daily_totals("15-04-2026", self.tmpdir)
        assert result["count"] == 2
        assert result["totals"]["kcal"] == 476  # 216 + 260
        assert result["totals"]["protein"] == 24.3  # 18.9 + 5.4

    def test_only_requested_date(self):
        result = get_daily_totals("16-04-2026", self.tmpdir)
        assert result["count"] == 1
        assert result["totals"]["kcal"] == 75

    def test_empty_day(self):
        result = get_daily_totals("20-04-2026", self.tmpdir)
        assert result["count"] == 0
        assert result["totals"]["kcal"] == 0

    def test_nonexistent_month(self):
        result = get_daily_totals("15-06-2026", self.tmpdir)
        assert result["count"] == 0
        assert result["entries"] == []

    def test_entries_have_food_details(self):
        result = get_daily_totals("15-04-2026", self.tmpdir)
        assert len(result["entries"]) == 2
        foods = [e["food"] for e in result["entries"]]
        assert "Eggs" in foods
        assert "Rice" in foods

    def test_invalid_date(self):
        result = get_daily_totals("2026-04-15", self.tmpdir)
        assert "error" in result


class TestParseWeightUnit:
    """Test the weight/volume unit parser."""

    def test_grams(self):
        assert _parse_weight_unit("g") == (1.0, "g")

    def test_100g(self):
        assert _parse_weight_unit("100g") == (100.0, "g")

    def test_kg_to_grams(self):
        assert _parse_weight_unit("kg") == (1000.0, "g")

    def test_ml(self):
        assert _parse_weight_unit("ml") == (1.0, "ml")

    def test_100ml(self):
        assert _parse_weight_unit("100ml") == (100.0, "ml")

    def test_litre_to_ml(self):
        assert _parse_weight_unit("l") == (1000.0, "ml")

    def test_named_units_return_none(self):
        for u in ["serving", "cortado", "egg", "cup", "slice", "pint", ""]:
            assert _parse_weight_unit(u) is None


class TestResolveLogUnits:
    """Test deterministic unit normalization (the kefir/cashew 100x bug fix)."""

    def test_cache_lookup_grams_to_100g_overrides_model_rates(self):
        # cashew nuts is cached per-100g; user said 40g. Model passes GARBAGE rates.
        matches = search_foods("cashew nuts", PERSONAL, POPULAR)
        assert matches, "cashew nuts must exist in cache for this test"
        entry = matches[0]
        assert entry["unit"] == "100g"

        out = resolve_log_units(
            food="cashew nuts", qty=40, unit="g",
            kcal_per_unit=999, protein_per_unit=999, fat_per_unit=999, carbs_per_unit=999,
            source="cache_lookup", personal_path=PERSONAL, popular_path=POPULAR,
        )
        assert out["unit"] == "100g"
        assert out["qty"] == 0.4
        # Cache rates override whatever the model passed
        assert out["kcal_per_unit"] == entry["kcal_per_unit"]
        assert out["kcal_per_unit"] != 999
        assert out["note"]  # a normalization note was recorded

    def test_cache_lookup_already_correct_is_idempotent(self):
        matches = search_foods("cashew nuts", PERSONAL, POPULAR)
        entry = matches[0]
        out = resolve_log_units(
            food="cashew nuts", qty=0.4, unit="100g",
            kcal_per_unit=entry["kcal_per_unit"], protein_per_unit=entry["protein_per_unit"],
            fat_per_unit=entry["fat_per_unit"], carbs_per_unit=entry["carbs_per_unit"],
            source="cache_lookup", personal_path=PERSONAL, popular_path=POPULAR,
        )
        assert out["unit"] == "100g"
        assert out["qty"] == 0.4
        assert out["note"] == ""  # no change

    def test_cache_lookup_named_unit_unchanged(self):
        # cortado has a named unit; qty stays, cache rates used
        out = resolve_log_units(
            food="cortado", qty=1, unit="cortado",
            kcal_per_unit=999, protein_per_unit=999, fat_per_unit=999, carbs_per_unit=999,
            source="cache_lookup", personal_path=PERSONAL, popular_path=POPULAR,
        )
        assert out["unit"] == "cortado"
        assert out["qty"] == 1
        assert out["kcal_per_unit"] == 30  # cache value, not 999

    def test_heuristic_photo_label_grams_per100g(self):
        # Cream of mushroom soup label: 400g, 53 kcal/100g — model wrongly used unit='g'
        out = resolve_log_units(
            food="Cream of mushroom soup", qty=400, unit="g",
            kcal_per_unit=53, protein_per_unit=1.6, fat_per_unit=2.8, carbs_per_unit=5.3,
            source="photo_label", personal_path=PERSONAL, popular_path=POPULAR,
        )
        assert out["unit"] == "100g"
        assert out["qty"] == 4.0
        assert out["note"]

    def test_heuristic_macro_sum_triggers(self):
        # 50g salted peanuts, per-100g macros — macro sum >> 1 triggers conversion
        out = resolve_log_units(
            food="Salted peanuts", qty=50, unit="g",
            kcal_per_unit=567, protein_per_unit=25.8, fat_per_unit=49.2, carbs_per_unit=16.1,
            source="text_estimate", personal_path=PERSONAL, popular_path=POPULAR,
        )
        assert out["unit"] == "100g"
        assert out["qty"] == 0.5

    def test_heuristic_kcal_branch_only(self):
        # macros zero but kcal > 9 per 'gram' → still per-100g
        out = resolve_log_units(
            food="qqzz-nonexistent", qty=100, unit="g",
            kcal_per_unit=50, protein_per_unit=0, fat_per_unit=0, carbs_per_unit=0,
            source="text_estimate", personal_path=PERSONAL, popular_path=POPULAR,
        )
        assert out["unit"] == "100g"
        assert out["qty"] == 1.0

    def test_heuristic_true_per_gram_unchanged(self):
        # Legit tiny per-gram values (kcal < 9, macro sum < 1) → leave alone
        out = resolve_log_units(
            food="qqzz-nonexistent", qty=50, unit="g",
            kcal_per_unit=0.5, protein_per_unit=0.02, fat_per_unit=0.01, carbs_per_unit=0.1,
            source="text_estimate", personal_path=PERSONAL, popular_path=POPULAR,
        )
        assert out["unit"] == "g"
        assert out["qty"] == 50
        assert out["note"] == ""

    def test_named_unit_estimate_untouched(self):
        # serving-based estimate with sane values → no change
        out = resolve_log_units(
            food="Spanish eggs", qty=1, unit="serving",
            kcal_per_unit=280, protein_per_unit=19, fat_per_unit=20, carbs_per_unit=2.5,
            source="cache_lookup", personal_path=PERSONAL, popular_path=POPULAR,
        )
        # whatever the cache says for the unit; qty preserved (named unit)
        assert out["qty"] == 1


class TestNormalizeFoodName:
    """Food names written to the monthly log use SENTENCE case.

    Enforced in the write path so every client (Veda, veda-clerk, future
    ones) shares one convention. Without it the same food splits by casing
    depending on how it was resolved — a cache hit yields the lowercase
    YAML key ("espresso") while an LLM estimate yields "Espresso" — and
    anything grouping by name double-counts.
    """

    def test_lowercase_gets_capitalized(self):
        assert normalize_food_name("espresso") == "Espresso"
        assert normalize_food_name("small cappuccino") == "Small cappuccino"

    def test_already_capitalized_unchanged(self):
        assert normalize_food_name("Espresso") == "Espresso"
        assert normalize_food_name("Scrambled eggs") == "Scrambled eggs"

    def test_acronyms_and_brands_preserved(self):
        # Title-casing would wreck these — only the first char is touched.
        assert normalize_food_name("MCT C8 oil") == "MCT C8 oil"
        assert normalize_food_name("Pret nicoise salad") == "Pret nicoise salad"

    def test_interior_case_never_touched(self):
        assert normalize_food_name("greek Yogurt WITH honey") == "Greek Yogurt WITH honey"

    def test_whitespace_trimmed(self):
        assert normalize_food_name("  espresso  ") == "Espresso"

    def test_empty_and_none_safe(self):
        assert normalize_food_name("") == ""
        assert normalize_food_name("   ") == ""
        assert normalize_food_name(None) == ""

    def test_non_alpha_first_char_unchanged(self):
        assert normalize_food_name("100g cheese") == "100g cheese"


class TestLogFoodEntryNormalizesName:
    """The write path applies the convention — clients need not remember."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _log(self, food):
        return log_food_entry(
            "15-03-2026 08:30", food, 1, "cup",
            protein_per_unit=1, fat_per_unit=1, carbs_per_unit=1,
            kcal_per_unit=10, source="cache_lookup", confidence=0.95,
            log_dir=self.tmpdir,
        )

    def test_lowercase_name_written_capitalized(self):
        result = self._log("espresso")
        assert result["entry"]["food"] == "Espresso"
        written = (Path(self.tmpdir) / "2026-03.md").read_text()
        assert "| Espresso" in written
        assert "| espresso" not in written

    def test_acronym_survives_the_write_path(self):
        result = self._log("MCT C8 oil")
        assert result["entry"]["food"] == "MCT C8 oil"
