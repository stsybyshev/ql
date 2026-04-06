"""Tests for food cache MCP server."""

import tempfile
import shutil
from pathlib import Path

import pytest

from food_cache import search_foods, append_personal_food, load_yaml, log_food_entry, get_daily_totals

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
