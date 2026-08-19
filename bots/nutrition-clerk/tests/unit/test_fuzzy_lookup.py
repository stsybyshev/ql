"""Unit tests for FoodCacheClient.fuzzy_lookup — deterministic, no MCP."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nutrition_clerk.config import MCPFoodSettings
from nutrition_clerk.workflow.food_cache_client import FoodCacheClient


def _make_client(tmp_path: Path, personal_entries=None, popular_entries=None) -> FoodCacheClient:
    personal_path = tmp_path / "personal.yaml"
    personal_path.write_text(yaml.safe_dump(personal_entries or []))
    popular_path = tmp_path / "popular.yaml"
    popular_path.write_text(yaml.safe_dump(popular_entries or []))
    settings = MCPFoodSettings(
        personal_foods_path=personal_path,
        popular_foods_path=popular_path,
    )
    return FoodCacheClient(settings)


def _entry(name, kcal=100, aliases=None, unit="serving"):
    return {
        "name": name, "aliases": aliases or [], "qty_default": 1, "unit": unit,
        "kcal_per_unit": kcal, "protein_per_unit": 5,
        "fat_per_unit": 3, "carbs_per_unit": 10,
        "notes": "", "source": "seed",
    }


def test_typo_matches_sundubu(tmp_path):
    client = _make_client(tmp_path, personal_entries=[_entry("sundubu jigaye")])
    hits = client.fuzzy_lookup("ssundubu-jigaye")
    assert len(hits) == 1
    assert hits[0]["name"] == "sundubu jigaye"


def test_dash_vs_space(tmp_path):
    client = _make_client(tmp_path, personal_entries=[_entry("sundubu jigaye")])
    hits = client.fuzzy_lookup("sundubu-jigaye")
    assert len(hits) == 1


def test_case_insensitive(tmp_path):
    client = _make_client(tmp_path, personal_entries=[_entry("Manchego Cheese")])
    hits = client.fuzzy_lookup("manchego cheese")
    assert len(hits) == 1


def test_too_far_returns_nothing(tmp_path):
    client = _make_client(tmp_path, personal_entries=[_entry("sundubu jigaye")])
    hits = client.fuzzy_lookup("ssoondobu")   # heavily mangled — WRatio ~56
    assert hits == []


def test_alias_participates_in_scoring(tmp_path):
    client = _make_client(tmp_path, personal_entries=[
        _entry("dead-name-nobody-uses", aliases=["chia pudding"]),
    ])
    hits = client.fuzzy_lookup("chia pudn")   # matches alias, not name
    assert len(hits) == 1


def test_personal_beats_popular_at_tie(tmp_path):
    client = _make_client(
        tmp_path,
        personal_entries=[_entry("chia seeds", kcal=486)],
        popular_entries=[_entry("chia seeds", kcal=999)],  # same name, different macros
    )
    hits = client.fuzzy_lookup("chia seeds")
    # Personal comes first — its kcal wins the tiebreak.
    assert hits[0]["kcal_per_unit"] == 486
    assert hits[0]["source"] == "personal"


def test_top_n_limit(tmp_path):
    client = _make_client(tmp_path, personal_entries=[
        _entry(f"food_{i}") for i in range(10)
    ])
    hits = client.fuzzy_lookup("food_1", top_n=2)
    assert len(hits) <= 2


def test_empty_query_returns_empty(tmp_path):
    client = _make_client(tmp_path, personal_entries=[_entry("apple")])
    assert client.fuzzy_lookup("") == []
    assert client.fuzzy_lookup("   ") == []


def test_missing_files_return_empty(tmp_path):
    # Point at non-existent yaml files — load_yaml handles this by returning [].
    settings = MCPFoodSettings(
        personal_foods_path=tmp_path / "nope1.yaml",
        popular_foods_path=tmp_path / "nope2.yaml",
    )
    client = FoodCacheClient(settings)
    assert client.fuzzy_lookup("apple") == []
