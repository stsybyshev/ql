from __future__ import annotations

from nutrition_clerk.agents.pipeline import _wrap_with_meta
from nutrition_clerk.channels.base import InboundEvent


def test_wrap_prefixes_msg_id():
    event = InboundEvent(chat_id=1, msg_id=42, text="1 apple")
    wrapped = _wrap_with_meta(event)
    assert wrapped.startswith("[SYSTEM_META: telegram_msg_id=42]")
    assert wrapped.endswith("1 apple")


def test_wrap_handles_empty_text():
    event = InboundEvent(chat_id=1, msg_id=7)
    wrapped = _wrap_with_meta(event)
    assert "telegram_msg_id=7" in wrapped
    # Empty user text should not produce None in the output.
    assert "None" not in wrapped
