"""A frozen filesystem must not hold a request-path caller past its deadline.

Each test holds the underlying open/write on an Event (no real sleep, all paths
under ``tmp_path``), asserts the caller returns within 500ms, then releases the
Event so the abandoned worker drains.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import atomic_append
from coordinator_core.bash_guards import chain_arrival_ledger as cal
from coordinator_core.hooks import postuse_advisory_dispatch as pad
from coordinator_core.ops.dispatch_emit import op as emit_op

CALLER_BOUND_SECS = 0.5
HOLD_CEILING_SECS = 30


class _Gate:
    """Blocks the stubbed fs call until released; records that it was entered."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def hold(self) -> None:
        self.entered.set()
        self.release.wait(HOLD_CEILING_SECS)


def _timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, time.monotonic() - t0


def test_append_line_windows_open_blocked_returns_to_caller(tmp_path, monkeypatch):
    gate = _Gate()
    monkeypatch.setattr(atomic_append, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_append, "_open_append_fd_windows", lambda path: gate.hold() or (_ for _ in ()).throw(OSError("released")))
    sink = tmp_path / "sink-record.jsonl"
    try:
        out, elapsed = _timed(lambda: atomic_append.append_line(sink, b"x\n"))
        assert gate.entered.wait(HOLD_CEILING_SECS)
        assert out is False  # record line: still queued, caller told it did not land
        assert elapsed < CALLER_BOUND_SECS
    finally:
        gate.release.set()


def test_append_line_droppable_open_blocked_returns_to_caller(tmp_path, monkeypatch):
    gate = _Gate()
    monkeypatch.setattr(atomic_append, "IS_WINDOWS", True)
    monkeypatch.setattr(atomic_append, "_open_append_fd_windows", lambda path: gate.hold() or (_ for _ in ()).throw(OSError("released")))
    sink = tmp_path / "sink-droppable.jsonl"
    try:
        _, elapsed = _timed(lambda: atomic_append.append_line(sink, b"x\n", droppable=True))
        assert gate.entered.wait(HOLD_CEILING_SECS)
        assert elapsed < CALLER_BOUND_SECS
    finally:
        gate.release.set()


def test_workflow_run_record_blocked_write_returns_to_caller(tmp_path, monkeypatch):
    gate = _Gate()
    monkeypatch.setattr(pad.json, "dump", lambda record, fh: gate.hold())
    path = tmp_path / "run-record-blocked" / "rec.json"
    path.parent.mkdir()
    try:
        out, elapsed = _timed(lambda: pad._write_workflow_run_record(str(path), {"run_id": "r1"}))
        assert gate.entered.wait(HOLD_CEILING_SECS)
        assert out is None
        assert elapsed < CALLER_BOUND_SECS
    finally:
        gate.release.set()


def test_workflow_run_record_unfrozen_write_still_lands(tmp_path):
    path = tmp_path / "run-record-ok" / "rec.json"
    path.parent.mkdir()
    pad._write_workflow_run_record(str(path), {"run_id": "r2", "args": {"runToken": "t", "k": 1}})
    assert json.loads(path.read_text(encoding="utf-8")) == {"run_id": "r2", "args": {"k": 1}}


def test_chain_arrival_blocked_fs_returns_to_caller(tmp_path, monkeypatch):
    gate = _Gate()
    monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)
    monkeypatch.setattr(cal, "_rotate_if_oversize", lambda path: gate.hold())
    try:
        out, elapsed = _timed(lambda: cal.record_chain_arrival("sess-freeze", True, cwd=str(tmp_path)))
        assert gate.entered.wait(HOLD_CEILING_SECS)
        assert out is None
        assert elapsed < CALLER_BOUND_SECS
    finally:
        gate.release.set()


def test_chain_arrival_unfrozen_write_still_lands(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)
    cal.record_chain_arrival("sess-ok", False, cwd=str(tmp_path))
    ledger = tmp_path / "state" / "chain-arrival-ledger" / "sess-ok" / "chain-arrival-ledger.jsonl"
    assert json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])["session_id"] == "sess-ok"


def test_ask_file_blocked_write_raises_typed_degraded_error(tmp_path, monkeypatch):
    gate = _Gate()
    real_write_text = Path.write_text

    def _write_text(self, *args, **kwargs):
        if self.name == "ask-frozen.md":
            gate.hold()
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _write_text)
    target = tmp_path / "rec" / "ask-frozen.md"
    try:
        t0 = time.monotonic()
        with pytest.raises(TimeoutError, match="degraded"):
            emit_op._write_record_file_bounded(target, "body")
        assert time.monotonic() - t0 < CALLER_BOUND_SECS
        assert gate.entered.wait(HOLD_CEILING_SECS)
    finally:
        gate.release.set()


def test_ask_file_unfrozen_write_lands(tmp_path):
    target = tmp_path / "rec2" / "ask-ok.md"
    emit_op._write_record_file_bounded(target, "body")
    assert target.read_text(encoding="utf-8") == "body"
