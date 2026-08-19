from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InboundEvent:
    """Channel-agnostic representation of one inbound user message."""

    chat_id: int
    msg_id: int
    text: str = ""
    photos: list[Path] = field(default_factory=list)
    update_id: int | None = None


class Channel(abc.ABC):
    """Async pluggable input/output channel (Telegram, WhatsApp, stub, ...)."""

    @abc.abstractmethod
    def inbound(self) -> AsyncIterator[InboundEvent]:
        """Yield inbound events as they arrive. Must be an async iterator."""

    @abc.abstractmethod
    async def send_text(self, chat_id: int, text: str) -> None:
        """Send a text reply to the given chat."""

    async def start(self) -> None:  # pragma: no cover - default no-op
        """Optional setup hook called before iteration."""

    async def stop(self) -> None:  # pragma: no cover - default no-op
        """Optional teardown hook."""
