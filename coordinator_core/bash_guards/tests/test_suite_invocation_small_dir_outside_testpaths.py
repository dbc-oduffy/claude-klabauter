"""A single small pytest directory outside the testpaths roots is admitted for the EM and for a
dispatched agent; roots, ancestors, globs, a second directory and over-threshold or uncountable
directories stay refused. Counts come from a stubbed index read: no git spawn.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py (_bounded_outside_dir)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.git import git_state
from coordinator_core.guard_messages.testing import envelope_of

_T = guard._DIR_TEST_FILE_THRESHOLD
_D = "control/python-extract/tests"
_AGENT = "a0123456789abcdef"


def _populate(repo: Path, d: str, n: int) -> None:
    (repo / d).mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (repo / d / f"test_{i}.py").write_text("", encoding="utf-8")


def _track(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = frozenset(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file())
    monkeypatch.setattr(git_state, "read_index_paths", lambda _repo: snapshot)


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
    monkeypatch.setattr(git_state, "read_index_paths", lambda _repo: frozenset())
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_is_cloud_box", lambda payload: False)
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    return tmp_path


def _verdict(repo: Path, command: str, agent_id: str | None = None):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(repo),
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return envelope_of(guard.check(payload))


def _decision(repo: Path, command: str, agent_id: str | None = None) -> str:
    verdict = _verdict(repo, command, agent_id)
    return "none" if verdict is None else verdict["hookSpecificOutput"]["permissionDecision"]


def test_em_small_directory_outside_roots_is_admitted(repo, monkeypatch) -> None:
    _populate(repo, _D, 11)
    _track(repo, monkeypatch)
    assert _decision(repo, f"python3 -m pytest {_D}") == "none"
    assert _decision(repo, f"python -m pytest {_D}/") == "none"


def test_em_directory_at_threshold_admitted_one_over_denied(repo, monkeypatch) -> None:
    _populate(repo, _D, _T)
    _track(repo, monkeypatch)
    assert _decision(repo, f"pytest {_D}") == "none"
    _populate(repo, _D, _T + 1)
    _track(repo, monkeypatch)
    assert _decision(repo, f"pytest {_D}") == "deny"


def test_subagent_small_directory_outside_roots_is_rewritten_not_denied(repo, monkeypatch) -> None:
    _populate(repo, _D, 11)
    _track(repo, monkeypatch)
    verdict = _verdict(repo, f"python3 -m pytest {_D}", _AGENT)
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "with-tier-t-slot" in verdict["hookSpecificOutput"]["updatedInput"]["command"]


def test_subagent_over_threshold_directory_outside_roots_denied(repo, monkeypatch) -> None:
    _populate(repo, _D, _T + 1)
    _track(repo, monkeypatch)
    assert _decision(repo, f"pytest {_D}", _AGENT) == "deny"


@pytest.mark.parametrize("agent_id", [None, _AGENT])
@pytest.mark.parametrize("target", [".", "./", "tests", "tests/"])
def test_repo_root_testpaths_root_and_ancestors_stay_denied(repo, monkeypatch, agent_id, target) -> None:
    _populate(repo, _D, 1)
    _populate(repo, "tests", 1)
    _track(repo, monkeypatch)
    assert _decision(repo, f"pytest {target}", agent_id) == "deny"


@pytest.mark.parametrize("agent_id", [None, _AGENT])
def test_glob_and_second_directory_stay_denied(repo, monkeypatch, agent_id) -> None:
    _populate(repo, _D, 2)
    _populate(repo, "other/tests", 2)
    _track(repo, monkeypatch)
    assert _decision(repo, "pytest control/*/tests", agent_id) == "deny"
    assert _decision(repo, f"pytest {_D} other/tests", agent_id) == "deny"


@pytest.mark.parametrize("agent_id", [None, _AGENT])
def test_uncountable_directory_stays_denied(repo, agent_id) -> None:
    _populate(repo, _D, 1)
    assert _decision(repo, f"pytest {_D}", agent_id) == "deny"


@pytest.mark.parametrize("agent_id", [None, _AGENT])
def test_directory_with_no_tracked_tests_or_absent_on_disk_stays_denied(repo, monkeypatch, agent_id) -> None:
    (repo / "empty").mkdir()
    (repo / "empty" / "notes.txt").write_text("", encoding="utf-8")
    _populate(repo, "tests", 1)
    _track(repo, monkeypatch)
    assert _decision(repo, "pytest empty/", agent_id) == "deny"
    assert _decision(repo, "pytest missing/", agent_id) == "deny"


def test_directory_escaping_the_repo_stays_denied(repo, monkeypatch, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("elsewhere")
    _populate(outside, "tests", 1)
    _track(repo, monkeypatch)
    assert _decision(repo, f"pytest {outside / 'tests'}") == "deny"


def test_whole_suite_forms_stay_denied(repo, monkeypatch) -> None:
    _populate(repo, _D, 1)
    _track(repo, monkeypatch)
    assert _decision(repo, "pytest") == "deny"
    assert _decision(repo, f"pytest {_D} .") == "deny"


def test_small_directory_beside_a_file_is_admitted(repo, monkeypatch) -> None:
    _populate(repo, _D, 2)
    _populate(repo, "tests", 1)
    _track(repo, monkeypatch)
    assert _decision(repo, f"pytest {_D} tests/test_0.py") == "none"
