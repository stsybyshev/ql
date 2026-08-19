from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from nutrition_clerk.channels.base import Channel, InboundEvent


class StubChannel(Channel):
    """In-process channel driven by an asyncio.Queue. For tests and dev echo."""

    def __init__(self) -> None:
        self._inbox: asyncio.Queue[InboundEvent | None] = asyncio.Queue()
        self.sent: list[tuple[int, str]] = []

    async def feed(self, event: InboundEvent) -> None:
        await self._inbox.put(event)

    async def close(self) -> None:
        await self._inbox.put(None)

    async def inbound(self) -> AsyncIterator[InboundEvent]:
        while True:
            event = await self._inbox.get()
            if event is None:
                return
            yield event

    async def send_text(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))
