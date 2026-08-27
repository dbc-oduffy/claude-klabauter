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
                                         carrying CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
                                         and NOT DETACHED_PROCESS, and no `start_new_session`
                                         kwarg.
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

2026-08-21 additions (state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md) --
  `script_path` resolved against the CALLER'S cwd rather than `repo_root`, and no
  `PYTHONPATH` carrying `repo_root`, meant a detached child could silently fail to find
  its own script (real fleet condition: caller cwd is whatever repo the session is in)
  or, having found it, still import the wrong `coordinator_core` (this box's ambient
  editable-install pin):
  (i)   resolves_script_against_repo_root_not_caller_cwd -- the path-anchor fix.
  (j)   child_env_carries_repo_root_on_pythonpath        -- the PYTHONPATH-injection fix.
  (k)   missing_script_is_logged_and_never_spawned       -- a script that still resolves
                                                             to nothing is now refused
                                                             BEFORE Popen, not spawned blind.

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


@pytest.fixture
def existing_script(tmp_path: Path) -> str:
    """A REAL file under `repo_root`, relative-path form -- `spawn_detached`
    now stats the resolved path before ever calling `Popen` (this row's own
    fix; see `test_missing_script_is_logged_and_never_spawned` below), so a
    fabricated, non-existent path like the old suite's `"/some/script.py"`
    no longer reaches any of the Popen-shape assertions these tests exist
    to make -- it is refused one line earlier instead."""
    (tmp_path / "script.py").write_text("# placeholder\n", encoding="utf-8")
    return "script.py"


def test_posix_uses_start_new_session(repo_root: str, existing_script: str) -> None:
    with mock.patch.object(detached_spawn.os, "fork", create=True, new=lambda: 0), \
         mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        ok = detached_spawn.spawn_detached(repo_root, existing_script, ["--x", "1"])

    assert ok is True
    _, kwargs = mock_popen.call_args
    assert kwargs["start_new_session"] is True
    assert "creationflags" not in kwargs


