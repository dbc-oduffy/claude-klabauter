"""An import- or filesystem-held stall must not restart the engine; a genuine hang still does.

Drives `note_stall`, `maybe_restart` and `_disk_decision` against `tmp_path` runtime dirs with
stall records shaped like the server's own (header lines, then a faulthandler-style thread dump).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core.warm import hang_restart

pytestmark = pytest.mark.complete

T0 = 1_800_000_000.0
_IMPORT_STACK = (
    "Thread 0x0001 (most recent call first):\n"
    '  File "<frozen importlib._bootstrap_external>", line 1593 in _path_stat\n'
    '  File "<frozen importlib._bootstrap_external>", line 1624 in find_spec\n'
    '  File "coordinator_core/hooks/preuse_bash_dispatch.py", line 228 in _run_chain\n'
)
_ZIP_STACK = (
    "Thread 0x0001 (most recent call first):\n"
    '  File "<frozen zipimport>", line 200 in _get_module_code\n'
    '  File "<frozen importlib._bootstrap>", line 1247 in _find_spec\n'
)
_HUNG_STACK = (
    "Thread 0x0001 (most recent call first):\n"
    '  File "threading.py", line 359 in wait\n'
    '  File "coordinator_core/ops/some_op.py", line 10 in run\n'
    "Thread 0x0002 (most recent call first):\n"
    '  File "<frozen importlib._bootstrap>", line 1247 in _find_spec\n'
)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hang_restart, "_stall_marks", {})
    monkeypatch.setattr(hang_restart, "_progress_last", None)
    monkeypatch.setattr(hang_restart, "_progress_excused_since", {})
    monkeypatch.setattr(hang_restart.os, "getpid", lambda: 200)


def _seed(stall_dir: Path, now: float, body: str, *, pid: int = 200, count: int | None = None) -> None:
    stall_dir.mkdir(parents=True, exist_ok=True)
    for i in range(hang_restart.HANG_DUMP_THRESHOLD if count is None else count):
        stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(now - 10 + i))
        (stall_dir / f"{stamp}-{pid}-{body[:6].strip()}-{i}.txt").write_text(
            "engine-stamp: test\nsource-sha: unknown\nopen-path: none\nsinks: []\nleg: \n--- threads ---\n" + body,
            encoding="utf-8",
            newline="\n",
        )


def _seed_restart(svc: Path, pid: int, at: float) -> None:
    stamp = time.strftime(hang_restart._LINE_STAMP_FMT, time.gmtime(at))
    with open(svc / hang_restart.HANG_RESTART_LOG_FILENAME, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{stamp} pid={pid} action=restart count=8 window_s=60 backoff_s=0 dumps=\n")


def _log(svc: Path) -> str:
    log = svc / hang_restart.HANG_RESTART_LOG_FILENAME
    return log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.parametrize("body", [_IMPORT_STACK, _ZIP_STACK])
def test_import_wait_stack_top_classifies_as_io_wait(tmp_path: Path, body: str) -> None:
    _seed(tmp_path, T0, body, count=1)
    (record,) = tmp_path.glob("*.txt")
    assert hang_restart._is_io_wait_record(record)


def test_non_import_stack_top_stays_hung(tmp_path: Path) -> None:
    """An import frame deeper in the stack, or under another thread's non-import top, is not a wait."""
    _seed(tmp_path, T0, _HUNG_STACK, count=1)
    (record,) = tmp_path.glob("*.txt")
    assert not hang_restart._is_io_wait_record(record)


def test_import_wait_window_is_io_stall_not_restart(tmp_path: Path) -> None:
    stall_dir = tmp_path / "stalls"
    _seed(stall_dir, T0, _IMPORT_STACK)
    exits: list = []
    out = hang_restart.maybe_restart(stall_dir, server_pid=200, now=T0 + 5, exit_fn=exits.append)
    assert out == "io-stall" and exits == []
    assert "action=io-stall" in _log(tmp_path) and "action=restart" not in _log(tmp_path)


