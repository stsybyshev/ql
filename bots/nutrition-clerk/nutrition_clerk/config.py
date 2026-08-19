import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class BotSettings(BaseModel):
    telegram_token: str = ""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    channel: Literal["stub", "telegram"] = "stub"


class PathSettings(BaseModel):
    state_dir: Path = Path("~/.local/state/nutrition-clerk")

    def resolved_state_dir(self) -> Path:
        return self.state_dir.expanduser().resolve()


class ModelProfile(BaseModel):
    model: str
    api_base: str | None = None
    api_key_env: str | None = None


class RoutingRule(BaseModel):
    """One rule in the ordered routing list. First match wins.

    A rule matches when BOTH conditions hold:
      - `pattern` (if set) matches the message text (case-insensitive regex.search)
      - `requires_photo` (if set) matches whether the message has attached photos:
          True  -> only match when photos are attached
          False -> only match when NO photos are attached
          None  -> don't care about photos
    """

    name: str
    profile: str
    pattern: str | None = None
    requires_photo: bool | None = None


class ModelSettings(BaseModel):
    default: str = "cloud_haiku"
    profiles: dict[str, ModelProfile] = Field(
        default_factory=lambda: {
            "cloud_haiku": ModelProfile(
                model="anthropic/claude-haiku-4-5",
                api_key_env="ANTHROPIC_API_KEY",
            ),
            "cloud_sonnet": ModelProfile(
                model="anthropic/claude-sonnet-5",
                api_key_env="ANTHROPIC_API_KEY",
            ),
            "local_gemma": ModelProfile(
                model="ollama_chat/gemma4:e4b",
                api_base="http://localhost:11434",
            ),
        }
    )
    # Ordered list; first match wins. Empty (default) = always use `default` profile.
    routing: list[RoutingRule] = Field(default_factory=list)

    def active_profile_name(self) -> str:
        return os.environ.get("NUTRITION_CLERK_PROFILE", self.default)

    def active_profile(self) -> ModelProfile:
        name = self.active_profile_name()
        if name not in self.profiles:
            raise KeyError(
                f"model profile {name!r} not defined; available: {list(self.profiles)}"
            )
        return self.profiles[name]


class MCPFoodSettings(BaseModel):
    """Points at the food-tracker MCP server (existing repo, not modified)."""

    project_dir: Path = Path(
        "/home/stan/dev/ql/skills/nutrition-tracker/mcp-server/food-tracker"
    )
    # Optional overrides. Leave None to use the MCP server's own defaults
    # (which point at ~/.openclaw/workspace/... — the same files Veda uses).
    personal_foods_path: Path | None = None
    popular_foods_path: Path | None = None
    food_log_dir: Path | None = None

    def env_overrides(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.personal_foods_path:
            env["PERSONAL_FOODS_PATH"] = str(self.personal_foods_path.expanduser())
        if self.popular_foods_path:
            env["POPULAR_FOODS_PATH"] = str(self.popular_foods_path.expanduser())
        if self.food_log_dir:
            env["FOOD_LOG_DIR"] = str(self.food_log_dir.expanduser())
        return env

    def resolved_log_dir(self) -> Path:
        """Resolve the log dir even if not explicitly configured.

        Must return the SAME path the MCP server uses; otherwise `recent_meals`
        would read a different file than `log_food` writes to. The default
        matches the MCP server's own FOOD_LOG_DIR default.
        """
        if self.food_log_dir:
            return self.food_log_dir.expanduser()
        return Path("~/.openclaw/workspace/food-tracker").expanduser()

    def resolved_personal_foods_path(self) -> Path:
        """Resolve personal-foods.yaml even if not explicitly configured.

        Must match the MCP server's PERSONAL_FOODS_PATH default so our
        in-process fuzzy fallback reads the same file the MCP writes to.
        """
        if self.personal_foods_path:
            return self.personal_foods_path.expanduser()
        return Path("~/.openclaw/workspace/food-tracker/personal-foods.yaml").expanduser()

    def resolved_popular_foods_path(self) -> Path:
        """Resolve popular-foods.yaml even if not explicitly configured."""
        if self.popular_foods_path:
            return self.popular_foods_path.expanduser()
        return Path(
            "~/.openclaw/workspace/skills/openclaw-food-tracker/references/popular-foods.yaml"
        ).expanduser()


class MCPSettings(BaseModel):
    food_tracker: MCPFoodSettings = Field(default_factory=MCPFoodSettings)


class NodeSettings(BaseModel):
    """Per-node model selection (N6).

    Each LLM call in the workflow can use a different profile. Empty string
    means "fall back to models.default / NUTRITION_CLERK_PROFILE", so a config
    that omits [nodes] entirely behaves exactly as before.

    Rationale per node:
    - extractor: text-only parsing. Cheap models do fine (Haiku; Gemma works
      at ~90% reliability — see scripts/compare_vision_models.py).
    - vision: label OCR. Haiku 4.5 matched Sonnet 5 exactly on real labels at
      1/3 the cost, so Haiku is the default.
    - knowledge: short "estimate macros for X" calls. Cheapest tier is fine.
    """

    extractor_profile: str = ""
    vision_profile: str = "cloud_haiku"
    knowledge_profile: str = ""

    def resolve(self, field_value: str, fallback: str) -> str:
        return field_value or fallback


class ContextSettings(BaseModel):
    """Per-chat conversation context (workflow/context.py)."""

    # A gap longer than this starts a fresh context: recent-meals ring cleared,
    # any pending clarification dropped. 16h spans a normal waking day, so
    # breakfast and dinner share one session but yesterday never leaks in.
    inactivity_timeout_hours: float = 16.0
    # How many recently-logged rows stay available for "save the X I had".
    recent_entries_ring_size: int = 10


class TracingSettings(BaseModel):
    """Per-turn JSONL tracing — see workflow/trace.py."""

    enabled: bool = True
    # Relative names resolve under paths.state_dir.
    file: Path = Path("turns.jsonl")
    # Capture the actual prompts and model responses. The main reason tracing
    # exists; turn off if the log's size or contents become a concern.
    record_payloads: bool = True
    max_payload_chars: int = 4000
    # Copy photos out of the channel's temp dir when a turn fails, so the
    # failure can be replayed later.
    retain_failed_photos: bool = True

    def resolved_path(self, state_dir: Path) -> Path:
        p = self.file.expanduser()
        return p if p.is_absolute() else (state_dir / p)


class Config(BaseModel):
    bot: BotSettings = Field(default_factory=BotSettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    nodes: NodeSettings = Field(default_factory=NodeSettings)
    tracing: TracingSettings = Field(default_factory=TracingSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)


def _default_config_path() -> Path:
    env = os.environ.get("NUTRITION_CLERK_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path("~/.config/nutrition-clerk/config.toml").expanduser()


def load_config(path: Path | None = None) -> Config:
    """Load config from a TOML file. Missing file returns defaults (stub channel)."""
    path = path or _default_config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Config(**data)
