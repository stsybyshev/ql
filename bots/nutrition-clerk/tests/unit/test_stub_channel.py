from __future__ import annotations

import pytest

from nutrition_clerk.channels.base import InboundEvent
from nutrition_clerk.channels.stub import StubChannel


@pytest.mark.asyncio
async def test_stub_yields_events_in_order():
    channel = StubChannel()
    await channel.feed(InboundEvent(chat_id=1, msg_id=1, text="hi"))
    await channel.feed(InboundEvent(chat_id=1, msg_id=2, text="there"))
    await channel.close()

    received = [event async for event in channel.inbound()]
    assert [e.text for e in received] == ["hi", "there"]


@pytest.mark.asyncio
async def test_stub_send_text_records():
    channel = StubChannel()
    await channel.send_text(42, "hello")
    await channel.send_text(42, "again")
    assert channel.sent == [(42, "hello"), (42, "again")]
