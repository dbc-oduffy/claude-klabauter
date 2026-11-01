"""BoundedStore: refusal past allowance, remediation text, no eviction."""

from __future__ import annotations

import pytest

from coordinator_core.resource_lifecycle.errors import AllowanceExceeded
from coordinator_core.resource_lifecycle.store import BoundedStore


def test_write_past_allowance_refused_and_history_intact(tmp_path):
    p = tmp_path / "s.jsonl"
    store = BoundedStore(p, max_bytes=40)
    store.write({"n": 1})
    store.write({"n": 2})
    before = p.read_bytes()
    with pytest.raises(AllowanceExceeded) as ei:
        store.write({"payload": "x" * 100})
    assert "max_bytes" in str(ei.value) and "archive" in str(ei.value)
    assert ei.value.remediation
    assert p.read_bytes() == before
    assert list(store.read()) == [{"n": 1}, {"n": 2}]


def test_exact_fit_accepted_and_one_more_byte_refused(tmp_path):
    p = tmp_path / "s.jsonl"
    line = len(b'{"n":1}\n')
    store = BoundedStore(p, max_bytes=line)
    store.write({"n": 1})
    with pytest.raises(AllowanceExceeded):
        store.write({"n": 2})
    assert BoundedStore(p, max_bytes=line - 1).size == line


def test_reopen_counts_existing_bytes(tmp_path):
    p = tmp_path / "s.jsonl"
    BoundedStore(p, max_bytes=100).write({"n": 1})
    again = BoundedStore(p, max_bytes=10)
    with pytest.raises(AllowanceExceeded):
        again.write({"n": 2})
    assert list(again.read()) == [{"n": 1}]


def test_no_destructive_api():
    for name in ("truncate", "rotate", "delete", "clear", "evict", "remove"):
        assert not hasattr(BoundedStore, name)
