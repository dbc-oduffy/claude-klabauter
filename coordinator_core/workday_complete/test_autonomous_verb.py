"""
coordinator_core.workday_complete.test_autonomous_verb — regression coverage for
the exit-120/silent-stdout defect reported in
state/cross-repo/inbox/2026-09-24-example-retrieval-repo-ue-addon-em-autonomous-verb-exit-120.md.

The reported symptom: `autonomous-verb.exe` (no arg, or `on`) exited 120 with
NO stdout and NO stderr. `_run_sentinel_cli` used to spawn the
`misc-session-and-guards autonomous-sentinel` child with its stdout/stderr left
to inherit this process's own OS-level std handles rather than being captured
and re-emitted through `print()` — invisible on the warm leg, where
`ops.invoke_from_argv._run_entrypoint` retargets only the Python-level
`sys.stdout`/`sys.stderr` objects via `contextlib.redirect_stdout`/
`redirect_stderr`, never the OS fd. A nonzero child exit (120 or otherwise)
also carried no stderr diagnostic at all — main() only printed on success.

These tests exercise `_run_sentinel_cli` and `main()` against a fake
`misc-session-and-guards` stand-in (never a real subprocess, never touching
git) so they stay fast and deterministic, and never spawn a live warm
door/server.

Run scoped only:
    python3 -m pytest coordinator_core/workday_complete/test_autonomous_verb.py -q
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.workday_complete import autonomous_verb


def _write_fake_sentinel_cli(tmp_path: Path, *, exit_code: int, stdout: str, stderr: str) -> Path:
    """A standalone script mimicking `misc-session-and-guards.py autonomous-sentinel`:
    writes `stdout`/`stderr` (if any) and exits `exit_code`. Run as a real
    subprocess via `sys.executable`, so this covers the actual OS-level
    stdio-capture path, not just Python-level monkeypatching."""
    script = tmp_path / "fake_sentinel_cli.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            stdout = {stdout!r}
            stderr = {stderr!r}
            if stdout:
                sys.stdout.write(stdout)
            if stderr:
                sys.stderr.write(stderr)
            sys.exit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    return script


def test_run_sentinel_cli_relays_child_stdout_to_caller(tmp_path, monkeypatch, capsys):
    """The sentinel-path confirmation line the child prints on success (memo ask
    2: "the verb prints where it wrote the sentinel") must reach THIS
    process's own stdout — not be swallowed because the child's OS-level
    stdout was left to inherit an unrelated handle."""
    script = _write_fake_sentinel_cli(
        tmp_path, exit_code=0, stdout="/tmp/autonomous-run-abc123\n", stderr=""
    )
    monkeypatch.setattr(autonomous_verb, "resolve_launchable", lambda s: [sys.executable, s])
    monkeypatch.setattr(autonomous_verb, "resolve_sentinel_cli", lambda: str(script))

    rc = autonomous_verb._run_sentinel_cli(["autonomous-sentinel", "enable", "--mode", "autonomous"])

    assert rc == 0
    out = capsys.readouterr()
    assert "/tmp/autonomous-run-abc123" in out.out


def test_run_sentinel_cli_fails_loud_on_nonzero_exit(tmp_path, monkeypatch, capsys):
    """Memo ask 1: on any failure, a message on stderr — never a bare nonzero
    return with nothing printed anywhere."""
    script = _write_fake_sentinel_cli(tmp_path, exit_code=1, stdout="", stderr="boom\n")
    monkeypatch.setattr(autonomous_verb, "resolve_launchable", lambda s: [sys.executable, s])
    monkeypatch.setattr(autonomous_verb, "resolve_sentinel_cli", lambda: str(script))

    rc = autonomous_verb._run_sentinel_cli(["autonomous-sentinel", "enable", "--mode", "autonomous"])

    assert rc == 1
    out = capsys.readouterr()
    assert "boom" in out.err
    assert "exited 1" in out.err


def test_run_sentinel_cli_exit_120_reports_loudly_even_with_no_child_output(
    tmp_path, monkeypatch, capsys
):
    """The exact reported shape: the child exits 120 (CPython's own
    `Py_FinalizeEx` < 0 stdout-flush-failure status) and writes NOTHING to
    either stream. This wrapper must still name the failure on stderr rather
    than exiting silently."""
    script = _write_fake_sentinel_cli(tmp_path, exit_code=120, stdout="", stderr="")
    monkeypatch.setattr(autonomous_verb, "resolve_launchable", lambda s: [sys.executable, s])
    monkeypatch.setattr(autonomous_verb, "resolve_sentinel_cli", lambda: str(script))

    rc = autonomous_verb._run_sentinel_cli(["autonomous-sentinel", "enable", "--mode", "autonomous"])

    assert rc == 120
    out = capsys.readouterr()
    assert out.out == ""
    assert "exited 120" in out.err
    assert "wrote nothing to stderr" in out.err


def test_main_never_exits_120_itself(monkeypatch, capsys):
    """Memo ask 1: "on any failure, exit non-120". `main()`'s own reported exit
    status must never collide with CPython's reserved 120, even when the
    wrapped CLI's raw exit code was exactly that."""
    monkeypatch.setattr(autonomous_verb, "enable", lambda mode="autonomous": 120)

    rc = autonomous_verb.main(["on"])

    assert rc != 120
    assert rc == autonomous_verb._REMAPPED_120_EXIT
    # No false success line on a failure path.
    assert "enabled" not in capsys.readouterr().out


def test_main_success_path_unaffected_by_remap(monkeypatch, capsys):
    monkeypatch.setattr(autonomous_verb, "enable", lambda mode="autonomous": 0)

    rc = autonomous_verb.main(["on"])

    assert rc == 0
    assert "Autonomous mode enabled" in capsys.readouterr().out


def test_run_sentinel_cli_subprocess_launch_failure_still_reports_on_stderr(monkeypatch, capsys):
    """Pre-existing OSError/TimeoutExpired path stays fail-loud too."""

    def _boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(autonomous_verb, "resolve_launchable", lambda s: [sys.executable, s])
    monkeypatch.setattr(autonomous_verb, "resolve_sentinel_cli", lambda: "does-not-matter")
    monkeypatch.setattr(subprocess, "run", _boom)

    rc = autonomous_verb._run_sentinel_cli(["autonomous-sentinel", "disable"])

    assert rc == 3
    assert "invocation failed" in capsys.readouterr().err
