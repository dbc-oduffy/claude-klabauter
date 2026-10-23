"""A configured test command plus appended test files / node ids is a named run (Tier T),
not the configured tier; a directory, a bare marker or a configured directory keeps the tier.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py (_segment_contains)
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.guard_messages.testing import envelope_of

_AGENT_ID = "a0123456789abcdef"
_RUNNER = "python3 bin/run-fast-tests.py"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n" 'testpaths = ["tests"]\n', encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_is_cloud_box", lambda payload: False)
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    return tmp_path


def _configure(monkeypatch, cmd):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", cmd)
    monkeypatch.setattr(
        guard, "_configured_test_cmds",
        lambda root: [guard.ConfiguredCmd("fast_test_cmd", cmd, 0)],
    )


def _subagent_verdict(command, repo):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(repo),
        "agent_id": _AGENT_ID,
    }
    return envelope_of(guard.check(payload))


def _denied(verdict):
    if verdict is None:
        return False
    return verdict.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


@pytest.mark.parametrize("command", [
    f"{_RUNNER} tests/test_a.py tests/test_b.py::test_x",
    f"{_RUNNER} -q tests/test_a.py",
    f"{_RUNNER} -k foo tests/test_a.py",
])
def test_named_files_on_configured_runner_are_a_named_run(repo, monkeypatch, command):
    _configure(monkeypatch, _RUNNER)
    assert not _denied(_subagent_verdict(command, repo))
    assert guard.classify_command(command, cwd=str(repo)) == []


@pytest.mark.parametrize("command", [
    _RUNNER,
    f"{_RUNNER} -x",
    f"{_RUNNER} -k foo",
    f'{_RUNNER} -m "not slow"',
    f"{_RUNNER} tests/",
    f"{_RUNNER} tests/ tests/test_a.py",
])
def test_unnarrowed_configured_runner_stays_the_tier_and_denied(repo, monkeypatch, command):
    _configure(monkeypatch, _RUNNER)
    assert _denied(_subagent_verdict(command, repo))
    assert guard.classify_command(command, cwd=str(repo)) != []


def test_configured_directory_still_runs_in_full_with_a_file_appended(repo, monkeypatch):
    _configure(monkeypatch, "pytest tests/")
    command = "pytest tests/ tests/test_a.py"
    assert _denied(_subagent_verdict(command, repo))
    assert guard.classify_command(command, cwd=str(repo)) != []
