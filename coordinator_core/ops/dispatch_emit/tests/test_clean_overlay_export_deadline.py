"""`export_step_timeout_s` and `collect_startup_s`: named env overrides, a floor, and a default that scales on Windows."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import time

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov


def _no_getloadavg(monkeypatch) -> None:
    def boom():
        raise AttributeError("getloadavg")

    monkeypatch.setattr(os, "getloadavg", boom, raising=False)


@pytest.mark.parametrize("raw", ["900", " 900 ", "900.0"])
def test_a_positive_export_override_wins(monkeypatch, raw) -> None:
    monkeypatch.setenv(cov.EXPORT_TIMEOUT_ENV, raw)
    assert cov.export_step_timeout_s() == 900
    assert cov.export_step_timeout_s(5000) == 900


@pytest.mark.parametrize("raw", ["", "0", "-5", "abc", "nan", "inf"])
def test_a_bad_export_override_falls_back_to_the_default(monkeypatch, raw) -> None:
    _no_getloadavg(monkeypatch)
    monkeypatch.setenv(cov.EXPORT_TIMEOUT_ENV, raw)
    assert cov.export_step_timeout_s(100) >= cov.TEST_TIMEOUT_S
    assert cov.export_step_timeout_s(100) != 0


def test_the_default_is_never_below_the_floor(monkeypatch) -> None:
    monkeypatch.delenv(cov.EXPORT_TIMEOUT_ENV, raising=False)
    _no_getloadavg(monkeypatch)
    monkeypatch.setattr(cov, "STALL_RETRY_SCALE_MIN", 0.1)
    monkeypatch.setattr(cov, "STALL_RETRY_SCALE_MAX", 0.1)
    assert cov.export_step_timeout_s(0) == cov.TEST_TIMEOUT_S


def test_the_windows_default_scales_with_paths(monkeypatch) -> None:
    monkeypatch.delenv(cov.EXPORT_TIMEOUT_ENV, raising=False)
    _no_getloadavg(monkeypatch)
    sizes = [0, 10, 200, 1416, 5000]
    values = [cov.export_step_timeout_s(n) for n in sizes]
    assert values == sorted(values)
    assert cov.export_step_timeout_s(1416) > cov.TEST_TIMEOUT_S
    assert cov.export_step_timeout_s(5000) > cov.export_step_timeout_s(1416)


def test_load_scales_the_default_where_getloadavg_exists(monkeypatch) -> None:
    monkeypatch.delenv(cov.EXPORT_TIMEOUT_ENV, raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    monkeypatch.setattr(os, "getloadavg", lambda: (32.0, 0.0, 0.0), raising=False)
    loaded = cov.export_step_timeout_s(0)
    monkeypatch.setattr(os, "getloadavg", lambda: (0.0, 0.0, 0.0), raising=False)
    assert loaded > cov.export_step_timeout_s(0) >= cov.TEST_TIMEOUT_S


def test_stall_retry_window_is_unchanged(monkeypatch) -> None:
    _no_getloadavg(monkeypatch)
    assert cov.stall_retry_window() == cov.PYTEST_STALL_S * cov.STALL_RETRY_SCALE_MIN


@pytest.mark.parametrize("raw", ["", "0", "-1", "abc"])
def test_collect_startup_defaults_on_bad_env(monkeypatch, raw) -> None:
    monkeypatch.setenv(cov.COLLECT_STARTUP_ENV, raw)
    assert cov.collect_startup_s() == cov.PYTEST_STARTUP_S


def test_collect_startup_honours_a_positive_env(monkeypatch) -> None:
    monkeypatch.setenv(cov.COLLECT_STARTUP_ENV, "600")
    assert cov.collect_startup_s() == 600


class _Pipe:
    """A stdout/stderr stand-in whose `read1` blocks until a chunk is fed or the pipe is closed."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()

    def feed(self, chunk: bytes) -> None:
        self._q.put(chunk)

    def read1(self, _n: int) -> bytes:
        return self._q.get()


