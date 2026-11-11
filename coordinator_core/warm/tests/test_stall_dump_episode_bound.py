"""One full stall dump per episode; later stalls leave a stub that still counts toward restart."""

from __future__ import annotations

import os
import threading
import time

from coordinator_core.warm import hang_restart, server


def _records(stall_dir):
    return sorted(stall_dir.glob("*.txt"))


def _full(stall_dir):
    return [p for p in _records(stall_dir) if "--- threads ---" in p.read_text(encoding="utf-8")]


def test_concurrent_burst_writes_one_full_dump_and_n_minus_one_stubs(tmp_path, monkeypatch):
    stall_dir = tmp_path / "svc" / "stall-dumps"
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda *a, **k: None)
    n = 8
    barrier = threading.Barrier(n)

    def _stall(i):
        barrier.wait()
        server._write_stall_record(stall_dir, None, key=f"k{i}", leg="{}")

    threads = [threading.Thread(target=_stall, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(_records(stall_dir)) == n
    (full,) = _full(stall_dir)
    assert "Thread 0x" in full.read_text(encoding="utf-8") or "Current thread" in full.read_text(encoding="utf-8")
    stubs = [p for p in _records(stall_dir) if p != full]
    assert len(stubs) == n - 1
    assert all(p.read_text(encoding="utf-8").startswith("stub:") for p in stubs)


def test_stall_after_window_writes_a_new_full_dump(tmp_path, monkeypatch):
    stall_dir = tmp_path / "svc" / "stall-dumps"
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda *a, **k: None)
    server._write_stall_record(stall_dir, None, key="a")
    marker = stall_dir / server._STALL_EPISODE_MARKER
    old = time.time() - 60
    os.utime(marker, (old, old))
    assert server._claim_stall_episode(stall_dir, 5.0) is True
    assert server._claim_stall_episode(stall_dir, 5.0) is False


def test_stub_records_count_toward_the_restart_threshold(tmp_path, monkeypatch):
    stall_dir = tmp_path / "svc" / "stall-dumps"
    exits = []
    monkeypatch.setattr(hang_restart, "exit_server", exits.append)
    seen = []
    for i in range(hang_restart.HANG_DUMP_THRESHOLD):
        server._write_stall_record(stall_dir, None, key=f"k{i}", leg="{}")
        seen.append(len(exits))
    assert len(_full(stall_dir)) == 1
    assert seen == [0] * (hang_restart.HANG_DUMP_THRESHOLD - 1) + [1]


def test_operator_dump_request_bypasses_the_episode_gate(tmp_path, monkeypatch):
    stall_dir = tmp_path / "svc" / "stall-dumps"
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda *a, **k: None)
    server._write_stall_record(stall_dir, None, key="a")
    server._write_stall_record(stall_dir, None, key="operator-stop", full=True)
    assert len(_full(stall_dir)) == 2
