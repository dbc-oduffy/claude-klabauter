"""A live-handoff head open that blocks never holds compute_gate_report past one wall."""
import builtins
import threading
import time

import pytest

pytestmark = pytest.mark.complete

from coordinator_core import bounded_io
from coordinator_core.roadmap import gate_v2

WALL = 0.3


def _write(root, name, body="body"):
    where = root / "state/handoffs"
    where.mkdir(parents=True, exist_ok=True)
    path = where / f"{name}.md"
    path.write_text(
        f"---\nstub_id: {name}\nbaton_role: work\nstatus: open\ndeployment_state: ready_to_fire\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


class _Block:
    def __init__(self, monkeypatch, target):
        self.target = str(target).replace("\\", "/")
        self.gate = threading.Event()
        self.entered = threading.Event()
        real = builtins.open

        def fake(path, *a, **k):
            if str(path).replace("\\", "/") == self.target:
                self.entered.set()
                self.gate.wait(15)
            return real(path, *a, **k)

        monkeypatch.setattr(gate_v2, "open", fake, raising=False)

    def release(self):
        self.gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with bounded_io._lock:
                if not any(k.startswith("gate-head-read:") for k in bounded_io._stalled) and not any(
                    k.startswith("gate-head-read:") for k in bounded_io._inflight
                ):
                    return
            time.sleep(0.01)


@pytest.fixture(autouse=True)
def _wall(monkeypatch):
    monkeypatch.setattr(gate_v2, "HEAD_READ_DEADLINE_SECS", WALL)
    gate_v2._HEAD_CACHE.clear()
    yield
    gate_v2._HEAD_CACHE.clear()


@pytest.fixture
def root(tmp_path):
    (tmp_path / "state/handoffs").mkdir(parents=True)
    return tmp_path


@pytest.mark.deliberate_wall_clock(reason="bounds the wall a blocked open may hold the gate scan")
def test_blocked_open_without_stale_head_refuses_naming_the_file(root, monkeypatch):
    _write(root, "ok")
    blocked = _write(root, "stuck")
    blk = _Block(monkeypatch, blocked.as_posix())
    try:
        t0 = time.monotonic()
        with pytest.raises(gate_v2.GateReadRefused) as err:
            gate_v2.compute_gate_report(root)
        assert time.monotonic() - t0 <= WALL + 0.5
        assert blk.entered.is_set()
        assert "state/handoffs/stuck.md" in str(err.value)
    finally:
        blk.release()


@pytest.mark.deliberate_wall_clock(reason="bounds the wall a blocked open may hold the gate scan")
def test_blocked_open_with_stale_head_serves_it_and_keeps_the_old_cache_key(root, monkeypatch):
    _write(root, "ok")
    blocked = _write(root, "stuck")
    first = gate_v2.compute_gate_report(root)
    assert {b["id"] for b in first["batons"]} == {"ok", "stuck"}
    full = blocked.as_posix()
    old_key = gate_v2._HEAD_CACHE[full][0]
    _write(root, "stuck", body="a longer body so the stat key moves")
    blk = _Block(monkeypatch, full)
    try:
        t0 = time.monotonic()
        report = gate_v2.compute_gate_report(root)
        assert time.monotonic() - t0 <= WALL + 0.5
        assert blk.entered.is_set()
        assert {b["id"] for b in report["batons"]} == {"ok", "stuck"}
        assert gate_v2._HEAD_CACHE[full][0] == old_key
    finally:
        blk.release()


def test_unblocked_scan_reads_and_caches_every_head(root):
    _write(root, "a")
    _write(root, "b")
    report = gate_v2.compute_gate_report(root)
    assert {b["id"] for b in report["batons"]} == {"a", "b"}
    assert len(gate_v2._HEAD_CACHE) == 2


def test_read_error_inside_the_wall_still_refuses_with_the_unreadable_message(root, monkeypatch):
    _write(root, "a")

    def boom(path, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(gate_v2, "open", boom, raising=False)
    with pytest.raises(gate_v2.GateReadRefused, match="live handoff unreadable: state/handoffs/a.md"):
        gate_v2.compute_gate_report(root)
