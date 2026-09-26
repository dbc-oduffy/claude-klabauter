
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core.git_ancestry import is_covered, is_ancestor, _is_ancestor
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
        **no_console_creationflags(),
    )


def _make_commit(repo: Path, message: str) -> str:
    _git(["commit", "--allow-empty", "-m", message], repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        check=True,
        **no_console_creationflags(),
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


@pytest.fixture
def chain(tmp_path: Path) -> dict:
    _init_repo(tmp_path)
    shas = {}
    shas["before_a"] = _make_commit(tmp_path, "before window start")
    shas["a"] = _make_commit(tmp_path, "window start (exclusive)")
    shas["mid1"] = _make_commit(tmp_path, "delivery commit 1")
    shas["mid2"] = _make_commit(tmp_path, "delivery commit 2")
    shas["b"] = _make_commit(tmp_path, "window end (inclusive)")
    shas["repo"] = tmp_path
    return shas


def test_parity_worked_example_shape(chain: dict) -> None:
    repo = chain["repo"]
    for key in ("mid1", "mid2", "b"):
        assert is_covered(chain[key], chain["a"], chain["b"], cwd=str(repo)) is True


def test_negative_clause_excludes_pre_window_commit(chain: dict) -> None:
    repo = chain["repo"]
    assert is_covered(chain["before_a"], chain["a"], chain["b"], cwd=str(repo)) is False


def test_boundary_exactly_at_start_is_excluded(chain: dict) -> None:
    repo = chain["repo"]
    assert is_covered(chain["a"], chain["a"], chain["b"], cwd=str(repo)) is False


def test_boundary_exactly_at_end_is_included(chain: dict) -> None:
    repo = chain["repo"]
    assert is_covered(chain["b"], chain["a"], chain["b"], cwd=str(repo)) is True


def test_mutation_kill_inverted_start_clause(chain: dict) -> None:
    repo = chain["repo"]

    def mutant(commit: str, start: str, end: str) -> bool:
        return _is_ancestor(commit, end, cwd=str(repo)) and _is_ancestor(commit, start, cwd=str(repo))

    for key in ("before_a", "a", "mid1", "mid2", "b"):
        correct = is_covered(chain[key], chain["a"], chain["b"], cwd=str(repo))
        mutated = mutant(chain[key], chain["a"], chain["b"])
        assert correct != mutated, f"mutant did not diverge for {key!r}"


def test_legacy_wrapper_git_not_on_path_fails_closed_not_raises(monkeypatch: pytest.MonkeyPatch) -> None:

    def _raise_missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _raise_missing_git)
    assert _is_ancestor("abc123", "def456") is False
    read_ok, observed = is_ancestor("abc123", "def456")
    assert read_ok is False
    assert observed is None
