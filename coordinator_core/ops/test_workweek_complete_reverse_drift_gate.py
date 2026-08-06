"""
test_workweek_complete_reverse_drift_gate.py — pytest unit tests for
coordinator_core.ops.workweek_reverse_drift_gate.

Port source: coordinator/commands/workweek-complete.md § Step 4g (example-doctrine-repo),
the per-plugin execution loop with 3-way rc branching — reauthored here as
direct in-process calls against the ported Python module's run_gate()/main(),
monkeypatching the discovery half (_run) so these tests exercise ONLY the
execution-loop/rc-fold logic this module owns.

Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Isolation: every test monkeypatches
coordinator_core.ops.workweek_reverse_drift_gate._run — no real registry file
or subprocess touches the operator's real machine-local state. The one test
that exercises a real subprocess (test_run_gate_runs_real_command) uses a
trivial `true`/`false`-shaped inline shell command, not a real
reverse_drift_cmd.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import pytest

from coordinator_core.ops import workweek_reverse_drift_gate as gate_mod
from coordinator_core.ops.workweek_reverse_drift_gate import main, run_gate


def _stub_run(stdout_lines: List[str], stderr_lines: List[str], rc: int):
    def _fake(scope_repo: Optional[str]) -> Tuple[List[str], List[str], int]:
        return list(stdout_lines), list(stderr_lines), rc
    return _fake


def test_run_gate_na_no_copy_install_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc==0 with zero rows (no copy_install plugins) is a clean pass."""
    monkeypatch.setattr(gate_mod, "_run", _stub_run([], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 0
    assert messages == []


def test_run_gate_misconfigured_rc3_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """rc==3 (copy_install plugins exist, none carry reverse_drift_cmd) fails
    the gate and reports the MISCONFIGURED message."""
    monkeypatch.setattr(gate_mod, "_run", _stub_run([], [], 3))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 1
    assert any("MISCONFIGURED" in m for m in messages)


def test_run_gate_misconfigured_rc3_override_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same rc==3 misconfiguration does not block when override=True —
    matches the bash oracle's per-branch `|| exit 1` override check."""
    monkeypatch.setattr(gate_mod, "_run", _stub_run([], [], 3))
    messages, rc = run_gate("/some/repo", override=True)
    assert rc == 0
    assert any("MISCONFIGURED" in m for m in messages)


def test_run_gate_reader_error_unexpected_rc_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader rc outside {0, 3} (e.g. 2 == invocation/parse error) fails the
    gate and reports the unexpected-code message."""
    monkeypatch.setattr(gate_mod, "_run", _stub_run([], [], 2))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 1
    assert any("unexpected code (rc=2)" in m for m in messages)


def test_run_gate_reader_error_unexpected_rc_override_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate_mod, "_run", _stub_run([], [], 2))
    messages, rc = run_gate("/some/repo", override=True)
    assert rc == 0
    assert any("unexpected code (rc=2)" in m for m in messages)


def test_run_gate_runs_real_command_success(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """rc==0 with a runnable row that succeeds is a clean pass — exercises
    the real subprocess execution path (bash -euo pipefail -c) against a
    trivial `true`-shaped command."""
    row = f"pluginA|{tmp_path}|true"
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 0
    assert messages == []


def test_run_gate_runs_real_command_failure_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A row whose reverse_drift_cmd exits non-zero fails the gate and
    reports the plugin name + FAILED remediation message."""
    row = f"pluginA|{tmp_path}|false"
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 1
    assert any("pluginA" in m and "failed" in m for m in messages)
    assert any("FAILED" in m for m in messages)


def test_run_gate_runs_real_command_failure_override_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    row = f"pluginA|{tmp_path}|false"
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=True)
    assert rc == 0
    assert any("FAILED" in m for m in messages)


def test_run_gate_multiple_rows_one_fails_still_reports_both(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Multiple rows: one passing, one failing — the loop must not short
    circuit, and the aggregate must fail."""
    rows = [f"pluginA|{tmp_path}|true", f"pluginB|{tmp_path}|false"]
    monkeypatch.setattr(gate_mod, "_run", _stub_run(rows, [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 1
    assert any("pluginB" in m for m in messages)
    assert not any("pluginA" in m and "failed" in m for m in messages)


def test_run_gate_forwards_reader_stderr_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reader-level warnings (e.g. double-quote advisory) are forwarded even
    on the clean rc==0 pass — they are informational, not blocking."""
    monkeypatch.setattr(
        gate_mod, "_run", _stub_run([], ["pluginA: WARNING: double-quote detected"], 0)
    )
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 0
    assert any("WARNING" in m for m in messages)


def test_main_scope_repo_flag_requires_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate_mod, "_run", _stub_run([], [], 0))
    rc = main(["--scope-repo"])
    assert rc == 2


def test_main_unknown_argument_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate_mod, "_run", _stub_run([], [], 0))
    rc = main(["--bogus"])
    assert rc == 2


