"""Unit tests for workflow.context — pending_clarification + inactivity timeout."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from nutrition_clerk.workflow import context as ctx_mod


@pytest.fixture(autouse=True)
def _reset_store():
    ctx_mod._reset_all()
    # Pin the timeout for these tests instead of inheriting whatever the
    # shipped default is — otherwise changing [context] in config breaks them.
    ctx_mod.configure(inactivity_timeout_hours=4, recent_ring_size=10)
    yield
    ctx_mod._reset_all()


def test_first_call_creates_context():
    ctx = ctx_mod.get_context(chat_id=42)
    assert ctx.chat_id == 42
    assert ctx.pending_clarification is None
    assert list(ctx.recent_entries) == []


def test_second_call_returns_same_instance():
    a = ctx_mod.get_context(chat_id=42)
    a.pending_clarification = "Q?"
    b = ctx_mod.get_context(chat_id=42)
    assert a is b
    assert b.pending_clarification == "Q?"


def test_different_chats_isolated():
    a = ctx_mod.get_context(chat_id=1)
    b = ctx_mod.get_context(chat_id=2)
    a.pending_clarification = "for chat 1"
    assert b.pending_clarification is None


def test_inactivity_timeout_resets_context():
    start = datetime(2026, 8, 16, 10, 0)
    ctx = ctx_mod.get_context(chat_id=1, now=start)
    ctx.pending_clarification = "carry-over"
    ctx.recent_entries.append({"food": "apple"})

    # Well past the 4h timeout — new call gets a fresh context.
    later = start + timedelta(hours=5)
    fresh = ctx_mod.get_context(chat_id=1, now=later)
    assert fresh.pending_clarification is None
    assert list(fresh.recent_entries) == []


def test_inactivity_within_window_preserves_context():
    start = datetime(2026, 8, 16, 10, 0)
    ctx = ctx_mod.get_context(chat_id=1, now=start)
    ctx.pending_clarification = "carry-over"
    ctx.touch()   # simulate mid-turn touch

    # 30 min later — still within 4h window.
    later = start + timedelta(minutes=30)
    same = ctx_mod.get_context(chat_id=1, now=later)
    assert same.pending_clarification == "carry-over"
    assert same is ctx
