"""Unit tests for the [nodes] per-node model config (N6)."""
from __future__ import annotations

from nutrition_clerk.config import Config, NodeSettings, load_config


def test_defaults_are_backwards_compatible():
    """No [nodes] section -> extractor/knowledge fall back, vision prefers Haiku."""
    nodes = NodeSettings()
    assert nodes.extractor_profile == ""
    assert nodes.knowledge_profile == ""
    assert nodes.vision_profile == "cloud_haiku"


def test_config_has_nodes_section():
    config = Config()
    assert isinstance(config.nodes, NodeSettings)


def test_nodes_loaded_from_toml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[nodes]
extractor_profile = "local_gemma"
vision_profile = "cloud_sonnet"
knowledge_profile = "cloud_haiku"
"""
    )
    monkeypatch.delenv("NUTRITION_CLERK_PROFILE", raising=False)
    config = load_config(cfg)
    assert config.nodes.extractor_profile == "local_gemma"
    assert config.nodes.vision_profile == "cloud_sonnet"
    assert config.nodes.knowledge_profile == "cloud_haiku"


def test_partial_nodes_section_keeps_defaults(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[nodes]
extractor_profile = "local_gemma"
"""
    )
    config = load_config(cfg)
    assert config.nodes.extractor_profile == "local_gemma"
    assert config.nodes.vision_profile == "cloud_haiku"   # default preserved
    assert config.nodes.knowledge_profile == ""


def test_resolve_helper():
    nodes = NodeSettings(extractor_profile="", vision_profile="cloud_sonnet")
    assert nodes.resolve(nodes.extractor_profile, "cloud_haiku") == "cloud_haiku"
    assert nodes.resolve(nodes.vision_profile, "cloud_haiku") == "cloud_sonnet"
