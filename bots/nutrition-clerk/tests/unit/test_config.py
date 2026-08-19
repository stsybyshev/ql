from __future__ import annotations

import os

import pytest

from nutrition_clerk.config import Config, ModelProfile, ModelSettings, load_config


def test_defaults_have_expected_profiles():
    settings = ModelSettings()
    assert "cloud_haiku" in settings.profiles
    assert "local_gemma" in settings.profiles
    assert settings.default == "cloud_haiku"
    assert settings.active_profile().model == "anthropic/claude-haiku-4-5"


def test_active_profile_respects_env(monkeypatch):
    settings = ModelSettings()
    monkeypatch.setenv("NUTRITION_CLERK_PROFILE", "local_gemma")
    assert settings.active_profile_name() == "local_gemma"
    assert settings.active_profile().model.startswith("ollama_chat/")


def test_unknown_profile_raises(monkeypatch):
    settings = ModelSettings()
    monkeypatch.setenv("NUTRITION_CLERK_PROFILE", "does-not-exist")
    with pytest.raises(KeyError):
        settings.active_profile()


def test_load_config_from_toml(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[bot]
telegram_token = "abc"
allowed_chat_ids = [1, 2]
channel = "telegram"

[paths]
state_dir = "~/.local/state/x"

[models]
default = "custom"

[models.profiles.custom]
model = "anthropic/claude-fake"
api_key_env = "FAKE_KEY"
"""
    )
    monkeypatch.delenv("NUTRITION_CLERK_PROFILE", raising=False)
    config = load_config(cfg_path)
    assert config.bot.telegram_token == "abc"
    assert config.bot.allowed_chat_ids == [1, 2]
    assert config.bot.channel == "telegram"
    assert config.models.default == "custom"
    assert config.models.active_profile().model == "anthropic/claude-fake"


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert isinstance(cfg, Config)
    assert cfg.bot.channel == "stub"
    assert cfg.bot.allowed_chat_ids == []
