
from __future__ import annotations

from typing import List, Optional, Tuple

import pytest

from coordinator_core.ops import workweek_reverse_drift_gate as gate_mod
from coordinator_core.ops.workweek_reverse_drift_gate import main, run_gate

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _stub_run(stdout_lines: List[str], stderr_lines: List[str], rc: int):
    def _fake(scope_repo: Optional[str]) -> Tuple[List[str], List[str], int]:
        return list(stdout_lines), list(stderr_lines), rc
    return _fake


def test_run_gate_na_no_copy_install_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(gate_mod, "_run", _stub_run([], [], 3))
    messages, rc = run_gate("/some/repo", override=True)
    assert rc == 0
    assert any("MISCONFIGURED" in m for m in messages)


def test_run_gate_reader_error_unexpected_rc_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
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
    row = f"pluginA|{tmp_path}|true"
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 0
    assert messages == []


def test_run_gate_runs_real_command_failure_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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
    rows = [f"pluginA|{tmp_path}|true", f"pluginB|{tmp_path}|false"]
    monkeypatch.setattr(gate_mod, "_run", _stub_run(rows, [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 1
    assert any("pluginB" in m for m in messages)
    assert not any("pluginA" in m and "failed" in m for m in messages)


def test_run_gate_forwards_reader_stderr_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
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
    row = f'pluginA|{tmp_path}|python -c "import sys" --flag "value with spaces"'
    monkeypatch.setattr(gate_mod, "_run", _stub_run([row], [], 0))
    messages, rc = run_gate("/some/repo", override=False)
    assert rc == 0
    assert messages == []


def test_run_gate_empty_cmd_after_parse_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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
    assert _shell_metachar_check("echo $HOME") is None


def test_main_default_scope_repo_resolves_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def _fake_run(scope_repo: Optional[str]) -> Tuple[List[str], List[str], int]:
        captured["scope_repo"] = scope_repo
        return [], [], 0

    monkeypatch.setattr(gate_mod, "_run", _fake_run)
    rc = main([])
    assert rc == 0
    assert captured["scope_repo"] is not None
