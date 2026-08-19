from __future__ import annotations

from pathlib import Path

from nutrition_clerk.config import MCPFoodSettings, MCPSettings


def test_defaults_point_at_repo_food_tracker():
    settings = MCPSettings()
    assert settings.food_tracker.project_dir == Path(
        "/home/stan/dev/ql/skills/nutrition-tracker/mcp-server/food-tracker"
    )
    # No path overrides by default — the MCP server uses its own defaults
    # (pointing at ~/.openclaw/workspace/... which Veda already writes to).
    assert settings.food_tracker.env_overrides() == {}


def test_env_overrides_expand_and_serialize(tmp_path):
    personal = tmp_path / "personal.yaml"
    popular = tmp_path / "popular.yaml"
    log_dir = tmp_path / "logs"
    settings = MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=popular,
        food_log_dir=log_dir,
    )
    env = settings.env_overrides()
    assert env["PERSONAL_FOODS_PATH"] == str(personal)
    assert env["POPULAR_FOODS_PATH"] == str(popular)
    assert env["FOOD_LOG_DIR"] == str(log_dir)


def test_env_overrides_partial():
    settings = MCPFoodSettings(personal_foods_path=Path("/x/p.yaml"))
    assert set(settings.env_overrides().keys()) == {"PERSONAL_FOODS_PATH"}
