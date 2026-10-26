"""run_recorded_falsifier: pass on exit 0, not_met on nonzero, never a pass when indeterminate."""
from __future__ import annotations

from coordinator_core.execute_plan_assemble.falsifier_run import run_recorded_falsifier


def test_exit_zero_command_passes(tmp_path):
    obs = run_recorded_falsifier(
        "run `python -c \"print('ok')\"` and see exit 0", repo_root=tmp_path, timeout_s=30
    )
    assert obs.ran is True
    assert obs.passed is True
    assert obs.exit_code == 0
    assert "ok" in obs.output


def test_nonzero_exit_is_not_met_with_exit_code(tmp_path):
    obs = run_recorded_falsifier(
        "run `python -c \"import sys; sys.exit(3)\"`", repo_root=tmp_path, timeout_s=30
    )
    assert obs.ran is True
    assert obs.passed is False
    assert obs.exit_code == 3


def test_no_backticked_command_is_never_a_pass(tmp_path):
    obs = run_recorded_falsifier("eyeball it, no command here", repo_root=tmp_path, timeout_s=30)
    assert obs.ran is False
    assert obs.passed is None
    assert obs.exit_code is None


def test_unparseable_quoting_is_never_a_pass(tmp_path):
    obs = run_recorded_falsifier("run `python -c \"unterminated`", repo_root=tmp_path, timeout_s=30)
    assert obs.ran is False
    assert obs.passed is None


def test_unlaunchable_command_is_never_a_pass(tmp_path):
    obs = run_recorded_falsifier(
        "run `no-such-binary-xyz-falsifier --flag`", repo_root=tmp_path, timeout_s=30
    )
    assert obs.ran is False
    assert obs.passed is None
