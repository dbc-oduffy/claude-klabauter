"""The CLI's --register-pid admit checks and reserves under admission_lock."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("box_memory_snapshot_atomic", BIN / "box-memory-snapshot.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)
host = mod._load("box_memory_host")
mod._load = lambda name: host

GIB = host.GIB


def _run(argv, avail=40):
    return mod.main(
        argv,
        platform="win32",
        status_fn=lambda: host.MemoryStatus(avail * GIB, 64 * GIB, 50 * GIB, 100 * GIB),
        nonpaged_fn=lambda: GIB,
        cache_host_fn=lambda: type("C", (), {"read_memory": lambda self: None})(),
        proc_table_fn=lambda: [],
        cmdline_fn=lambda pid: "",
        rss_fn=lambda p: 0.0,
        alive_fn=lambda p: True,
    )


def test_register_pid_routes_through_admit_and_register(tmp_path, monkeypatch):
    calls = []
    real = host.admit_and_register

    def spy(*a, **k):
        calls.append(a[1:4])
        return real(*a, **k)

    monkeypatch.setattr(host, "admit_and_register", spy)
    path = str(tmp_path / "admission.jsonl")
    assert _run(["--admission-file", path, "--command-key", "k", "--admit-peak", "10", "--register-pid", "5"]) == 0
    assert calls == [("k", 5, 10.0)]


def test_concurrent_cli_admits_cannot_both_pass(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-home"))
    path = str(tmp_path / "admission.jsonl")
    real_reserved = host.reserved_bytes

    # Widen the check-to-write window: without the lock both threads read zero reservations.
    gate = threading.Barrier(2, timeout=1)

    def slow_reserved(*a, **k):
        result = real_reserved(*a, **k)
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        return result

    monkeypatch.setattr(host, "reserved_bytes", slow_reserved)
    codes = []

    def worker(pid):
        argv = ["--admission-file", path, "--command-key", f"k{pid}", "--admit-peak", "20",
                "--register-pid", str(pid)]
        codes.append(_run(argv))

    threads = [threading.Thread(target=worker, args=(p,)) for p in (101, 102)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # peak 20 needs 25 avail; one admit reserves 20 of 40, so the other must HOLD (exit 4).
    assert sorted(codes) == [0, 4]