def test_windows_uses_detached_flags(
    repo_root: str, existing_script: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(detached_spawn.os, "fork", raising=False)
    with mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        ok = detached_spawn.spawn_detached(repo_root, existing_script, ["--x", "1"])

    assert ok is True
    _, kwargs = mock_popen.call_args
    assert "start_new_session" not in kwargs
    expected = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    assert kwargs["creationflags"] == expected
    # Load-bearing: ORing DETACHED_PROCESS back in is a no-op suppression, not a
    # stronger one -- Win32 ignores CREATE_NO_WINDOW when DETACHED_PROCESS is set,
    # and the child's descendants then each allocate a WINDOWED console.
    assert not kwargs["creationflags"] & getattr(subprocess, "DETACHED_PROCESS", 0)


@pytest.mark.parametrize("has_fork", [True, False])
def test_both_platforms_disown_stdio(
    repo_root: str, existing_script: str, monkeypatch: pytest.MonkeyPatch, has_fork: bool
) -> None:
    if not has_fork:
        monkeypatch.delattr(detached_spawn.os, "fork", raising=False)
    else:
        monkeypatch.setattr(detached_spawn.os, "fork", lambda: 0, raising=False)

    with mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        detached_spawn.spawn_detached(repo_root, existing_script, [])

    _, kwargs = mock_popen.call_args
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["close_fds"] is True


def test_resolves_script_against_repo_root_not_caller_cwd(
    repo_root: str, existing_script: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE FIX (state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md):
    `script_path` must resolve against `repo_root`, never against
    `os.getcwd()`. Proven by pointing the process's OWN cwd at an unrelated
    directory that does NOT contain `existing_script` -- the prior
    `os.path.abspath(script_path)` form would have resolved to a path under
    THAT directory and (in production, with Popen unmocked) spawned a doomed
    interpreter that could never find its script."""
    unrelated = str(Path(repo_root).parent)  # a real dir, no `script.py` in it
    monkeypatch.chdir(unrelated)
    with mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        ok = detached_spawn.spawn_detached(repo_root, existing_script)

    assert ok is True
    args, kwargs = mock_popen.call_args
    argv = args[0]
    assert argv[1] == str(Path(repo_root) / existing_script)
    assert kwargs["cwd"] == repo_root


def test_child_env_carries_repo_root_on_pythonpath(repo_root: str, existing_script: str) -> None:
    """THE OTHER HALF of the fix -- `_child_env`'s own docstring: resolving
    the script path alone still imports the WRONG `coordinator_core` inside
    the child (confirmed live against this box's ambient editable-install
    pin). `PYTHONPATH` must carry `repo_root` PREPENDED, not merely present,
    ahead of anything the parent process already had set."""
    with mock.patch.dict(detached_spawn.os.environ, {"PYTHONPATH": "/pre-existing"}, clear=False), \
         mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        detached_spawn.spawn_detached(repo_root, existing_script)

    _, kwargs = mock_popen.call_args
    pythonpath = kwargs["env"]["PYTHONPATH"]
    assert pythonpath.startswith(repo_root)
    assert "/pre-existing" in pythonpath


def test_missing_script_is_logged_and_never_spawned(repo_root: str) -> None:
    """A `script_path` that resolves to nothing (repo_root-relative form,
    not the old cwd-relative one) must be caught BEFORE `Popen` -- the prior
    code had no way to distinguish "found the file, spawned it" from
    "resolved to nothing, spawned a doomed interpreter anyway"; both
    returned `True`. This is the check that turns the class of defect this
    whole row exists to close into a logged, attributable failure instead
    of a silent one."""
    with mock.patch.object(detached_spawn.subprocess, "Popen") as mock_popen:
        ok = detached_spawn.spawn_detached(repo_root, "no-such-script.py")

    assert ok is False
    mock_popen.assert_not_called()
    log_path = detached_spawn.housekeeping_failures_log_path(repo_root)
    line = log_path.read_text(encoding="utf-8").splitlines()[0]
    assert "SPAWN FAILED" in line
    assert "FileNotFoundError" in line
    assert str(Path(repo_root) / "no-such-script.py") in line


def test_popen_exception_is_swallowed(repo_root: str, existing_script: str) -> None:
    with mock.patch.object(detached_spawn.subprocess, "Popen", side_effect=OSError("boom")):
        ok = detached_spawn.spawn_detached(repo_root, existing_script, ["--y"])

    assert ok is False


def test_failure_is_logged(repo_root: str, existing_script: str) -> None:
    with mock.patch.object(detached_spawn.subprocess, "Popen", side_effect=OSError("boom")):
        detached_spawn.spawn_detached(repo_root, existing_script, ["--y"])

    log_path = detached_spawn.housekeeping_failures_log_path(repo_root)
    assert log_path == Path(repo_root, "state", "housekeeping-failures.log")
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("[")
    assert "SPAWN FAILED" in line
    # `script_path` is now resolved against `repo_root` (this row's own fix),
    # never against `os.path.abspath` / the caller's cwd.
    assert "script=%s" % (str(Path(repo_root) / existing_script),) in line
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


# ---------------------------------------------------------------------------
# drain-then-retain: clear_failures_log appends to the archive before truncating
# ---------------------------------------------------------------------------


def test_clear_appends_content_to_archive_and_empties_live_log(repo_root: str) -> None:
    detached_spawn.record_child_failure(repo_root, "/x.py", exit_code=1)
    live_content = detached_spawn.read_failures_log(repo_root)

    detached_spawn.clear_failures_log(repo_root)

    assert detached_spawn.read_failures_log(repo_root) == ""
    archive_path = detached_spawn.housekeeping_failures_archive_path(repo_root)
    assert archive_path == Path(repo_root, "state", "housekeeping-failures.archive.log")
    archive_content = archive_path.read_text(encoding="utf-8")
    assert live_content.strip() in archive_content


def test_clear_archive_block_has_drained_header(repo_root: str) -> None:
    detached_spawn.record_child_failure(repo_root, "/x.py", exit_code=1)

    detached_spawn.clear_failures_log(repo_root)

    archive_path = detached_spawn.housekeeping_failures_archive_path(repo_root)
    archive_content = archive_path.read_text(encoding="utf-8")
    assert "--- drained " in archive_content
    assert archive_content.startswith("--- drained ")


def test_clear_twice_accumulates_both_records_in_archive(repo_root: str) -> None:
    detached_spawn.record_child_failure(repo_root, "/first.py", exit_code=1)
    detached_spawn.clear_failures_log(repo_root)

    detached_spawn.record_child_failure(repo_root, "/second.py", exit_code=2)
    detached_spawn.clear_failures_log(repo_root)

    archive_path = detached_spawn.housekeeping_failures_archive_path(repo_root)
    archive_content = archive_path.read_text(encoding="utf-8")
    assert archive_content.count("--- drained ") == 2
    assert "first.py" in archive_content
    assert "second.py" in archive_content


def test_clear_empty_or_whitespace_log_writes_nothing_to_archive(repo_root: str) -> None:
    log_path = detached_spawn.housekeeping_failures_log_path(repo_root)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("   \n\n  ", encoding="utf-8")

    detached_spawn.clear_failures_log(repo_root)

    archive_path = detached_spawn.housekeeping_failures_archive_path(repo_root)
    assert not archive_path.exists()
    assert detached_spawn.read_failures_log(repo_root) == ""


def test_clear_missing_live_log_does_not_raise_and_no_archive_written(repo_root: str) -> None:
    detached_spawn.clear_failures_log(repo_root)  # must not raise

    archive_path = detached_spawn.housekeeping_failures_archive_path(repo_root)
    assert not archive_path.exists()


def test_archive_cap_trims_and_always_starts_on_a_drained_header(
    repo_root: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(detached_spawn, "_FAILURES_ARCHIVE_MAX_BYTES", 200)

    for i in range(10):
        detached_spawn.record_child_failure(repo_root, f"/script{i}.py", exit_code=1)
        detached_spawn.clear_failures_log(repo_root)

    archive_path = detached_spawn.housekeeping_failures_archive_path(repo_root)
    archive_content = archive_path.read_text(encoding="utf-8")
    assert len(archive_content.encode("utf-8")) <= 200 + len("--- drained YYYY-MM-DDTHH:MM:SSZ ---\n") + 200
    assert archive_content.startswith("--- drained ")
    # No mid-record fragment: the content up to the first header boundary is
    # itself a complete header line.
    first_line = archive_content.splitlines()[0]
    assert first_line.startswith("--- drained ") and first_line.endswith("---")


def test_clear_never_raises_when_archive_path_is_a_directory(repo_root: str) -> None:
    detached_spawn.record_child_failure(repo_root, "/x.py", exit_code=1)
    assert detached_spawn.read_failures_log(repo_root) != ""

    archive_path = detached_spawn.housekeeping_failures_archive_path(repo_root)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.mkdir()  # archive path itself is a directory -> unwritable as a file

    detached_spawn.clear_failures_log(repo_root)  # must not raise

    assert detached_spawn.read_failures_log(repo_root) == ""


# ---------------------------------------------------------------------------
# Per-reader delivery: the shared log is not a lottery
# ---------------------------------------------------------------------------


class TestPerReaderDelivery:
    """~50 sessions read one shared failures log. Clearing it on delivery meant
    the first session to regenerate its orientation cache consumed every record
    and the other ~49 never saw them -- surfacing was a lottery, and a record was
    almost always cleared by a session other than the one that would display it.
    """

    def _log(self, root, text):
        path = detached_spawn.housekeeping_failures_log_path(str(root))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def test_every_reader_sees_every_record(self, tmp_path):
        self._log(tmp_path, "[t] SPAWN FAILED one\n")
        for reader in ("sid-a", "sid-b", "sid-c"):
            seen = detached_spawn.read_failures_log(str(tmp_path), cursor_key=reader)
            assert "one" in seen, (
                f"{reader} never saw a record another reader had already consumed"
            )
            detached_spawn.advance_failures_cursor(str(tmp_path), reader)

    def test_delivery_is_once_per_reader(self, tmp_path):
        self._log(tmp_path, "[t] SPAWN FAILED one\n")
        assert "one" in detached_spawn.read_failures_log(str(tmp_path), cursor_key="sid-a")
        detached_spawn.advance_failures_cursor(str(tmp_path), "sid-a")
        assert detached_spawn.read_failures_log(str(tmp_path), cursor_key="sid-a") == ""

        self._log(tmp_path, "[t] SPAWN FAILED two\n")
        fresh = detached_spawn.read_failures_log(str(tmp_path), cursor_key="sid-a")
        assert "two" in fresh
        assert "one" not in fresh

    def test_reading_never_mutates_the_shared_log(self, tmp_path):
        self._log(tmp_path, "[t] SPAWN FAILED one\n")
        log = detached_spawn.housekeeping_failures_log_path(str(tmp_path))
        before = log.read_text(encoding="utf-8")
        detached_spawn.read_failures_log(str(tmp_path), cursor_key="sid-a")
        detached_spawn.advance_failures_cursor(str(tmp_path), "sid-a")
        assert log.read_text(encoding="utf-8") == before

    def test_a_trimmed_log_restarts_rather_than_delivering_nothing(self, tmp_path):
        """Over-delivering a record is arguable to a reader; a vanished one is
        arguable to no one."""
        self._log(tmp_path, "[t] SPAWN FAILED one\n" * 40)
        detached_spawn.read_failures_log(str(tmp_path), cursor_key="sid-a")
        detached_spawn.advance_failures_cursor(str(tmp_path), "sid-a")

        detached_spawn.clear_failures_log(str(tmp_path))
        self._log(tmp_path, "[t] SPAWN FAILED post-trim\n")
        assert "post-trim" in detached_spawn.read_failures_log(
            str(tmp_path), cursor_key="sid-a"
        )

    def test_uncursored_read_still_returns_everything(self, tmp_path):
        """The --check dry-run path passes no cursor and must stay unchanged."""
        self._log(tmp_path, "[t] SPAWN FAILED one\n")
        assert "one" in detached_spawn.read_failures_log(str(tmp_path))
        assert "one" in detached_spawn.read_failures_log(str(tmp_path))

    def test_trim_is_a_noop_under_the_cap(self, tmp_path):
        self._log(tmp_path, "[t] SPAWN FAILED one\n")
        detached_spawn.trim_failures_log(str(tmp_path))
        log = detached_spawn.housekeeping_failures_log_path(str(tmp_path))
        assert "one" in log.read_text(encoding="utf-8")

    def test_trim_empties_the_log_past_the_cap(self, tmp_path):
        bulk = "[t] SPAWN FAILED bulk\n" * (detached_spawn._FAILURES_LOG_MAX_BYTES // 20 + 100)
        self._log(tmp_path, bulk)
        detached_spawn.trim_failures_log(str(tmp_path))

        log = detached_spawn.housekeeping_failures_log_path(str(tmp_path))
        assert log.read_text(encoding="utf-8") == ""
        archive = detached_spawn.housekeeping_failures_archive_path(str(tmp_path))
        assert "bulk" in archive.read_text(encoding="utf-8", errors="replace"), (
            "retention must survive the trim -- a drain without history answers "
            "no failure-rate question"
        )
