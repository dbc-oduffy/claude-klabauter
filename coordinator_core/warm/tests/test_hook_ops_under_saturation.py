"""Hook handler CPU cost with 14 busy I/O threads in the same process.

Measures the calling thread's CPU time (`time.thread_time`), never wall clock: wall clock
measures peer load. TRAP: thread CPU time is blind to scheduler and lock queueing, and the
handlers are called directly, so this pins handler cost only; it does not exercise the guard
pool or its admission gate. Set HOOK_SATURATION_REPORT=1 to print each op's median ms.
"""

from __future__ import annotations

import os
import statistics
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import guard_editor_python_direct, preuse_bash_dispatch, taskstop_launch_recorder

BUSY_WORKERS = 14
SAMPLES = 5
BATCH = 10
BAR_MS = 200.0


def _busy_worker(stop: threading.Event, scratch: Path, idx: int) -> None:
    target = scratch / f"busy-{idx}.bin"
    block = os.urandom(64 * 1024)
    while not stop.is_set():
        with open(target, "wb") as fh:
            fh.write(block)
            fh.flush()
            os.fsync(fh.fileno())
        os.stat(target)


@pytest.fixture
def saturated_pool(tmp_path: Path):
    stop = threading.Event()
    scratch = tmp_path / "busy"
    scratch.mkdir()
    threads = [
        threading.Thread(target=_busy_worker, args=(stop, scratch, i), daemon=True) for i in range(BUSY_WORKERS)
    ]
    for t in threads:
        t.start()
    try:
        yield
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(taskstop_launch_recorder.sys, "platform", "win32")
    return root


def _median_thread_ms(call) -> float:
    """Median per-call CPU ms over SAMPLES batches; a batch spans the thread-clock tick (15.6ms on Windows)."""
    call()
    samples = []
    for _ in range(SAMPLES):
        t0 = time.thread_time()
        for _ in range(BATCH):
            call()
        samples.append((time.thread_time() - t0) * 1000.0 / BATCH)
    return statistics.median(samples)


def _ops(repo: Path) -> dict:
    bash = {"payload": {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(repo), "session_id": "s-sat"}}
    editor = {"payload": {"tool_name": "Read", "tool_input": {}, "cwd": str(repo)}}
    counter = iter(range(10**6))

    def recorder() -> object:
        return taskstop_launch_recorder._handler(
            {
                "payload": {
                    "session_id": "s-sat",
                    "tool_use_id": "tu-sat",
                    "tool_name": "Bash",
                    "tool_input": {"command": "sleep 3001", "run_in_background": True},
                    "tool_response": {"backgroundTaskId": f"bgsat{next(counter):06d}"},
                }
            }
        )

    return {
        "preuse_bash_dispatch": lambda: preuse_bash_dispatch._handler(bash),
        "guard_editor_python_direct": lambda: guard_editor_python_direct._handler(editor),
        "taskstop_launch_recorder": recorder,
    }


def test_hook_ops_stay_under_200ms_process_time_with_14_busy_workers(saturated_pool, repo: Path) -> None:
    results = {name: _median_thread_ms(call) for name, call in _ops(repo).items()}
    if os.environ.get("HOOK_SATURATION_REPORT"):
        for name, ms in results.items():
            print(f"HOOK_SATURATION {name} {ms:.1f}ms")
    over = {name: round(ms, 1) for name, ms in results.items() if ms >= BAR_MS}
    assert not over, f"hook ops over {BAR_MS:.0f}ms process time with {BUSY_WORKERS} busy workers: {over}"


def test_saturation_fixture_keeps_workers_busy_while_measuring(saturated_pool, tmp_path: Path) -> None:
    before = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "busy").iterdir()}
    time.sleep(0.2)
    after = {p.name: p.stat().st_mtime_ns for p in (tmp_path / "busy").iterdir()}
    assert len(after) == BUSY_WORKERS
    assert sum(after[k] != before.get(k) for k in after) >= BUSY_WORKERS // 2
