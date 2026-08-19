"""In-memory per-chat context store.

Process-lifetime only. Lost on restart — intentional; food entries live in the
monthly MD file which is the source of truth. Context holds:
- `pending_clarification`: question asked last turn; injected as PREV_CLARIFY
  header into the extractor input next turn.
- `recent_entries`: bounded ring of recently-logged rows for SHAPE 6.4
  ("save the pomegranate juice I had") — wired up in N3.
- `last_touched`: for inactivity-timeout resets; conversation older than
  `_INACTIVITY_TIMEOUT` starts fresh so we don't drag stale context forward.

Not thread-safe. Fine for a single-user personal bot processing sequentially.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

log = logging.getLogger("nutrition_clerk.workflow.context")

# Defaults; overridden at startup from [context] in config.toml via configure().
_INACTIVITY_TIMEOUT = timedelta(hours=16)
_RECENT_RING_SIZE = 10


def configure(*, inactivity_timeout_hours: float, recent_ring_size: int) -> None:
    """Apply [context] config. Called once from build_handler at startup.

    Module-level rather than per-context because the store itself is a
    process-wide singleton keyed on chat_id.
    """
    global _INACTIVITY_TIMEOUT, _RECENT_RING_SIZE
    _INACTIVITY_TIMEOUT = timedelta(hours=inactivity_timeout_hours)
    _RECENT_RING_SIZE = recent_ring_size
    log.info(
        "chat context: inactivity timeout %sh, recent ring %d",
        inactivity_timeout_hours, recent_ring_size,
    )


@dataclass
class ChatContext:
    chat_id: int
    pending_clarification: str | None = None
    # Entries from the previous turn that could NOT be resolved (ambiguous
    # cache hits). Serialised ExtractedEntry dicts, in message order; the
    # FIRST one is what `pending_clarification` is asking about.
    #
    # We deliberately store the unresolved ENTRIES rather than the original
    # message text. Replaying the whole message on resume caused the entries
    # that already logged fine to be logged a second time (and, because the
    # same ambiguity recurred, to loop forever adding duplicates each turn).
    pending_entries: list[dict] = field(default_factory=list)
    # Set ONLY for extractor-emitted clarifications ("some chia" — too vague
    # to produce any entry). There is nothing to queue in that case, so the
    # resume falls back to replaying the original message. Safe here, unlike
    # the orchestrator case, because nothing was logged that turn.
    pending_original_message: str | None = None
    # maxlen read at construction time so configure() applies to new contexts.
    recent_entries: deque = field(default_factory=lambda: deque(maxlen=_RECENT_RING_SIZE))
    last_touched: datetime = field(default_factory=datetime.now)

    def touch(self) -> None:
        self.last_touched = datetime.now()

    def clear_pending(self) -> None:
        self.pending_clarification = None
        self.pending_entries = []
        self.pending_original_message = None


_STORE: dict[int, ChatContext] = {}


def get_context(chat_id: int, now: datetime | None = None) -> ChatContext:
    """Return the ChatContext for a chat, resetting if inactive too long.

    Callers should treat the returned object as mutable and call `touch()`
    before finishing the turn (graph.py handles this).
    """
    now = now or datetime.now()
    ctx = _STORE.get(chat_id)
    if ctx is None or (now - ctx.last_touched) > _INACTIVITY_TIMEOUT:
        if ctx is not None:
            log.info(
                "chat %s: idle >%s — starting fresh context (drop %d recent entries, %s pending)",
                chat_id,
                _INACTIVITY_TIMEOUT,
                len(ctx.recent_entries),
                "1" if ctx.pending_clarification else "0",
            )
        ctx = ChatContext(chat_id=chat_id, last_touched=now)
        _STORE[chat_id] = ctx
    return ctx


def _reset_all() -> None:
    """Test-only: clear the process-wide store between tests."""
    _STORE.clear()
