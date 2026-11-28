"""The ask verify-tests leg runs test_leg_run detached and awaited on exit; the judge never excuses a zero-test leg."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose


def _tests_call() -> str:
    return ask_compose._verify_tests_call(
        head="H", anchor="A", test_js=ask_compose._test_instruction_js("HEAD", "REPO"), run_id="r1",
        repo_root="REPO", out_path="/run/test-leg.json", base_sha="abc123",
    )


def test_the_leg_runs_test_leg_run_in_the_background_and_reads_its_out_file():
    call = _tests_call()
    assert "coordinator_core.ops.test_leg_run" in call
    assert "--out /run/test-leg.json -- " in call and "--base-sha abc123" in call
    assert "run_in_background: true" in call
    assert "PowerShell:" in call and "Run once using the native shell" in call
    assert 'PYTHONPATH=' not in call
    assert "(_manifest.test_targets ?? []).map(" in call
    assert "--measured --run-id r1" in call
    assert "Read /run/test-leg.json" in call and "copy status, tests_run, and tests_failed" in call


def test_the_leg_carries_no_deadline_clause_and_no_bash_timeout():
    call = _tests_call()
    assert "Deadline:" not in call and "Bash tool timeout" not in call and "timeout:" not in call
    assert "no timeout parameter" in call


def test_no_js_race_bound_wraps_the_verify_tests_call():
    import inspect

    src = inspect.getsource(ask_compose)
    at = src.index("_VERIFY_TEST_RESULT_VAR} = await {verify_tests_call}")
    window = src[at - 400 : at + 400]
    assert "Promise.race" not in window and "VERIFY_WALL_BUDGET_S" not in window and "bounded_js" not in window


def test_the_judge_rules_hold_a_zero_test_leg_not_met_and_keep_stall_only_for_other_legs():
    rules = ask_compose._LEDGER_ITEMS_RULES.replace("\n", " ")
    assert "is inconclusive evidence, never" not in rules
    assert "tests_run 0" in rules and "never stall_only" in rules
    assert "Any other verify leg that stalled or timed out" in rules and "set stall_only true" in rules


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_emitted_verifier_runs_from_foreign_cwd(shell, tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify

    executable = shutil.which("bash" if shell == "bash" else "pwsh")
    if executable is None and shell == "bash" and os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            executable = str(git_bash)
    if executable is None:
        pytest.skip(f"{shell} is unavailable")
    monkeypatch.setattr(clean_overlay_verify, "_gate_python", lambda _: sys.executable)
    target = tmp_path / "test_shell.py"
    target.write_text(
        "from coordinator_core.ops import test_leg_run\n\ndef test_pass():\n    assert test_leg_run.__file__\n",
        encoding="utf-8",
    )
    out = tmp_path / "test leg's receipt.json"
    quote = shlex.quote if shell == "bash" else ask_compose._powershell_quote
    command = ask_compose.engine_module_command(
        "coordinator_core.ops.test_leg_run", str(tmp_path),
        f"--out {quote(out.as_posix())} -- {quote(target.name)}", shell=shell,
    )
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    flags = ["-c"] if shell == "bash" else ["-NoProfile", "-Command"]
    proc = subprocess.run([executable, *flags, command], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    receipt = json.loads(out.read_text(encoding="utf-8"))
    assert receipt["tests_run"] == 1 and receipt["pytest_exit_code"] == 0, receipt
