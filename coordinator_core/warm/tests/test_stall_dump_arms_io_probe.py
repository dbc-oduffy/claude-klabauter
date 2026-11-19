"""A stall dump arms one in-thread I/O probe per quiet window; a dump inside the window arms none,
and the probe log line format is fixed."""

from __future__ import annotations

import threading
import time

import pytest

from coordinator_core.warm import hang_restart, io_probe, server

pytestmark = pytest.mark.complete


@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    """Server-side dump context with the probe loop replaced by a thread that parks until released."""
    release = threading.Event()
    started = []

    def parked(targets, log_path, deadline, **_kw):
        started.append((list(targets), log_path, deadline))
        release.wait(30)

    monkeypatch.setattr(io_probe, "run_loop", parked)
    monkeypatch.setattr(io_probe, "_thread", None)
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda *a, **k: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    svc = tmp_path / "svc"
    yield svc, repo, started, release
    release.set()
    t = io_probe._thread
    if t is not None:
        t.join(5)


def _dump(svc, repo, key):
    return server._write_stall_record(svc / "stall-dumps", None, key=key, leg="x", repo_root=str(repo), full=True, sample_box=False)


def _wait_started(started, n):
    deadline = time.monotonic() + 5
    while len(started) < n and time.monotonic() < deadline:
        time.sleep(0.01)


def test_a_dump_arms_one_probe_beside_the_stall_dumps_dir(probe_env):
    svc, repo, started, release = probe_env
    assert _dump(svc, repo, "k1") is not None
    _wait_started(started, 1)
    assert len(started) == 1
    targets, log_path, deadline = started[0]
    assert log_path == svc / io_probe.LOG_NAME
    assert targets[0] == ("repo", repo / "scratch")
    assert 0 < deadline - time.monotonic() <= io_probe.WINDOW_S


def test_a_second_dump_inside_the_window_arms_none(probe_env):
    svc, repo, started, release = probe_env
    _dump(svc, repo, "k1")
    _wait_started(started, 1)
    _dump(svc, repo, "k2")
    time.sleep(0.1)
    assert len(started) == 1
    assert io_probe.arm(svc, repo) is False


def test_a_dump_after_the_probe_ended_arms_a_new_one(probe_env):
    svc, repo, started, release = probe_env
    assert io_probe.arm(svc, repo) is True
    release.set()
    io_probe._thread.join(5)
    release.clear()
    assert io_probe.arm(svc, repo) is True


def test_a_pool_worker_dump_arms_nothing(probe_env, monkeypatch):
    svc, repo, started, release = probe_env
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", svc / "stall-dumps")
    _dump(svc, repo, "k1")
    time.sleep(0.1)
    assert started == []


def test_the_log_line_format_is_fixed():
    # 2026-11-18T10:21:13.250Z
    wall = 1794997273.25
    assert io_probe.format_line(wall, "repo", 123.456, "E:/r/scratch") == "2026-11-18T10:21:13.250Z io-probe repo 123.5ms E:/r/scratch\n"


def test_only_ops_over_the_threshold_are_logged(tmp_path):
    log = tmp_path / "io-probe.log"
    clock = iter([0.0, 0.5, 1.0])
    ms = {"fast": io_probe.SLOW_MS, "slow": io_probe.SLOW_MS + 0.1}
    io_probe.run_loop(
        [("fast", tmp_path / "a"), ("slow", tmp_path / "b")],
        log,
        deadline=1.0,
        monotonic=lambda: next(clock),
        wall=lambda: 0.0,
        wait=lambda _s: None,
        probe=lambda d: ms["fast"] if d.name == "a" else ms["slow"],
    )
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(" io-probe slow 50.1ms " in line for line in lines)


def test_a_log_at_its_size_cap_takes_no_more_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(io_probe, "LOG_MAX_BYTES", 10)
    log = tmp_path / "io-probe.log"
    log.write_text("x" * 10, encoding="utf-8")
    clock = iter([0.0, 0.5, 1.0])
    io_probe.run_loop(
        [("slow", tmp_path / "b")],
        log,
        deadline=1.0,
        monotonic=lambda: next(clock),
        wall=lambda: 0.0,
        wait=lambda _s: None,
        probe=lambda d: io_probe.SLOW_MS + 100.0,
    )
    assert log.read_text(encoding="utf-8") == "x" * 10


def test_probe_work_stays_cheap(tmp_path):
    # The Windows CPU clock ticks at ~15.6 ms: the loop must span >= 100 ticks (~1.6 s of process
    # time) so the 15 ms/min bar (both targets combined, one tick per TICK_S) is measured, not quantized.
    # Targets follow production: a second target exists only on a different volume, so tmp_path
    # (one volume) yields exactly the targets the engine would probe here.
    targets = [d for _label, d in io_probe.targets_for(tmp_path / "repo")]
    span = 100 * 0.015625

    def ms_per_minute() -> float:
        ticks = 0
        t0 = time.process_time()
        while time.process_time() - t0 < span:
            for d in targets:
                io_probe.probe_once(d)
            ticks += 1
        probe_cpu = time.process_time() - t0
        t0 = time.process_time()
        for _ in range(ticks):
            for d in targets:
                pass
        baseline_cpu = time.process_time() - t0
        return (probe_cpu - baseline_cpu) * 1000 / (ticks * io_probe.TICK_S / 60.0)

    # Best of three: peer load inflates CPU time; the floor is the probe's own cost.
    assert min(ms_per_minute() for _ in range(3)) <= 15.0
    assert all(list(d.iterdir()) == [] for d in targets if d.exists())
