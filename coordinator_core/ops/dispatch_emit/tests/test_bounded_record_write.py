import threading
import time

import pytest

from coordinator_core import _settings_home
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import live_run_index as idx


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml-home")


def _blocker(monkeypatch, real=None):
    gate = threading.Event()
    calls = []

    def fake(path, data, **kw):
        calls.append(path)
        gate.wait(10)
        if real is not None:
            real(path, data)

    monkeypatch.setattr(hrr, "atomic_write_bytes", fake)
    return gate, calls


def test_success_returns_none_and_writes(tmp_path):
    p = tmp_path / "a.bin"
    assert hrr.bounded_write(p, b"x", 1000) is None
    assert p.read_bytes() == b"x"
    assert not hrr.write_outstanding(p)


def test_blocked_write_returns_stalled_within_deadline(tmp_path, monkeypatch):
    gate, _ = _blocker(monkeypatch)
    p = tmp_path / "a.bin"
    t0 = time.monotonic()
    try:
        assert hrr.bounded_write(p, b"x", 100) is hrr.STALLED
        assert time.monotonic() - t0 < 0.2
        assert hrr.write_outstanding(p)
    finally:
        gate.set()


def test_second_write_to_outstanding_path_stalls_at_once(tmp_path, monkeypatch):
    gate, calls = _blocker(monkeypatch)
    p = tmp_path / "a.bin"
    try:
        assert hrr.bounded_write(p, b"1", 50) is hrr.STALLED
        t0 = time.monotonic()
        assert hrr.bounded_write(p, b"2", 5000) is hrr.STALLED
        assert time.monotonic() - t0 < 0.1
        assert len(calls) == 1
    finally:
        gate.set()


def test_late_write_lands_and_path_frees(tmp_path, monkeypatch):
    from coordinator_core.atomic_replace import atomic_write_bytes

    gate, _ = _blocker(monkeypatch, real=atomic_write_bytes)
    p = tmp_path / "a.bin"
    assert hrr.bounded_write(p, b"late", 50) is hrr.STALLED
    gate.set()
    deadline = time.monotonic() + 3
    while hrr.write_outstanding(p) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert p.read_bytes() == b"late"
    monkeypatch.undo()
    assert hrr.bounded_write(p, b"next", 1000) is None


def test_oserror_on_worker_is_reraised_not_stalled(tmp_path, monkeypatch):
    def boom(path, data, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(hrr, "atomic_write_bytes", boom)
    p = tmp_path / "a.bin"
    with pytest.raises(OSError):
        hrr.bounded_write(p, b"x", 1000)
    assert not hrr.write_outstanding(p)


def test_live_run_index_add_returns_false_when_write_blocks(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    gate, _ = _blocker(monkeypatch)
    monkeypatch.setattr(idx, "WRITE_DEADLINE_MS", 100.0)
    t0 = time.monotonic()
    try:
        assert idx.add(tmp_path, "r1", 1.0) is False
        assert time.monotonic() - t0 < 0.3
    finally:
        gate.set()


def test_mutate_skips_load_while_write_outstanding(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    gate, _ = _blocker(monkeypatch)
    monkeypatch.setattr(idx, "WRITE_DEADLINE_MS", 50.0)
    loads = []
    monkeypatch.setattr(idx, "_load", lambda *a: loads.append(1) or {})
    try:
        assert idx.add(tmp_path, "r1", 1.0) is False
        assert len(loads) == 1
        assert idx.add(tmp_path, "r2", 1.0) is False
        assert len(loads) == 1
    finally:
        gate.set()
