
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.workday_complete import autonomous_verb


def _write_fake_sentinel_cli(tmp_path: Path, *, exit_code: int, stdout: str, stderr: str) -> Path:
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
    monkeypatch.setattr(autonomous_verb, "enable", lambda mode="autonomous": 120)

    rc = autonomous_verb.main(["on"])

    assert rc != 120
    assert rc == autonomous_verb._REMAPPED_120_EXIT
    assert "enabled" not in capsys.readouterr().out


def test_main_success_path_unaffected_by_remap(monkeypatch, capsys):
    monkeypatch.setattr(autonomous_verb, "enable", lambda mode="autonomous": 0)

    rc = autonomous_verb.main(["on"])

    assert rc == 0
    assert "Autonomous mode enabled" in capsys.readouterr().out


def test_run_sentinel_cli_subprocess_launch_failure_still_reports_on_stderr(monkeypatch, capsys):

    def _boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(autonomous_verb, "resolve_launchable", lambda s: [sys.executable, s])
    monkeypatch.setattr(autonomous_verb, "resolve_sentinel_cli", lambda: "does-not-matter")
    monkeypatch.setattr(subprocess, "run", _boom)

    rc = autonomous_verb._run_sentinel_cli(["autonomous-sentinel", "disable"])

    assert rc == 3
    assert "invocation failed" in capsys.readouterr().err
