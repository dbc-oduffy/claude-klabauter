"""Tests for `coordinator_core.warm.server`'s boot-crash reporting guard.

Spec backlink: state/subagent-share/1c9c881e-02dd-4b91-b65c-5275748a2fa4/
coordinatorexecutor-f461a99c.md's BLINDFOLD #1 -- before this guard,
`main()` had no top-level try/except: a crash between resolving the engine
clone and `serve_forever`'s own setup wrote an uncaught traceback to
`sys.stderr`, which `detached_spawn.spawn_detached` opens as
`subprocess.DEVNULL` for the real spawn path, and reached no log, no
telemetry, nothing.

`test_main_reports_run_guarded_crash_and_reraises` proves the LOGIC
in-process (fast, deterministic, no subprocess): a `_run_guarded` failure
is reported via `record_child_failure` before `main()` re-raises.

`test_boot_crash_surfaces_under_devnull_stdio` proves the property the
audit's fix is actually FOR: that the report reaches disk even when the
process's own stdio is fully redirected to DEVNULL, exactly as
`detached_spawn.spawn_detached` configures every real detached child. This
is the one claim an in-process test cannot make on its own -- DEVNULL
redirection only exists at the OS-process boundary -- so it runs the
guard in a real subprocess. It does NOT start a warm server: `_run_guarded`
is monkeypatched to raise before any election, pipe bind, or breadcrumb
write happens, so no `%LOCALAPPDATA%\\coordinator\\warm\\` breadcrumb
directory is ever created and there is nothing to clean up afterward.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.warm import server

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_main_reports_run_guarded_crash_and_reraises(monkeypatch):
    """A `_run_guarded` failure must reach `record_child_failure` with the
    real exception, and `main()` must still raise -- not swallow it into a
    quiet non-zero return, per PM ruling: die audibly, not gracefully."""
    boom = RuntimeError("boot exploded before election")

    def _raise():
        raise boom

    monkeypatch.setattr(server, "_run_guarded", _raise)
    monkeypatch.setattr(server.sys, "platform", "win32")

    recorded = {}

    def _fake_record_child_failure(repo_root, script_path, *, exit_code=None, exc=None):
        recorded["repo_root"] = repo_root
        recorded["script_path"] = script_path
        recorded["exc"] = exc

    monkeypatch.setattr(
        "coordinator_core.ops.ceremony.detached_spawn.record_child_failure",
        _fake_record_child_failure,
    )

    with pytest.raises(RuntimeError):
        server.main()

    assert recorded["exc"] is boom
    assert recorded["script_path"] == server.__file__


def test_boot_crash_surfaces_under_devnull_stdio(tmp_path):
    """Real-subprocess proof: a `_run_guarded` crash still lands a "CHILD
    FAILED" record in `state/housekeeping-failures.log` when the child's
    own stdin/stdout/stderr are all `subprocess.DEVNULL`, matching
    `detached_spawn.spawn_detached`'s exact stdio configuration for every
    real spawn."""
    repo_root = Path(server.__file__).resolve().parents[2]
    fake_repo_root = tmp_path / "fake-repo-root"
    fake_repo_root.mkdir()

    worker = tmp_path / "_boot_crash_worker.py"
    worker.write_text(
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from coordinator_core.warm import server\n"
        "def _raise():\n"
        "    raise RuntimeError('synthetic boot crash under DEVNULL')\n"
        "server._run_guarded = _raise\n"
        "server._engine_clone_root = lambda: %r\n"
        "sys.exit(server.main())\n"
        % (str(repo_root), str(fake_repo_root)),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(worker)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert proc.returncode != 0  # died, audibly -- not a quiet 0

    log_path = fake_repo_root / "state" / "housekeeping-failures.log"
    content = log_path.read_text(encoding="utf-8")
    assert "CHILD FAILED" in content
    assert "RuntimeError" in content
    assert "synthetic boot crash under DEVNULL" in content
