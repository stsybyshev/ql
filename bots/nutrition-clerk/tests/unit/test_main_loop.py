from __future__ import annotations

import pytest

from nutrition_clerk.channels.base import InboundEvent
from nutrition_clerk.channels.stub import StubChannel
from nutrition_clerk.config import BotSettings, Config, PathSettings
from nutrition_clerk.main import echo_handler, run
from nutrition_clerk.persistence.idempotency import Idempotency


def _make_config(tmp_path, allowed_chat_ids):
    return Config(
        bot=BotSettings(channel="stub", allowed_chat_ids=allowed_chat_ids),
        paths=PathSettings(state_dir=tmp_path),
    )


@pytest.mark.asyncio
async def test_echo_only_replies_to_allowlisted_chat(tmp_path):
    config = _make_config(tmp_path, allowed_chat_ids=[100])
    channel = StubChannel()
    await channel.feed(InboundEvent(chat_id=100, msg_id=1, text="hi", update_id=10))
    await channel.feed(InboundEvent(chat_id=999, msg_id=2, text="stranger", update_id=11))
    await channel.close()

    await run(config, channel, echo_handler)

    assert channel.sent == [(100, "echo: hi")]


@pytest.mark.asyncio
async def test_duplicate_msg_id_dropped_across_restart(tmp_path):
    config = _make_config(tmp_path, allowed_chat_ids=[100])

    # First run — process msg 1 fully.
    channel1 = StubChannel()
    await channel1.feed(InboundEvent(chat_id=100, msg_id=1, text="first", update_id=10))
    await channel1.close()
    await run(config, channel1, echo_handler)
    assert channel1.sent == [(100, "echo: first")]

    # Second run — the platform redelivers msg 1 alongside a new msg 2.
    # Only msg 2 should get a reply.
    channel2 = StubChannel()
    await channel2.feed(InboundEvent(chat_id=100, msg_id=1, text="first", update_id=10))
    await channel2.feed(InboundEvent(chat_id=100, msg_id=2, text="second", update_id=11))
    await channel2.close()
    await run(config, channel2, echo_handler)
    assert channel2.sent == [(100, "echo: second")]

    # Offset was advanced.
    inbox = Idempotency(tmp_path / "inbox.json")
    assert inbox.offset == 12
    assert inbox.seen(1)
    assert inbox.seen(2)


@pytest.mark.asyncio
async def test_handler_exception_does_not_commit(tmp_path):
    """If the handler crashes, we must NOT ack — the platform should redeliver."""
    config = _make_config(tmp_path, allowed_chat_ids=[100])

    async def failing(_event):
        raise RuntimeError("boom")

    channel1 = StubChannel()
    await channel1.feed(InboundEvent(chat_id=100, msg_id=7, text="hi", update_id=1))
    await channel1.close()
    await run(config, channel1, failing)
    assert channel1.sent == []

    inbox = Idempotency(tmp_path / "inbox.json")
    assert not inbox.seen(7)
    assert inbox.offset == 0

    # Recovery run: same message redelivered, this time succeeds.
    channel2 = StubChannel()
    await channel2.feed(InboundEvent(chat_id=100, msg_id=7, text="hi", update_id=1))
    await channel2.close()
    await run(config, channel2, echo_handler)
    assert channel2.sent == [(100, "echo: hi")]


@pytest.mark.asyncio
async def test_empty_allowlist_drops_everything(tmp_path):
    config = _make_config(tmp_path, allowed_chat_ids=[])
    channel = StubChannel()
    await channel.feed(InboundEvent(chat_id=100, msg_id=1, text="hi", update_id=10))
    await channel.close()

    await run(config, channel, echo_handler)

    assert channel.sent == []
