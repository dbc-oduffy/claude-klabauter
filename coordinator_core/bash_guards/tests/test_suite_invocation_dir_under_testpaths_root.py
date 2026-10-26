"""An EM pytest directory strictly under a testpaths root is judged by its tracked test-file
count: at most ``_DIR_TEST_FILE_THRESHOLD`` stays Tier T, above it (summed over every directory
positional) or uncomputable is Tier U. Files and node ids stay Tier T; dispatched-agent (R9)
and JS-runner directory handling is unchanged. The count is an in-process index read: no spawn.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py
(_tracked_test_file_count, _em_dirs_over_threshold)
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.git import git_state
from coordinator_core.guard_messages.testing import envelope_of

_T = guard._DIR_TEST_FILE_THRESHOLD
_A = "coordinator_core/ops/alpha"
_B = "coordinator_core/ops/beta"


def _populate(repo, d, n, pattern="test_{i}.py"):
    (repo / d).mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (repo / d / pattern.format(i=i)).write_text("", encoding="utf-8")


_PATCH: list = []


def _track(repo):
    """Snapshot every file now on disk as the tracked set the index reader returns;
    files written afterwards stay untracked. Stubbed so the tests spawn no git."""
    snapshot = frozenset(
        p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file()
    )
    _PATCH[-1].setattr(git_state, "read_index_paths", lambda _repo: snapshot)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n" 'testpaths = ["coordinator_core"]\n', encoding="utf-8"
    )
    (tmp_path / ".git").mkdir()
    _PATCH.append(monkeypatch)
    monkeypatch.setattr(git_state, "read_index_paths", lambda _repo: frozenset())
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_is_cloud_box", lambda payload: False)
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    yield tmp_path
    _PATCH.pop()


def _payload(repo, command, agent_id=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": str(repo),
    }
    if agent_id:
        p["agent_id"] = agent_id
    return p


def _denied(repo, command, agent_id=None):
    verdict = envelope_of(guard.check(_payload(repo, command, agent_id)))
    if verdict is None:
        return False
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
    return True


def test_threshold_constant_is_fifty():
    assert _T == 50


def test_directory_with_exactly_threshold_files_is_allowed(repo):
    _populate(repo, _A, _T)
    _track(repo)
    assert not _denied(repo, f"pytest {_A}")


def test_directory_one_over_threshold_is_denied(repo):
    _populate(repo, _A, _T + 1)
    _track(repo)
    assert _denied(repo, f"pytest {_A}")


def test_denial_names_pytest_as_detected(repo):
    _populate(repo, _A, _T + 1)
    _track(repo)
    verdict = envelope_of(guard.check(_payload(repo, f"pytest {_A}")))
    assert "Detected: pytest" in verdict["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("first,second,denied", [(30, 21, True), (25, 25, False), (50, 1, True)])
def test_several_directories_are_summed(repo, first, second, denied):
    _populate(repo, _A, first)
    _populate(repo, _B, second)
    _track(repo)
    assert _denied(repo, f"pytest {_A} {_B}") is denied


def test_untracked_test_files_are_not_counted(repo):
    _populate(repo, _A, _T)
    _track(repo)
    _populate(repo, _A, 10, pattern="test_untracked_{i}.py")
    assert not _denied(repo, f"pytest {_A}")


def test_suffix_test_files_are_counted(repo):
    _populate(repo, _A, 26)
    _populate(repo, _A, 25, pattern="mod_{i}_test.py")
    _track(repo)
    assert _denied(repo, f"pytest {_A}")


def test_non_test_files_are_not_counted(repo):
    _populate(repo, _A, _T)
    _populate(repo, _A, 20, pattern="helper_{i}.py")
    _populate(repo, _A, 5, pattern="test_data_{i}.txt")
    _track(repo)
    assert not _denied(repo, f"pytest {_A}")


def test_nested_tests_count_toward_their_directory(repo):
    _populate(repo, _A + "/sub/deeper", _T + 1)
    _track(repo)
    assert _denied(repo, f"pytest {_A}")


def test_sibling_directory_sharing_a_name_prefix_is_not_counted(repo):
    _populate(repo, _A, 3)
    _populate(repo, _A + "_extra", _T + 10)
    _track(repo)
    assert not _denied(repo, f"pytest {_A}")


def test_unreadable_index_is_broad(repo, monkeypatch):
    _populate(repo, _A, 1)
    _track(repo)

    def boom(_repo):
        raise git_state.IndexParseError("corrupt")

    monkeypatch.setattr(git_state, "read_index_paths", boom)
    assert _denied(repo, f"pytest {_A}")


def test_empty_index_is_broad(repo):
    _populate(repo, _A, 1)
    assert _denied(repo, f"pytest {_A}")


def test_file_beside_over_threshold_directory_is_denied(repo):
    _populate(repo, _A, _T + 1)
    _populate(repo, _B, 1)
    _track(repo)
    assert _denied(repo, f"pytest {_A} {_B}/test_0.py")


def test_file_or_node_id_stays_tier_t_beside_a_large_directory_elsewhere(repo):
    _populate(repo, _A, _T + 1)
    _track(repo)
    assert not _denied(repo, f"pytest {_A}/test_0.py")
    assert not _denied(repo, f"pytest {_A}/test_0.py::test_x")


def test_directory_via_argsfile_is_counted(repo):
    _populate(repo, _A, _T + 1)
    _track(repo)
    af = repo / "args.txt"
    af.write_text(_A + "\n", encoding="utf-8")
    assert _denied(repo, f"pytest @{af}")


@pytest.mark.parametrize("root", ["coordinator_core", "coordinator_core/", "."])
def test_root_and_ancestor_stay_denied_before_any_count(repo, root):
    _populate(repo, _A, 1)
    _track(repo)
    assert _denied(repo, f"pytest {root}")


def test_directory_outside_testpaths_stays_denied(repo):
    _populate(repo, "elsewhere", 1)
    _track(repo)
    assert _denied(repo, "pytest elsewhere")


def test_glob_matching_several_files_is_still_judged_by_the_glob_rule(repo):
    _populate(repo, _A, 3)
    _track(repo)
    assert _denied(repo, f"pytest {_A}/test_*.py")


def test_glob_matching_one_file_is_still_allowed(repo):
    _populate(repo, _A, 3)
    _track(repo)
    assert not _denied(repo, f"pytest {_A}/test_0*.py")


def test_subagent_directory_deny_is_unchanged(repo):
    _populate(repo, _A, 1)
    _track(repo)
    assert _denied(repo, f"pytest {_A}", agent_id="a0123456789abcdef")


def test_subagent_classification_ignores_the_count(repo):
    _populate(repo, _A, _T + 1)
    _track(repo)
    token = guard._STRICT_DIRS.set(False)
    try:
        assert guard._classify_pytest([_A], ["coordinator_core"], str(repo), "pytest") is None
    finally:
        guard._STRICT_DIRS.reset(token)


@pytest.mark.parametrize("command", [
    "npx vitest run {d}",
    "node --test {d}",
])
def test_em_js_runner_directory_stays_denied_even_when_small(repo, command):
    _populate(repo, _A, 1)
    _track(repo)
    assert _denied(repo, command.format(d=_A))


def _count_spawns(monkeypatch):
    spawned = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, *a, **kw):
            spawned.append(a[0] if a else kw.get("args"))
            super().__init__(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return spawned


def test_directory_invocation_spawns_nothing(repo, monkeypatch):
    _populate(repo, _A, _T + 1)
    _track(repo)
    spawned = _count_spawns(monkeypatch)
    assert _denied(repo, f"pytest {_A}")
    assert spawned == []


def test_allowed_directory_invocation_spawns_nothing(repo, monkeypatch):
    _populate(repo, _A, _T)
    _track(repo)
    spawned = _count_spawns(monkeypatch)
    assert not _denied(repo, f"pytest {_A}")
    assert spawned == []


def test_file_only_invocation_spawns_nothing_and_reads_no_index(repo, monkeypatch):
    _populate(repo, _A, 1)
    _track(repo)
    spawned = _count_spawns(monkeypatch)

    def no_index(_repo):
        raise AssertionError("index read for a file-only invocation")

    monkeypatch.setattr(git_state, "read_index_paths", no_index)
    assert not _denied(repo, f"pytest {_A}/test_0.py")
    assert spawned == []