class _FakeProc:
    """A silent process on a fake clock: each `wait(timeout)` advances the clock by `timeout`, runs `on_tick`
    (output or CPU growth), and exits once the clock reaches `exit_at`."""

    returncode = 0

    def __init__(self, clock: list, exit_at: float, on_tick=None) -> None:
        self.stdout, self.stderr = _Pipe(), _Pipe()
        self._clock, self._exit_at, self._on_tick = clock, exit_at, on_tick
        self.closed = False

    def wait(self, timeout=None):
        if self.closed or self._clock[0] >= self._exit_at:
            return self.returncode
        self._clock[0] += timeout or 0.0
        if self._on_tick is not None:
            self._on_tick(self)
        if self._clock[0] >= self._exit_at:
            return self.returncode
        raise subprocess.TimeoutExpired("fake", timeout)

    def close(self) -> None:
        self.closed = True
        self.stdout.feed(b"")
        self.stderr.feed(b"")


def _watch(monkeypatch, proc, clock, stall_s, cpu=None):
    monkeypatch.setattr(cov, "_now", lambda: clock[0])
    if cpu is not None:
        proc.close.tree_cpu_s = cpu
    return cov._watched(proc, proc.close, ["pytest"], stall_s)


def _bind_close(proc: _FakeProc) -> _FakeProc:
    bound = proc.close
    proc.close = lambda: bound()
    return proc


def test_collect_startup_window_comes_from_the_env_and_expires_on_a_silent_idle_tree(monkeypatch) -> None:
    monkeypatch.setenv(cov.COLLECT_STARTUP_ENV, "40")
    clock = [0.0]
    proc = _bind_close(_FakeProc(clock, exit_at=1e9))
    with pytest.raises(cov.PytestStalled) as stalled:
        _watch(monkeypatch, proc, clock, stall_s=10, cpu=lambda: 1.0)
    assert 40 <= clock[0] < 41
    assert stalled.value.timeout == 40


def test_a_silent_tree_still_burning_cpu_is_not_stalled_past_the_startup_window(monkeypatch) -> None:
    monkeypatch.setenv(cov.COLLECT_STARTUP_ENV, "40")
    clock = [0.0]
    proc = _bind_close(_FakeProc(clock, exit_at=200.0))
    done = _watch(monkeypatch, proc, clock, stall_s=10, cpu=lambda: clock[0] * 0.9)
    assert done.returncode == 0
    assert clock[0] >= 200.0


def test_a_tree_whose_cpu_is_unreadable_falls_back_to_the_wall_clock_window(monkeypatch) -> None:
    monkeypatch.setenv(cov.COLLECT_STARTUP_ENV, "40")
    clock = [0.0]
    proc = _bind_close(_FakeProc(clock, exit_at=1e9))
    with pytest.raises(cov.PytestStalled):
        _watch(monkeypatch, proc, clock, stall_s=10, cpu=lambda: None)
    assert 40 <= clock[0] < 41


def test_growing_collect_output_extends_the_window_and_a_stall_after_it_uses_the_stall_window(monkeypatch) -> None:
    monkeypatch.setenv(cov.COLLECT_STARTUP_ENV, "40")
    clock = [0.0]

    def collecting(p: _FakeProc) -> None:
        if clock[0] < 100.0 and int(clock[0] * 2) % 20 == 0:
            p.stdout.feed(b"collected %d items\n" % clock[0])
            time.sleep(0.02)

    proc = _bind_close(_FakeProc(clock, exit_at=1e9, on_tick=collecting))
    with pytest.raises(cov.PytestStalled) as stalled:
        _watch(monkeypatch, proc, clock, stall_s=10, cpu=lambda: 1.0)
    assert clock[0] >= 100.0
    assert stalled.value.timeout == 10


@pytest.mark.skipif(not (sys.platform == "win32" or sys.platform.startswith("linux")), reason="tree CPU readable on Windows and Linux")
def test_the_tree_cpu_progress_signal_is_readable_on_a_live_spawned_tree() -> None:
    from coordinator_core.diagnostics.contained_run import spawn_in_capped_job

    proc, close = spawn_in_capped_job(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        popen_kwargs=dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL),
        background=True,
    )
    try:
        assert isinstance(close.tree_cpu_s(), float)
    finally:
        close()
    if sys.platform == "win32":
        assert close.tree_cpu_s() is None
