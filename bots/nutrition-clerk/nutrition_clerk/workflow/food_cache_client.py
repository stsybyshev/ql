"""Lightweight warm MCP stdio client for the food-tracker server.

Spawns `python server.py` once at process start, keeps stdin/stdout open,
exchanges JSON-RPC. Async-friendly, one shared session for the process.

No ADK `McpToolset` involvement — we don't need agent lifecycle wrapping.
Public API mirrors the four food-tracker tools clerk actually uses.

Startup is lazy: the first tool call spawns the subprocess and initialises
the session. Subsequent calls reuse it (~1-2ms overhead vs ~1s cold start).
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from nutrition_clerk.config import MCPFoodSettings
from nutrition_clerk.workflow import trace

log = logging.getLogger("nutrition_clerk.workflow.food_cache_client")

# Minimum max(ratio, token_set_ratio) a fuzzy candidate must ALSO reach before
# its WRatio counts. See `_pair_score` — WRatio alone lets a single shared
# token carry a short query onto a long, unrelated name.
#
# Measured against the real cache (17-08-2026):
#   real typos      ssundubu-jigaye->sundubu 63, ->sundubu jjigae 82,
#                   capuccino->cappuccino 94, cashewnuts->cashew nuts 95
#   false positives red grapefruit->"Tuna, Red Kidney Beans and Celery
#                   Salad" 35, red grapefruit->grapes 50
# 60 sits in the gap. It is deliberately not higher: 70 cut
# "ssundubu-jigaye"->"sundubu" (63), a case this feature exists to handle.
_FUZZY_CORROBORATION_FLOOR = 60


class FoodCacheClient:
    """Warm MCP client for the food-tracker server.

    Lifetime: constructed once at pipeline build, `close()` called on shutdown.
    Not thread-safe (single-user personal bot); safe under sequential asyncio.
    """

    def __init__(self, settings: MCPFoodSettings) -> None:
        self._settings = settings
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._start_lock = asyncio.Lock()

    async def _ensure_started(self) -> ClientSession:
        if self._session is not None:
            return self._session
        async with self._start_lock:
            if self._session is not None:
                return self._session
            project = str(self._settings.project_dir)
            env = {**os.environ, **self._settings.env_overrides()}
            params = StdioServerParameters(
                command="uv",
                args=["run", "--project", project, "python", f"{project}/server.py"],
                env=env,
            )
            log.info(
                "starting food-tracker MCP subprocess: %s (overrides=%s)",
                project,
                self._settings.env_overrides(),
            )
            self._exit_stack = AsyncExitStack()
            read, write = await self._exit_stack.enter_async_context(stdio_client(params))
            session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
            return session

    async def close(self) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with trace.node(f"mcp.{tool_name}", args=arguments) as n:
            out = await self._call_inner(tool_name, arguments)
            n["result"] = out
            return out

    async def _call_inner(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        session = await self._ensure_started()
        result = await session.call_tool(tool_name, arguments=arguments)
        # MCP tool responses come back as `content: [TextContent | ...]`. The
        # food-tracker server returns JSON text in a single TextContent block.
        if result.isError:
            raise RuntimeError(
                f"food-tracker MCP tool {tool_name!r} failed: {result.content!r}"
            )
        # Prefer structured content if the server ships it; otherwise parse JSON text.
        if getattr(result, "structuredContent", None):
            data = result.structuredContent
            # server.py returns dicts directly; MCP wraps under a `result` key
            if isinstance(data, dict) and set(data.keys()) == {"result"}:
                return data["result"]
            return data
        # Fall back to text-content JSON.
        import json as _json

        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                return _json.loads(text)
        raise RuntimeError(f"food-tracker MCP tool {tool_name!r} returned no content")

    # --------- Tool wrappers (typed for clerk's convenience) ---------

    async def lookup_food(self, query: str) -> list[dict[str, Any]]:
        result = await self._call("lookup_food", {"query": query})
        # Server returns a list directly, but MCP structured content usually wraps.
        if isinstance(result, dict) and "result" in result:
            return result["result"]
        return result if isinstance(result, list) else []

    async def add_personal_food(
        self,
        *,
        name: str,
        unit: str,
        kcal_per_unit: float,
        protein_per_unit: float,
        fat_per_unit: float,
        carbs_per_unit: float,
        aliases: list[str] | None = None,
        qty_default: float = 1,
        notes: str = "",
    ) -> dict[str, Any]:
        return await self._call(
            "add_personal_food",
            {
                "name": name,
                "unit": unit,
                "kcal_per_unit": kcal_per_unit,
                "protein_per_unit": protein_per_unit,
                "fat_per_unit": fat_per_unit,
                "carbs_per_unit": carbs_per_unit,
                "aliases": aliases or [],
                "qty_default": qty_default,
                "notes": notes,
            },
        )

    async def log_food(
        self,
        *,
        datetime: str,
        food: str,
        qty: float,
        unit: str,
        kcal_per_unit: float,
        protein_per_unit: float,
        fat_per_unit: float,
        carbs_per_unit: float,
        source: str,
        confidence: float,
    ) -> dict[str, Any]:
        return await self._call(
            "log_food",
            {
                "datetime": datetime,
                "food": food,
                "qty": qty,
                "unit": unit,
                "kcal_per_unit": kcal_per_unit,
                "protein_per_unit": protein_per_unit,
                "fat_per_unit": fat_per_unit,
                "carbs_per_unit": carbs_per_unit,
                "source": source,
                "confidence": confidence,
            },
        )

    async def get_todays_totals(self, date: str) -> dict[str, Any]:
        return await self._call("get_todays_totals", {"date": date})

    # -----------------------------------------------------------------
    # In-process fuzzy fallback (N4.5). Does NOT round-trip to the MCP
    # subprocess — reads the same YAML files directly using the parser
    # bundled with food-tracker. Runs after `lookup_food` returns 0 hits.
    # -----------------------------------------------------------------

    def fuzzy_lookup(self, query: str, min_score: int = 85, top_n: int = 3) -> list[dict[str, Any]]:
        """Return up to top-N cache entries whose name or alias fuzzy-matches
        the query with score >= min_score. Personal entries beat popular
        entries at equal score (personal-first is a preserved invariant).

        Uses rapidfuzz.WRatio (case-insensitive). Same tokenised weighted
        ratio the orchestrator uses for multi-hit disambiguation.

        Results are shaped exactly like `lookup_food`'s output so the
        orchestrator can slot them into the same downstream logic without a
        branch.
        """
        import sys as _sys
        from rapidfuzz import fuzz

        # Reuse food-tracker's own load_yaml — handles missing files, returns [].
        food_tracker_dir = str(self._settings.project_dir.resolve())
        if food_tracker_dir not in _sys.path:
            _sys.path.insert(0, food_tracker_dir)
        from food_cache import load_yaml  # type: ignore

        personal = load_yaml(str(self._settings.resolved_personal_foods_path()))
        popular = load_yaml(str(self._settings.resolved_popular_foods_path()))

        q = (query or "").lower().strip()
        if not q:
            return []

        def _pair_score(candidate: str) -> int:
            """WRatio, but only when a non-partial metric also agrees.

            WRatio boosts partial matches when the two strings differ a lot in
            length, which makes it dangerously generous for a short query
            against a long name:

                "red grapefruit" vs "Tuna, Red Kidney Beans and Celery Salad"
                    WRatio 85.5  <- passes the 85 threshold
                    ratio 26, partial 38, token_set 35  <- obviously not a match

            One shared token ("red") was enough. So require a floor on
            max(ratio, token_set_ratio), which stays high for the real fuzzy
            cases this feature exists for — "ssundubu-jigaye" vs
            "sundubu jigaye" scores ratio 90 — and collapses for coincidental
            token overlap (35 above).
            """
            w = int(fuzz.WRatio(q, candidate))
            if w < min_score:
                return 0
            corroboration = max(
                int(fuzz.ratio(q, candidate)),
                int(fuzz.token_set_ratio(q, candidate)),
            )
            return w if corroboration >= _FUZZY_CORROBORATION_FLOOR else 0

        def _best_score(entry: dict) -> int:
            score = _pair_score((entry.get("name") or "").lower())
            for alias in entry.get("aliases", []) or []:
                score = max(score, _pair_score(str(alias).lower()))
            return score

        scored: list[tuple[int, int, dict, str]] = []
        # 2nd tuple element: 0 for personal, 1 for popular — sort ascending
        # so personal wins ties.
        for entry in personal:
            s = _best_score(entry)
            if s >= min_score:
                scored.append((s, 0, entry, "personal"))
        for entry in popular:
            s = _best_score(entry)
            if s >= min_score:
                scored.append((s, 1, entry, "seed"))

        scored.sort(key=lambda x: (-x[0], x[1]))

        results: list[dict[str, Any]] = []
        for _, _, entry, source in scored[:top_n]:
            results.append({
                "name": entry.get("name", ""),
                "aliases": entry.get("aliases", []),
                "qty_default": entry.get("qty_default", 1),
                "unit": entry.get("unit", "serving"),
                "kcal_per_unit": entry.get("kcal_per_unit", 0),
                "protein_per_unit": entry.get("protein_per_unit", 0),
                "fat_per_unit": entry.get("fat_per_unit", 0),
                "carbs_per_unit": entry.get("carbs_per_unit", 0),
                "notes": entry.get("notes", ""),
                "source": source,
            })
        return results