def test_unfinished_decision_leg_does_not_exit_and_the_same_window_still_restarts_when_hung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stall_dir = tmp_path / "stalls"
    release = threading.Event()
    real = hang_restart._disk_decision
    monkeypatch.setattr(hang_restart, "_disk_decision", lambda *a: release.wait(30))
    monkeypatch.setattr(hang_restart, "_first_suppress", {str(stall_dir): time.monotonic()})
    exits: list = []
    out = None
    try:
        for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
            out = hang_restart.note_stall(stall_dir, server_pid=7, exit_fn=exits.append, deadline=0.05)
    finally:
        release.set()
    assert out == "suppressed" and exits == []

    monkeypatch.setattr(hang_restart, "_disk_decision", real)
    _seed(stall_dir, T0, _HUNG_STACK)
    out = hang_restart.maybe_restart(stall_dir, server_pid=7, now=T0 + 5, exit_fn=exits.append)
    assert out == "restart" and exits == [7]


def test_unfinished_decision_leg_with_no_prior_suppression_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    monkeypatch.setattr(hang_restart, "_disk_decision", lambda *a: release.wait(30))
    monkeypatch.setattr(hang_restart, "_first_suppress", {})
    exits: list = []
    out = None
    try:
        for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
            out = hang_restart.note_stall(tmp_path / "stalls", server_pid=7, exit_fn=exits.append, deadline=0.05)
    finally:
        release.set()
    assert out == "restart" and exits == [7]


def test_finished_leg_that_allows_still_restarts(tmp_path: Path) -> None:
    exits: list = []
    out = None
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
        out = hang_restart.note_stall(tmp_path / "stalls", server_pid=7, exit_fn=exits.append)
    assert out == "restart" and exits == [7]


def test_stalled_successor_exemption_does_not_fire_on_an_import_wait_window(tmp_path: Path) -> None:
    stall_dir = tmp_path / "stalls"
    _seed_restart(tmp_path, 100, T0)
    _seed(stall_dir, T0 + 5, _IMPORT_STACK)
    verdict = hang_restart._disk_decision(stall_dir, 200, T0 + 5, 8, 60.0, hang_restart.RESTART_MIN_INTERVAL_SECS)
    assert verdict == "suppressed"
    assert "reason=backoff" in _log(tmp_path) and "stalled-successor" not in _log(tmp_path)


def test_stalled_successor_exemption_still_fires_on_a_genuine_hang_window(tmp_path: Path) -> None:
    stall_dir = tmp_path / "stalls"
    _seed_restart(tmp_path, 100, T0)
    _seed(stall_dir, T0 + 5, _HUNG_STACK)
    verdict = hang_restart._disk_decision(stall_dir, 200, T0 + 5, 8, 60.0, hang_restart.RESTART_MIN_INTERVAL_SECS)
    assert verdict == "restart"
    assert "pid=200 action=restart reason=stalled-successor" in _log(tmp_path)


def _holder_ctx(tmp_path: Path, leg: dict, monkeypatch: pytest.MonkeyPatch):
    import concurrent.futures
    import json

    from coordinator_core.warm import server

    ctx = server._ServerContext.__new__(server._ServerContext)
    ctx._claim_dir = tmp_path
    fut = concurrent.futures.Future()
    ctx._track_pool_task(fut, "a", "key-a", repo_root="root-c")
    row = ctx._pool_tasks[fut]
    ctx._pool_tasks[fut] = (row[0], time.monotonic() - (server.STALL_DUMP_SECS + 5), row[2], row[3])
    (tmp_path / "key-a.leg").write_text(json.dumps(leg), encoding="utf-8")
    monkeypatch.setattr(server, "_holder_drive_probe_ms", lambda root: server._SLOW_DRIVE_PROBE_MS + 1)
    return ctx


def test_a_compute_leg_holder_on_a_slow_drive_is_hung(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _holder_ctx(tmp_path, {"leg": "coordinator_core/ops/x.py::crunch", "frames": ["x.py::crunch:10"]}, monkeypatch)
    assert ctx._classify_stalled_holders() == (["a"], [])


def test_a_spawn_leg_holder_on_a_slow_drive_is_io_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _holder_ctx(tmp_path, {"leg": "x", "frames": ["subprocess.py::_execute_child:1500"]}, monkeypatch)
    assert ctx._classify_stalled_holders() == ([], ["a"])