def test_main_help_exits_0(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--help"])
    assert rc == 0
    assert "Usage" in capsys.readouterr().out


def test_run_gate_shell_metachar_pipe_refused(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A reverse_drift_cmd containing a pipe is refused (fail loud, no shell
    spawn) rather than mis-split into a flat argv and silently run wrong —
    docs/2026-07-29-debash-residual-sites-spec.md Group D."""
    row = f"pluginA|{tmp_path}|git diff | grep foo"
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 1
    assert any("pluginA" in m and "shell metacharacter" in m for m in messages)


def test_run_gate_shell_metachar_command_substitution_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    row = f"pluginA|{tmp_path}|echo $(git rev-parse HEAD)"
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 1
    assert any("shell metacharacter" in m for m in messages)


def test_run_gate_shell_metachar_semicolon_refused_override_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    row = f"pluginA|{tmp_path}|true; false"
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=True)
    assert rc == 0
    assert any("shell metacharacter" in m for m in messages)


def test_run_gate_plain_argv_with_quoted_args_runs_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A plain argument vector (no shell metacharacters) — including one with
    shell-style quoting a plugin author would write for a multi-word arg — is
    shlex.split and executed directly, no shell spawned."""
    row = f'pluginA|{tmp_path}|python -c "import sys" --flag "value with spaces"'
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 0
    assert messages == []


def test_run_gate_empty_cmd_after_parse_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A whitespace-only reverse_drift_cmd parses to an empty argv — matches
    the retired bash oracle's no-op-empty-script behavior (no failure)."""
    row = f"pluginA|{tmp_path}|   "
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 0
    assert messages == []


def test_shell_metachar_check_detects_each_guarded_char() -> None:
    from coordinator_core.ops.workweek_reverse_drift_gate import _shell_metachar_check

    assert _shell_metachar_check("a | b") == "|"
    assert _shell_metachar_check("a && b") == "&&"
    assert _shell_metachar_check("a || b") == "||"
    assert _shell_metachar_check("a; b") == ";"
    assert _shell_metachar_check("a > b") == ">"
    assert _shell_metachar_check("a < b") == "<"
    assert _shell_metachar_check("a `b`") == "`"
    assert _shell_metachar_check("a $(b)") == "$("
    assert _shell_metachar_check("plain --flag value") is None
    assert _shell_metachar_check("echo $HOME") is None  # bare $ not guarded


def test_main_default_scope_repo_resolves_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When --scope-repo is omitted, main() must resolve a concrete repo root
    (not silently widen to all-plugins legacy behavior) before calling
    run_gate — verified by asserting _run receives a non-None scope_repo."""
    captured = {}

    def _fake_run(scope_repo: Optional[str]) -> Tuple[List[str], List[str], int]:
        captured["scope_repo"] = scope_repo
        return [], [], 0

    monkeypatch.setattr(gate_mod, "_run", _fake_run)
    rc = main([])
    assert rc == 0
    assert captured["scope_repo"] is not None
