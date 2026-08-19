from __future__ import annotations

import json

from nutrition_clerk.persistence.idempotency import MAX_SEEN, Idempotency


def test_fresh_store_defaults(tmp_path):
    store = Idempotency(tmp_path / "inbox.json")
    assert store.offset == 0
    assert not store.seen(123)


def test_commit_persists_and_reloads(tmp_path):
    path = tmp_path / "inbox.json"
    store = Idempotency(path)
    store.commit(offset=42, msg_id=101)
    store.commit(offset=43, msg_id=102)

    reloaded = Idempotency(path)
    assert reloaded.offset == 43
    assert reloaded.seen(101)
    assert reloaded.seen(102)
    assert not reloaded.seen(999)


def test_offset_is_monotonic(tmp_path):
    store = Idempotency(tmp_path / "inbox.json")
    store.commit(offset=50, msg_id=1)
    store.commit(offset=10, msg_id=2)  # out-of-order — should not regress offset
    assert store.offset == 50


def test_seen_ring_bounded(tmp_path):
    store = Idempotency(tmp_path / "inbox.json")
    for i in range(MAX_SEEN + 20):
        store.commit(offset=i, msg_id=i)
    assert not store.seen(0)  # evicted
    assert store.seen(MAX_SEEN + 19)  # newest kept


def test_duplicate_commit_noop(tmp_path):
    store = Idempotency(tmp_path / "inbox.json")
    store.commit(offset=1, msg_id=1)
    store.commit(offset=1, msg_id=1)  # duplicate
    with (tmp_path / "inbox.json").open() as f:
        data = json.load(f)
    assert data["seen"].count(1) == 1


def test_atomic_write_does_not_leave_tmp(tmp_path):
    path = tmp_path / "inbox.json"
    store = Idempotency(path)
    store.commit(offset=1, msg_id=1)
    assert not (tmp_path / "inbox.json.tmp").exists()
    assert path.exists()


def test_corrupt_file_treated_as_empty(tmp_path):
    path = tmp_path / "inbox.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{{")
    store = Idempotency(path)
    assert store.offset == 0
    assert not store.seen(1)
