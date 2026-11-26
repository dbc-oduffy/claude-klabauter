"""A pytest path list held in a shell variable assigned literal test files earlier in the same
command line is classified like the inline paths, for the EM and for a dispatched agent; an
unassigned, prefix-assigned or broad-valued variable keeps its refused verdict.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py (bind_literal_operands)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.git import git_state
from coordinator_core.guard_messages.testing import envelope_of

_AGENT = "a0123456789abcdef"
_A = "tests/unit/test_a.py"
_B = "tests/unit/test_b.py"


@pytest.fixture(autouse=True)
def _isolate_leg_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.bash_guards import _test_run_admission as adm

    monkeypatch.setattr(adm, "over_admit_ceiling", lambda payload: None)
    monkeypatch.setattr(adm, "test_run_verdict", lambda *a, **k: adm.TestRunVerdict(True))


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (tmp_path / ".git").mkdir()
    unit = tmp_path / "tests" / "unit"
    unit.mkdir(parents=True)
    for name in ("test_a.py", "test_b.py"):
        (unit / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(git_state, "read_index_paths", lambda _repo: frozenset())
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_is_cloud_box", lambda payload: False)
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    return tmp_path


def _decision(repo: Path, command: str, agent_id: str | None = None) -> str:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(repo),
    }
    if agent_id:
        payload["agent_id"] = agent_id
    verdict = envelope_of(guard.check(payload))
    return "none" if verdict is None else verdict["hookSpecificOutput"]["permissionDecision"]


_ASSIGNED_TWO_FILES = [
    f'FILES="{_A} {_B}"; pytest $FILES',
    f'FILES="{_A} {_B}"; python -m pytest -q ${{FILES}}',
    f'FILES="{_A} {_B}" && python -m pytest $FILES',
    f"FILES='{_A} {_B}'\npytest $FILES",
]

_REFUSED = [
    "pytest $FILES",
    "FILES=$(git ls-files 'tests/*.py'); pytest $FILES",
    f"FILES={_A} pytest $FILES",
    f'FILES="{_A} {_B}"; FILES=tests; pytest $FILES',
    "FILES=tests; pytest $FILES",
    f'FILES="{_A} tests"; pytest $FILES',
    f'false && FILES="{_A} {_B}"; pytest $FILES',
]


@pytest.mark.parametrize("command", _ASSIGNED_TWO_FILES)
def test_em_assigned_variable_of_two_literal_files_is_admitted(repo, command) -> None:
    assert _decision(repo, command) == "none"


@pytest.mark.parametrize("command", _ASSIGNED_TWO_FILES)
def test_subagent_assigned_variable_of_two_literal_files_is_rewritten_not_denied(repo, command) -> None:
    assert _decision(repo, command, _AGENT) == "allow"


@pytest.mark.parametrize("agent_id", [None, _AGENT])
@pytest.mark.parametrize("command", _REFUSED)
def test_unassigned_or_unresolvable_variable_keeps_its_refused_verdict(repo, command, agent_id) -> None:
    assert _decision(repo, command, agent_id) == "deny"


def test_assigned_variable_classifies_like_the_inline_paths(repo) -> None:
    inline = guard.classify_command(f"pytest {_A} {_B}", cwd=str(repo))
    held = guard.classify_command(f'FILES="{_A} {_B}"; pytest $FILES', cwd=str(repo))
    assert [m.tier for m in held] == [m.tier for m in inline]
