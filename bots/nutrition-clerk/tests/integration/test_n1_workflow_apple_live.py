"""N1 spike: M3 '1 apple' cache-hit turn through the NEW workflow handler.

Purpose:
- Prove the new pipeline works end-to-end against real Haiku 4.5 + real MCP.
- Measure input tokens per turn — the whole justification for the pivot.
- Assertion: token cost drops from ~37k (old pipeline) to <5k (target).

Assertions mirror the old M3 test's user-facing shape (row in MD, source
enum, correct macros) but call the new handler.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("google.adk")

from nutrition_clerk.channels.base import InboundEvent  # noqa: E402
from nutrition_clerk.config import Config, MCPFoodSettings, MCPSettings  # noqa: E402
from nutrition_clerk.workflow import build_handler  # noqa: E402

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — skipping live LLM test",
)

REAL_POPULAR = Path(
    "/home/stan/.openclaw/workspace/skills/openclaw-food-tracker/references/popular-foods.yaml"
)
REAL_PERSONAL = Path("/home/stan/.openclaw/workspace/food-tracker/personal-foods.yaml")


@pytest.mark.asyncio
async def test_one_apple_end_to_end_via_workflow(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="nutrition_clerk.workflow.extractor")

    if not REAL_POPULAR.exists():
        pytest.skip(f"missing popular-foods.yaml at {REAL_POPULAR}")

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    personal = tmp_path / "personal.yaml"
    if REAL_PERSONAL.exists():
        shutil.copyfile(REAL_PERSONAL, personal)

    config = Config()
    config.mcp = MCPSettings(food_tracker=MCPFoodSettings(
        personal_foods_path=personal,
        popular_foods_path=REAL_POPULAR,
        food_log_dir=log_dir,
    ))

    handler, client = build_handler(config)
    try:
        reply = await handler(
            InboundEvent(chat_id=1, msg_id=1, text="just had 1 apple")
        )
    finally:
        await client.close()

    assert reply and reply != "(no response)"
    assert "apple" in reply.lower()

    # Row was appended
    today = date.today()
    md_path = log_dir / f"{today.year:04d}-{today.month:02d}.md"
    assert md_path.exists(), f"no monthly log at {md_path}"
    rows = [
        line for line in md_path.read_text().splitlines()
        if re.match(r"^\|\s*\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}\s*\|", line)
    ]
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}:\n" + "\n".join(rows)
    row = rows[0].lower()
    assert "apple" in row
    assert "cache_lookup" in row
    assert "telegram:" not in row  # source is the enum, not the msg_id

    # ---- N1 SPIKE ASSERTION: token cost target ----
    # Look for the extractor's per-call token count in captured logs.
    extractor_lines = [
        r.getMessage() for r in caplog.records
        if "extractor" in r.name and "in=" in r.getMessage()
    ]
    assert extractor_lines, (
        "expected extractor to log a token count line "
        '("extractor: in=X out=Y ..."); got no matching log records'
    )
    # Parse "extractor: in=N out=M ..."
    m = re.search(r"in=(\d+)\s+out=(\d+)", extractor_lines[-1])
    assert m, f"unexpected log line format: {extractor_lines[-1]!r}"
    tokens_in = int(m.group(1))
    tokens_out = int(m.group(2))
    print(f"\n[N1 SPIKE] extractor tokens_in={tokens_in} tokens_out={tokens_out}")
    print(f"[N1 SPIKE] target: <5000 input tokens (old pipeline was ~37000)")

    # This is the whole point of the pivot.
    assert tokens_in < 5000, (
        f"N1 spike token target FAILED: extractor used {tokens_in} input tokens "
        f"(target <5000). Old pipeline used ~37k. If this failed, stop and "
        f"investigate what's inflating the extractor's context."
    )
