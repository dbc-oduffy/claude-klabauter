"""
coordinator_core.ops.ceremony.tests.test_detached_spawn

Tests for detached_spawn.py -- the shared detached-CLI-spawn helper factored out of
`auto_push.spawn_detached_push` for C17's other housekeeping-spawn chunks to reuse.

Coverage:
  (a) posix_uses_start_new_session   -- on a POSIX-shaped platform (`os.fork` present),
                                         Popen is called with start_new_session=True and
                                         no `creationflags` kwarg.
  (b) windows_uses_detached_flags     -- with `os.fork` monkeypatched away (simulating
                                         Windows), Popen is called with `creationflags`
                                         carrying DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                                         | CREATE_NO_WINDOW, and no `start_new_session` kwarg.
                                         Runs unconditionally on darwin too -- the whole
                                         point is not skipping the Windows leg just because
                                         the test host isn't Windows.
  (c) both_platforms_disown_stdio     -- stdin/stdout/stderr are all DEVNULL and
                                         close_fds=True on both legs.
  (d) popen_exception_is_swallowed    -- Popen raising is caught, NOT propagated, and
                                         `spawn_detached` returns False.
  (e) failure_is_logged               -- a swallowed failure appends one record to
                                         state/housekeeping-failures.log under the given
                                         repo_root, in the documented format.
  (f) no_interpreter_resolvable       -- sys.executable/shutil.which all falsy is handled
                                         the same way: logged, returns False, no raise.

C17b additions (child-side record + read/clear halves of the surfacing mechanism):
  (g) record_child_failure_*          -- exit_code / exception both produce a "CHILD FAILED"
                                         record (distinct verb from "SPAWN FAILED"); a no-op
                                         call (no exit_code, no exc) writes nothing.
  (h) read_and_clear_failures_log     -- read_failures_log is non-destructive (log survives
                                         a peek); clear_failures_log truncates it to empty;
                                         both are never-raise on a missing log file.

Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C17a/C17b.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops.ceremony import detached_spawn


@pytest.fixture
def repo_root(tmp_path: Path) -> str:
    return str(tmp_path)


def test_posix_uses_start_new_session(repo_root: str) -> None:
    with mock.patch.object(detached_spawn.os, "fork", create=True, new=lambda: 0), \
         mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        ok = detached_spawn.spawn_detached(repo_root, "/some/script.py", ["--x", "1"])

    assert ok is True
    _, kwargs = mock_popen.call_args
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


def test_windows_uses_detached_flags(repo_root: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(detached_spawn.os, "fork", raising=False)
    with mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        ok = detached_spawn.spawn_detached(repo_root, "/some/script.py", ["--x", "1"])

    assert ok is True
    _, kwargs = mock_popen.call_args
    assert "start_new_session" not in kwargs
    expected = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    assert kwargs["creationflags"] == expected


@pytest.mark.parametrize("has_fork", [True, False])
def test_both_platforms_disown_stdio(repo_root: str, monkeypatch: pytest.MonkeyPatch, has_fork: bool) -> None:
    if not has_fork:
        monkeypatch.delattr(detached_spawn.os, "fork", raising=False)
    else:
        monkeypatch.setattr(detached_spawn.os, "fork", lambda: 0, raising=False)

    with mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        detached_spawn.spawn_detached(repo_root, "/some/script.py", [])

    _, kwargs = mock_popen.call_args
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["close_fds"] is True


def test_popen_exception_is_swallowed(repo_root: str) -> None:
    with mock.patch.object(detached_spawn.subprocess, "Popen", side_effect=OSError("boom")):
        ok = detached_spawn.spawn_detached(repo_root, "/some/script.py", ["--y"])

    assert ok is False


def test_failure_is_logged(repo_root: str) -> None:
    with mock.patch.object(detached_spawn.subprocess, "Popen", side_effect=OSError("boom")):
        detached_spawn.spawn_detached(repo_root, "/some/script.py", ["--y"])

    log_path = detached_spawn.housekeeping_failures_log_path(repo_root)
    assert log_path == Path(repo_root, "state", "housekeeping-failures.log")
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("[")
    assert "SPAWN FAILED" in line
    # Both loggers record `os.path.abspath(script_path)`, so the expected
    # spelling is platform-dependent (Windows anchors a rooted path to the
    # cwd's drive and uses backslashes) -- the literal POSIX form is only
    # correct on POSIX.
    assert "script=%s" % (os.path.abspath("/some/script.py"),) in line
    assert "args=['--y']" in line
    assert "OSError: boom" in line


def test_no_interpreter_resolvable(repo_root: str) -> None:
    with mock.patch.object(detached_spawn.sys, "executable", ""), \
         mock.patch.object(detached_spawn.shutil, "which", return_value=None), \
         mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        ok = detached_spawn.spawn_detached(repo_root, "/some/script.py", [])

    assert ok is False
    mock_popen.assert_not_called()
    log_path = detached_spawn.housekeeping_failures_log_path(repo_root)
    assert log_path.exists()


# ---------------------------------------------------------------------------
# C17b: record_child_failure
# ---------------------------------------------------------------------------


def test_record_child_failure_exit_code(repo_root: str) -> None:
    detached_spawn.record_child_failure(repo_root, "/some/child.py", exit_code=2)

    log_path = detached_spawn.housekeeping_failures_log_path(repo_root)
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "CHILD FAILED" in lines[0]
    assert "SPAWN FAILED" not in lines[0]
    assert "script=%s" % (os.path.abspath("/some/child.py"),) in lines[0]
    assert "non-zero exit 2" in lines[0]


def test_record_child_failure_exception(repo_root: str) -> None:
    detached_spawn.record_child_failure(
        repo_root, "/some/child.py", exc=RuntimeError("kaboom")
    )

    log_path = detached_spawn.housekeeping_failures_log_path(repo_root)
    line = log_path.read_text(encoding="utf-8").splitlines()[0]
    assert "CHILD FAILED" in line
    assert "RuntimeError: kaboom" in line


def test_record_child_failure_noop_when_clean(repo_root: str) -> None:
    detached_spawn.record_child_failure(repo_root, "/some/child.py")
    detached_spawn.record_child_failure(repo_root, "/some/child.py", exit_code=0)

    log_path = detached_spawn.housekeeping_failures_log_path(repo_root)
    assert not log_path.exists()


def test_record_child_failure_never_raises_on_unwritable_log(
    repo_root: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom_mkdir(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr(detached_spawn.Path, "mkdir", _boom_mkdir)
    detached_spawn.record_child_failure(repo_root, "/some/child.py", exit_code=1)  # must not raise


# ---------------------------------------------------------------------------
# C17b: read_failures_log / clear_failures_log
# ---------------------------------------------------------------------------


def test_read_failures_log_is_non_destructive(repo_root: str) -> None:
    detached_spawn.record_child_failure(repo_root, "/x.py", exit_code=1)

    content = detached_spawn.read_failures_log(repo_root)
    assert "CHILD FAILED" in content

    # A second peek sees the identical content -- the log was not consumed.
    assert detached_spawn.read_failures_log(repo_root) == content


def test_read_failures_log_missing_file_returns_empty(repo_root: str) -> None:
    assert detached_spawn.read_failures_log(repo_root) == ""


def test_clear_failures_log_truncates(repo_root: str) -> None:
    detached_spawn.record_child_failure(repo_root, "/x.py", exit_code=1)
    assert detached_spawn.read_failures_log(repo_root) != ""

    detached_spawn.clear_failures_log(repo_root)

    assert detached_spawn.read_failures_log(repo_root) == ""


def test_clear_failures_log_missing_file_never_raises(repo_root: str) -> None:
    detached_spawn.clear_failures_log(repo_root)  # must not raise on an absent log
