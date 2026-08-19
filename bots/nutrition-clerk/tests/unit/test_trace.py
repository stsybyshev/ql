"""Unit tests for workflow.trace — no LLM, no MCP."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nutrition_clerk.workflow import trace


def _cfg(tmp_path: Path, **kw) -> trace.TraceConfig:
    defaults = dict(enabled=True, path=tmp_path / "turns.jsonl",
                    record_payloads=True, max_payload_chars=100)
    defaults.update(kw)
    return trace.TraceConfig(**defaults)


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_writes_one_record_per_turn(tmp_path):
    cfg = _cfg(tmp_path)
    with trace.turn(chat_id=1, msg_id=2, text="hi", config=cfg):
        trace.record("router", route="food")
    recs = _read(cfg.path)
    assert len(recs) == 1
    r = recs[0]
    assert r["chat_id"] == 1 and r["msg_id"] == 2
    assert r["input"]["text"] == "hi"
    assert r["nodes"][0]["node"] == "router"
    assert r["turn_id"].startswith("t-")
    assert r["total_ms"] >= 0


def test_appends_across_turns(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(3):
        with trace.turn(chat_id=1, msg_id=i, text=f"m{i}", config=cfg):
            pass
    assert len(_read(cfg.path)) == 3


def test_reply_recorded(tmp_path):
    cfg = _cfg(tmp_path)
    with trace.turn(chat_id=1, msg_id=1, text="hi", config=cfg) as t:
        t.reply = "Logged: 1 apple"
    assert _read(cfg.path)[0]["reply"] == "Logged: 1 apple"


def test_record_written_even_when_turn_raises(tmp_path):
    """The failure case is the one we most want to inspect."""
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        with trace.turn(chat_id=1, msg_id=1, text="boom", config=cfg):
            trace.record("extractor", tokens={"in": 5})
            raise ValueError("kaboom")
    recs = _read(cfg.path)
    assert len(recs) == 1
    assert "kaboom" in recs[0]["error"]
    assert recs[0]["nodes"][0]["node"] == "extractor"   # partial work preserved


def test_node_timer_records_duration(tmp_path):
    cfg = _cfg(tmp_path)
    with trace.turn(chat_id=1, msg_id=1, text="x", config=cfg):
        with trace.node("mcp.log_food", args={"a": 1}) as n:
            n["result"] = {"status": "ok"}
    node = _read(cfg.path)[0]["nodes"][0]
    assert node["node"] == "mcp.log_food"
    assert node["ms"] >= 0
    assert node["result"] == {"status": "ok"}


def test_node_timer_records_on_exception(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(RuntimeError):
        with trace.turn(chat_id=1, msg_id=1, text="x", config=cfg):
            with trace.node("mcp.log_food"):
                raise RuntimeError("mcp down")
    node = _read(cfg.path)[0]["nodes"][0]
    assert "mcp down" in node["error"]


def test_payloads_suppressed_when_disabled(tmp_path):
    cfg = _cfg(tmp_path, record_payloads=False)
    with trace.turn(chat_id=1, msg_id=1, text="x", config=cfg):
        trace.record("extractor", prompt="secret prompt", response="resp", tokens={"in": 1})
    node = _read(cfg.path)[0]["nodes"][0]
    assert "prompt" not in node and "response" not in node
    assert node["tokens"] == {"in": 1}   # non-payload fields still kept


def test_long_payloads_truncated(tmp_path):
    cfg = _cfg(tmp_path, max_payload_chars=50)
    with trace.turn(chat_id=1, msg_id=1, text="x", config=cfg):
        trace.record("extractor", prompt="A" * 500)
    prompt = _read(cfg.path)[0]["nodes"][0]["prompt"]
    assert len(prompt) < 200
    assert "truncated" in prompt


def test_disabled_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path, enabled=False)
    with trace.turn(chat_id=1, msg_id=1, text="x", config=cfg):
        trace.record("router", route="food")
    assert not cfg.path.exists()


def test_record_outside_turn_is_a_noop():
    """Must not raise when no turn is active (e.g. called from a unit test)."""
    trace.record("orphan", foo=1)   # no exception


def test_tracing_never_breaks_a_turn(tmp_path):
    """An unserialisable value must not propagate out of the turn."""
    cfg = _cfg(tmp_path)

    class Unserialisable:
        def __repr__(self):
            return "<obj>"

    with trace.turn(chat_id=1, msg_id=1, text="x", config=cfg) as t:
        trace.record("weird", thing=Unserialisable())
        t.reply = "fine"
    # default=str in the writer keeps this readable rather than exploding
    assert _read(cfg.path)[0]["reply"] == "fine"


def test_photos_recorded_as_paths(tmp_path):
    cfg = _cfg(tmp_path)
    with trace.turn(chat_id=1, msg_id=1, text="x",
                    photos=[Path("/tmp/a.jpg"), Path("/tmp/b.jpg")], config=cfg):
        pass
    assert _read(cfg.path)[0]["input"]["photos"] == ["/tmp/a.jpg", "/tmp/b.jpg"]


def test_nested_turns_restore_context(tmp_path):
    """contextvar token reset — a turn must not leak into the next one."""
    cfg = _cfg(tmp_path)
    with trace.turn(chat_id=1, msg_id=1, text="outer", config=cfg):
        assert trace.current() is not None
    assert trace.current() is None
