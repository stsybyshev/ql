from __future__ import annotations

import logging
import os
from typing import Sequence

from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from mcp import StdioServerParameters

from nutrition_clerk.config import MCPFoodSettings

log = logging.getLogger("nutrition_clerk.tools.mcp_food")

# Hide the `eat` echo stub — that endpoint exists for a slash-command handshake
# unrelated to logging and would only confuse the LLM.
FOOD_TOOL_ALLOWLIST: tuple[str, ...] = (
    "lookup_food",
    "add_personal_food",
    "log_food",
    "get_todays_totals",
)


def build_food_mcp_toolset(
    settings: MCPFoodSettings,
    *,
    tool_allowlist: Sequence[str] = FOOD_TOOL_ALLOWLIST,
    startup_timeout: float = 30.0,
) -> McpToolset:
    """Return an McpToolset that spawns the food-tracker server as a subprocess.

    The server is launched via `uv run --project <food-tracker>` so it uses its
    own pinned venv (including its `mcp` and `pyyaml` deps), independent of
    nutrition-clerk's venv. Env vars for path overrides are passed through.
    """
    project_dir = str(settings.project_dir)
    env = {**os.environ, **settings.env_overrides()}
    log.info(
        "food MCP: project=%s overrides=%s allowlist=%s",
        project_dir,
        settings.env_overrides(),
        list(tool_allowlist),
    )
    server_params = StdioServerParameters(
        command="uv",
        args=[
            "run",
            "--project",
            project_dir,
            "python",
            f"{project_dir}/server.py",
        ],
        env=env,
    )
    connection = StdioConnectionParams(
        server_params=server_params,
        timeout=startup_timeout,
    )
    return McpToolset(
        connection_params=connection,
        tool_filter=list(tool_allowlist),
    )
