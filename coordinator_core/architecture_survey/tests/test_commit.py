"""Pins the atlas commit and scratch cleanup against a fixture git repo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.architecture_survey import commit as cm


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


class _Budget:
    def __init__(self) -> None:
        self.n = 0

    def record_invocation(self, unit: str = "invocation", *, count: int = 1) -> int:
        self.n += count
        return self.n


def _g(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _g(tmp_path, "init", "-q", "-b", "work/x")
    _g(tmp_path, "config", "user.email", "t@t")
    _g(tmp_path, "config", "user.name", "t")
    (tmp_path / "README").write_text("r")
    _g(tmp_path, "add", "README")
    _g(tmp_path, "commit", "-qm", "init")
    (tmp_path / "docs/architecture").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "state/health-ledger.md").write_text("l")
    (tmp_path / "docs/architecture/a.md").write_text("a")
    return tmp_path


def _call(repo: Path, budget: _Budget, **kw):
    args = dict(mode="refresh", systems_mapped=3, ledger_stamped=False, commits_behind=0)
    args.update(kw)
    return cm.commit_atlas(repo, budget=budget, **args)


def test_commits_only_pathspecs_and_counts_two_spawns(repo: Path) -> None:
    (repo / "other.txt").write_text("x")
    b = _Budget()
    d = _call(repo, b, ledger_stamped=True, commits_behind=4)
    assert d.chosen == "committed" and b.n == 2
    subject = _g(repo, "log", "-1", "--format=%s").strip()
    assert subject == "deep-architecture-survey: refresh — 3 systems mapped; Last full audit bumped; index 4 behind"
    assert d.evidence.startswith(_g(repo, "rev-parse", "HEAD").strip())
    assert "other.txt" not in _g(repo, "show", "--name-only", "--format=")


def test_targeted_leaves_ledger_out(repo: Path) -> None:
    d = _call(repo, _Budget(), mode="targeted")
    assert d.chosen == "committed"
    files = _g(repo, "show", "--name-only", "--format=")
    assert "docs/architecture/a.md" in files and "health-ledger" not in files


def test_no_change(repo: Path) -> None:
    _call(repo, _Budget())
    assert _call(repo, _Budget()).chosen == "no-change"


def test_refuses_default_branch(repo: Path) -> None:
    _g(repo, "checkout", "-q", "-b", "main")
    b = _Budget()
    assert _call(repo, b).chosen == "refused-default-branch"
    assert b.n == 0


def test_cleanup_removes_only_run_dir(repo: Path) -> None:
    root = repo / cm.SCRATCH_ROOT
    (root / "r1").mkdir(parents=True)
    (root / "r1/f").write_text("x")
    (root / "r2").mkdir()
    cm.cleanup_scratch(repo, "r1")
    assert not (root / "r1").exists() and (root / "r2").exists()


@pytest.mark.parametrize("bad", ["", "..", "../x", "a/b", "."])
def test_cleanup_refuses_non_children(repo: Path, bad: str) -> None:
    (repo / cm.SCRATCH_ROOT).mkdir(parents=True)
    with pytest.raises(ValueError):
        cm.cleanup_scratch(repo, bad)
