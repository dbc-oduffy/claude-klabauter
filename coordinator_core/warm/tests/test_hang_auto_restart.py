"""N stall dumps (or in-memory stall marks) in the window make the server exit itself once, log one line, and respect holds."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from coordinator_core.warm import breadcrumb, hang_restart, server

NOW = 1_800_000_000.0


def _stamp(epoch: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(epoch))


def _dumps(stall_dir, n, *, end=NOW, pid=111):
    stall_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (stall_dir / f"{_stamp(end - i)}-{pid}-k{i}.txt").write_text("x")


def _run(tmp_path, **kw):
    exits = []
    stall = tmp_path / "stall-dumps"
    out = hang_restart.maybe_restart(stall, server_pid=777, now=NOW, exit_fn=exits.append, **kw)
    return out, exits, tmp_path / hang_restart.HANG_RESTART_LOG_FILENAME


def _lines(log):
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_n_dumps_exit_once_and_log_one_line_with_evidence(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)
    out, exits, log = _run(tmp_path)
    assert out == "restart" and exits == [777]
    (line,) = _lines(log)
    assert "pid=777 action=restart" in line
    assert line.startswith(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW)))
    assert f"count={hang_restart.HANG_DUMP_THRESHOLD}" in line and "window_s=60" in line
    assert "-k0.txt" in line and "+3-more" in line


def test_below_threshold_writes_nothing_and_does_not_exit(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD - 1)
    out, exits, log = _run(tmp_path)
    assert out is None and exits == [] and not log.exists()


def test_dumps_outside_the_window_do_not_count(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD, end=NOW - 3600)
    out, exits, log = _run(tmp_path)
    assert out is None and exits == []


def test_recycle_lines_do_not_count(tmp_path):
    stall = tmp_path / "stall-dumps"
    stall.mkdir()
    for i in range(hang_restart.HANG_DUMP_THRESHOLD):
        (stall / f"{_stamp(NOW - i)}-1-recycle.txt").write_text("x")
    out, exits, _ = _run(tmp_path)
    assert out is None and exits == []


def test_second_trigger_inside_rate_limit_is_suppressed_without_exit(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)
    _run(tmp_path)
    # Fresh dumps after the restart line, still inside the rate-limit interval.
    stall = tmp_path / "stall-dumps"
    later = NOW + hang_restart.RESTART_MIN_INTERVAL_SECS / 2
    _dumps(stall, hang_restart.HANG_DUMP_THRESHOLD, end=later, pid=222)
    exits = []
    out = hang_restart.maybe_restart(stall, server_pid=888, now=later, exit_fn=exits.append)
    assert out == "suppressed" and exits == []
    lines = _lines(tmp_path / hang_restart.HANG_RESTART_LOG_FILENAME)
    assert len(lines) == 2 and "action=suppressed reason=rate-limited" in lines[1]
    # A third trigger the same second adds no duplicate suppressed line.
    hang_restart.maybe_restart(stall, server_pid=888, now=later, exit_fn=exits.append)
    assert len(_lines(tmp_path / hang_restart.HANG_RESTART_LOG_FILENAME)) == 2


def test_a_wedged_replacement_is_restarted_within_a_minute_not_held_for_minutes(tmp_path):
    assert hang_restart.RESTART_MIN_INTERVAL_SECS <= 120.0
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)
    _run(tmp_path)
    later = NOW + hang_restart.RESTART_MIN_INTERVAL_SECS + 5
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD, end=later, pid=44)
    exits = []
    out = hang_restart.maybe_restart(tmp_path / "stall-dumps", server_pid=44, now=later, exit_fn=exits.append)
    assert out == "restart" and exits == [44]


def test_old_dumps_before_the_last_restart_never_retrigger(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)
    _run(tmp_path)
    exits = []
    later = NOW + hang_restart.RESTART_MIN_INTERVAL_SECS + 1
    out = hang_restart.maybe_restart(
        tmp_path / "stall-dumps", server_pid=5, now=later, exit_fn=exits.append, window=10_000
    )
    assert out is None and exits == []


def test_restart_allowed_again_after_the_interval(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)
    _run(tmp_path)
    later = NOW + hang_restart.RESTART_MIN_INTERVAL_SECS + 5
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD, end=later, pid=9)
    exits = []
    out = hang_restart.maybe_restart(tmp_path / "stall-dumps", server_pid=9, now=later, exit_fn=exits.append)
    assert out == "restart" and exits == [9]


def test_publish_marker_suppresses_with_a_publish_hold_line(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)
    (tmp_path / breadcrumb.PUBLISH_MARKER_FILENAME).write_text("not json")
    out, exits, log = _run(tmp_path)
    # The marker's mtime is real time, so judge it at real time.
    exits.clear()
    now = time.time()
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD, end=now, pid=3)
    log.unlink(missing_ok=True)
    out = hang_restart.maybe_restart(tmp_path / "stall-dumps", server_pid=1, now=now, exit_fn=exits.append)
    assert out == "suppressed" and exits == []
    (line,) = _lines(log)
    assert "action=suppressed reason=publish-hold" in line


def test_handover_pending_suppresses_and_expired_does_not(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)
    pending = tmp_path / breadcrumb.HANDOVER_PENDING_FILENAME
    pending.write_text(json.dumps({"expires_at": NOW + 100}))
    out, exits, log = _run(tmp_path)
    assert out == "suppressed" and exits == []
    assert "reason=handover-hold" in _lines(log)[0]

    pending.write_text(json.dumps({"expires_at": NOW - 1}))
    log.unlink()
    out, exits, _ = _run(tmp_path)
    assert out == "restart" and exits == [777]


def test_exit_fn_failure_never_raises(tmp_path):
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)

    def boom(_pid):
        raise OSError("no")

    assert hang_restart.maybe_restart(tmp_path / "stall-dumps", now=NOW, exit_fn=boom) is None


def test_write_stall_record_calls_the_trigger_with_its_own_pid(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda d, **kw: seen.append((d, kw)))
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", None)
    record = server._write_stall_record(tmp_path / "stall-dumps", None, key="k")
    assert record is not None and record.exists()
    assert seen == [(tmp_path / "stall-dumps", {"server_pid": os.getpid()})]


def test_pool_worker_names_its_parent_as_the_server(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(hang_restart, "maybe_restart", lambda d, **kw: seen.append(kw))
    monkeypatch.setattr(server, "_WORKER_STALL_DIR", tmp_path / "stall-dumps")
    server._write_stall_record(tmp_path / "stall-dumps", None, key="k")
    assert seen == [{"server_pid": os.getppid()}]


def test_exit_server_signals_a_foreign_pid_and_exits_own_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("kill", pid)))
    monkeypatch.setattr(os, "_exit", lambda code: calls.append(("exit", code)))
    hang_restart.exit_server(os.getpid() + 1)
    hang_restart.exit_server(os.getpid())
    assert calls == [("kill", os.getpid() + 1), ("exit", 0)]


def _fresh_marks(monkeypatch):
    monkeypatch.setattr(hang_restart, "_stall_marks", {})


def test_note_stall_below_threshold_never_exits_and_touches_no_disk(tmp_path, monkeypatch):
    _fresh_marks(monkeypatch)
    monkeypatch.setattr(hang_restart, "_disk_decision", lambda *a: pytest.fail("disk read below threshold"))
    exits = []
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD - 1):
        assert hang_restart.note_stall(tmp_path / "stall-dumps", server_pid=7, exit_fn=exits.append) is None
    assert exits == []


@pytest.mark.deliberate_wall_clock(reason="hang bound: the restart must not wait on a disk leg that never returns")
def test_note_stall_exits_at_threshold_even_when_the_disk_leg_never_returns(tmp_path, monkeypatch):
    _fresh_marks(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(hang_restart, "_disk_decision", lambda *a: release.wait(30))
    exits = []
    t0 = time.monotonic()
    out = None
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
        out = hang_restart.note_stall(tmp_path / "stall-dumps", server_pid=7, exit_fn=exits.append, deadline=0.2)
    release.set()
    assert out == "restart" and exits == [7]
    assert time.monotonic() - t0 < 5


def test_note_stall_honours_a_publish_hold_and_logs_it(tmp_path, monkeypatch):
    _fresh_marks(monkeypatch)
    (tmp_path / breadcrumb.PUBLISH_MARKER_FILENAME).write_text("not json")
    exits = []
    out = None
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
        out = hang_restart.note_stall(tmp_path / "stall-dumps", server_pid=7, exit_fn=exits.append)
    assert out == "suppressed" and exits == []
    assert "action=suppressed reason=publish-hold" in _lines(tmp_path / hang_restart.HANG_RESTART_LOG_FILENAME)[0]


def test_note_stall_logs_a_restart_line_when_the_disk_answers(tmp_path, monkeypatch):
    _fresh_marks(monkeypatch)
    exits = []
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
        hang_restart.note_stall(tmp_path / "stall-dumps", server_pid=7, exit_fn=exits.append)
    assert exits == [7]
    assert "pid=7 action=restart" in _lines(tmp_path / hang_restart.HANG_RESTART_LOG_FILENAME)[0]


def test_accept_stall_watcher_counts_before_it_touches_the_filesystem(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(hang_restart, "note_stall", lambda d, **kw: seen.append(d))
    blocked = threading.Event()

    class _Claim:
        def exists(self):
            assert seen, "the stall was not counted before the claim stat"
            blocked.set()
            return True

    disarm = server._arm_accept_stall(tmp_path / "stall-dumps", 0.05, _Claim(), "k", ["gate"])
    assert blocked.wait(5)
    disarm()
    assert seen == [tmp_path / "stall-dumps"]


def test_a_replacement_survives_its_own_cold_start_import_stalls(tmp_path):
    """The 2026-11-11 loop: a fresh server's workers stall importing under the backlog, 8 stalls
    land within ~15s, and a 20s rate limit let it exit before it warmed."""
    assert hang_restart.RESTART_MIN_INTERVAL_SECS >= 60.0
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD)
    _run(tmp_path)
    later = NOW + 20
    _dumps(tmp_path / "stall-dumps", hang_restart.HANG_DUMP_THRESHOLD, end=later, pid=55)
    exits = []
    out = hang_restart.maybe_restart(tmp_path / "stall-dumps", server_pid=55, now=later, exit_fn=exits.append)
    assert out == "suppressed" and exits == []


def test_stall_watchers_hold_no_lazy_hang_restart_import():
    """A lazy import in a stall watcher blocks on the importlib module lock when the server is
    imported-from-cold; the watchers must reach hang_restart through a module-level binding."""
    import inspect

    for fn in (server._write_stall_record, server._note_stall_in_memory):
        assert "import hang_restart" not in inspect.getsource(fn)
    assert server.hang_restart is hang_restart
