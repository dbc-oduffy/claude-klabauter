"""`emit_script` on a fixture plan spawns no git when the table is built and the matcher covers the ignore shape."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.dispatch_emit import emit, test_import_table
from coordinator_core.ops.dispatch_emit.emit import emit_script

from .conftest import REVIEW_KW

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = (
    "---\nplan_id: pln-x\nstatus: executing\n---\n\n# P\n\n## Tasks\n\n"
    "```yaml plan-tasks\n"
    "- id: A\n  title: A row\n  writes: [pkg/foo.py, out/gen.txt]\n  disposition: open\n"
    "  body: |\n    Do A.\n```\n"
)


def _git(repo: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, (args, done.stderr)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg" / "tests").mkdir(parents=True)
    (root / "pkg" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (root / "pkg" / "tests" / "test_foo.py").write_text("from pkg.foo import x\n", encoding="utf-8")
    (root / ".gitignore").write_text("out/\n", encoding="utf-8")
    (root / "p.md").write_text(_PLAN, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    test_import_table.rebuild(root)
    return root


def _forbid_git(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    def boom(argv, *a, **kw):
        calls.append(argv)
        raise AssertionError(f"git spawned: {argv}")

    monkeypatch.setattr(git_run, "run_git", boom)
    return calls


def test_emit_spawns_no_git_and_still_widens_and_filters(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _forbid_git(monkeypatch)
    script = emit_script(repo / "p.md", repo_root=repo, **REVIEW_KW)
    assert calls == []
    assert "pkg/tests/test_foo.py" in script
    assert "GITIGNORE FILTER DID NOT RUN" not in script


def test_a_matcher_gap_still_reaches_the_spawn(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emit, "_gitignored_paths_in_process", lambda paths, root: None)
    seen: list = []

    class _Result:
        timed_out = False
        returncode = 0
        stdout_bytes = b"out/gen.txt\0"

    monkeypatch.setattr(git_run, "run_git", lambda argv, **kw: seen.append(argv) or _Result())
    result = emit._gitignored_paths(["pkg/foo.py", "out/gen.txt"], repo_root=repo)
    assert seen == [["check-ignore", "-z", "--stdin"]]
    assert result.matched == frozenset({"out/gen.txt"})


def test_a_tracked_path_matching_an_ignore_rule_is_not_ignored(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "out").mkdir()
    (repo / "out" / "kept.txt").write_text("k\n", encoding="utf-8")
    _git(repo, "add", "-f", "out/kept.txt")
    _forbid_git(monkeypatch)
    result = emit._gitignored_paths(["out/kept.txt", "out/gen.txt"], repo_root=repo)
    assert result.matched == frozenset({"out/gen.txt"})
    assert not result.degraded


def test_a_longer_tracked_path_ending_in_the_candidate_does_not_make_it_tracked(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "x" / "out").mkdir(parents=True)
    (repo / "x" / "out" / "kept.txt").write_text("k\n", encoding="utf-8")
    _git(repo, "add", "-f", "x/out/kept.txt")
    _forbid_git(monkeypatch)
    assert emit._gitignored_paths(["out/kept.txt"], repo_root=repo).matched == frozenset({"out/kept.txt"})


def test_matcher_is_rebuilt_when_the_root_gitignore_moves(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_git(monkeypatch)
    assert emit._gitignored_paths(["tmp/a.txt"], repo_root=repo).matched == frozenset()
    (repo / ".gitignore").write_text("out/\ntmp/\n", encoding="utf-8")
    assert emit._gitignored_paths(["tmp/a.txt"], repo_root=repo).matched == frozenset({"tmp/a.txt"})
