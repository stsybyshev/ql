"""Per-turn tracing — one JSONL record per inbound message.

Motivation: the human-readable log tells you *that* the extractor ran and how
many tokens it used, but not what it was asked or what it said. Every bug found
so far (label-photo crash, silent multi-item drops, the small-cappuccino
mis-log) needed ad-hoc instrumentation added by hand to diagnose. This makes
that information always available.

Each turn appends one JSON object:

    {
      "turn_id": "t-3f9a21",
      "ts": "2026-08-16T11:08:03.412",
      "chat_id": 123, "msg_id": 4471,
      "input": {"text": "...", "photos": ["/path/a.jpg"]},
      "nodes": [
        {"node": "router", "ms": 0.2, "route": "food"},
        {"node": "extractor", "ms": 1240, "model": "claude-haiku-4-5",
         "tokens": {"in": 2364, "out": 140},
         "prompt": "...", "response": "..."},
        {"node": "mcp.log_food", "ms": 41, "args": {...}, "result": {...}}
      ],
      "reply": "Logged: ...",
      "error": null,
      "total_ms": 4310
    }

Design notes:
- A contextvar carries the active turn, so nodes record without every function
  signature having to thread a trace object through.
- Recording NEVER raises. A tracing bug must not break a turn — every entry
  point is wrapped.
- The record is written even when the turn raises; `error` carries the
  traceback. That is the case we most want to inspect.
- Payload capture is configurable and truncated, so a runaway prompt can't
  fill the disk.

Querying, once it's running:
    jq 'select(.error)' turns.jsonl                       # failed turns
    jq 'select(.nodes[] | .kind? == "unclear")' turns.jsonl
    jq '{id: .turn_id, ms: .total_ms}' turns.jsonl        # latency
"""
from __future__ import annotations

import json
import logging
import os
import time
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("nutrition_clerk.workflow.trace")


@dataclass
class TraceConfig:
    enabled: bool = True
    path: Path | None = None
    record_payloads: bool = True
    max_payload_chars: int = 4000


@dataclass
class TurnTrace:
    turn_id: str
    ts: str
    chat_id: int
    msg_id: int
    input: dict[str, Any]
    config: TraceConfig
    nodes: list[dict[str, Any]] = field(default_factory=list)
    reply: str | None = None
    error: str | None = None
    started: float = field(default_factory=time.perf_counter)

    def to_record(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "ts": self.ts,
            "chat_id": self.chat_id,
            "msg_id": self.msg_id,
            "input": self.input,
            "nodes": self.nodes,
            "reply": self.reply,
            "error": self.error,
            "total_ms": round((time.perf_counter() - self.started) * 1000, 1),
        }


_current: ContextVar[TurnTrace | None] = ContextVar("nutrition_clerk_turn_trace", default=None)


def current() -> TurnTrace | None:
    return _current.get()


def _truncate(value: Any, limit: int) -> Any:
    """Cap long strings so one runaway payload can't dominate the file."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…[truncated {len(value) - limit} chars]"
    return value


def record(node: str, **fields: Any) -> None:
    """Attach a node record to the active turn. Never raises."""
    try:
        trace = _current.get()
        if trace is None or not trace.config.enabled:
            return
        limit = trace.config.max_payload_chars
        clean: dict[str, Any] = {"node": node}
        for k, v in fields.items():
            if k in ("prompt", "response") and not trace.config.record_payloads:
                continue
            clean[k] = _truncate(v, limit)
        trace.nodes.append(clean)
    except Exception:  # pragma: no cover - tracing must never break a turn
        log.debug("trace.record failed for node %r", node, exc_info=True)


@contextmanager
def node(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Time a node and record it, even if the body raises.

    Yields a mutable dict — add fields to it inside the block and they land
    in the record:

        with trace.node("extractor", model="haiku") as n:
            result = await call()
            n["tokens"] = {"in": 10, "out": 2}
    """
    extra: dict[str, Any] = {}
    start = time.perf_counter()
    try:
        yield extra
    except Exception as exc:
        record(
            name,
            ms=round((time.perf_counter() - start) * 1000, 1),
            error=f"{type(exc).__name__}: {exc}",
            **{**fields, **extra},
        )
        raise
    else:
        record(
            name,
            ms=round((time.perf_counter() - start) * 1000, 1),
            **{**fields, **extra},
        )


@contextmanager
def turn(
    *,
    chat_id: int,
    msg_id: int,
    text: str,
    photos: list[Path] | None = None,
    config: TraceConfig | None = None,
) -> Iterator[TurnTrace]:
    """Scope one inbound message. Writes the record on exit, success or not."""
    cfg = config or TraceConfig(enabled=False)
    trace = TurnTrace(
        turn_id="t-" + uuid.uuid4().hex[:8],
        ts=datetime.now().isoformat(timespec="milliseconds"),
        chat_id=chat_id,
        msg_id=msg_id,
        input={
            "text": _truncate(text or "", cfg.max_payload_chars),
            "photos": [str(p) for p in (photos or [])],
        },
        config=cfg,
    )
    token = _current.set(trace)
    try:
        yield trace
    except Exception:
        trace.error = traceback.format_exc(limit=12)
        raise
    finally:
        _current.reset(token)
        _write(trace)


def _write(trace: TurnTrace) -> None:
    """Append the record as one JSONL line. Never raises."""
    cfg = trace.config
    if not cfg.enabled or cfg.path is None:
        return
    try:
        cfg.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(trace.to_record(), ensure_ascii=False, default=str)
        with cfg.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:  # pragma: no cover
        log.warning("could not write turn trace to %s", cfg.path, exc_info=True)
