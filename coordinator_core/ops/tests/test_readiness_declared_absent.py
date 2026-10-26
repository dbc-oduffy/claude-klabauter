"""Tests for the readiness declared-absent helper; repos live under tmp_path."""

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import readiness_declared_absent as m

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "old.py").write_text("x\n")
    (root / "src" / "keep.py").write_text("y\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


ROWS = [{"id": "A", "writes": ["src/new.py", "src/keep.py"]}, {"id": "B", "writes": ["src\\other.py"]}]


def test_writes_source_is_only_paths_absent_at_base(repo):
    root, sha = repo
    d = m.declared_absent_paths(root, sha, ROWS, {})
    assert d.absent == frozenset({"src/new.py", "src/other.py"})
    assert d.present_at_head == frozenset()


def test_absent_at_head_source_and_present_split(repo):
    root, sha = repo
    d = m.declared_absent_paths(root, sha, [], {"absent_at_head": ["src/gone.py", "./src/old.py"]})
    assert d.absent == frozenset({"src/gone.py"})
    assert d.present_at_head == frozenset({"src/old.py"})


def test_malformed_absent_at_head_is_ignored(repo):
    root, sha = repo
    bad = [None, 3, "", "  ", "/abs.py", "C" + ":/x.py", "../up.py", "src/*.py", "dir/", {"a": 1}]
    assert m.declared_absent_paths(root, sha, [], {"absent_at_head": bad}) == m.DeclaredAbsent()
    assert m.declared_absent_paths(root, sha, [], {"absent_at_head": "src/x.py"}) == m.DeclaredAbsent()
    assert m.declared_absent_paths(root, sha, "nope", None) == m.DeclaredAbsent()


def test_no_base_sha_or_bad_sha_declares_nothing(repo):
    root, _ = repo
    assert m.declared_absent_paths(root, None, ROWS, {}) == m.DeclaredAbsent()
    assert m.declared_absent_paths(root, "deadbeef" * 5, ROWS, {}) == m.DeclaredAbsent()


def test_declared_absent_is_one_counted_git_read(repo, monkeypatch):
    from coordinator_core.git import run as git_run

    root, sha = repo
    real = git_run.run_git
    calls: list = []

    def _counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(git_run, "run_git", _counting)
    got = m.declared_absent_paths(root, sha, ROWS, {"absent_at_head": ["src/gone.py", "src/old.py"]})
    assert len(calls) == 1
    assert got.absent == frozenset({"src/new.py", "src/other.py", "src/gone.py"})
    assert got.present_at_head == frozenset({"src/old.py"})
    assert m.classify_unresolved("src/new.py:12", got) == m.EXPECTED_ABSENT
    assert m.classify_unresolved("src/unrelated.py", got) == m.BLOCKS


def test_classifier_both_sides(repo):
    root, sha = repo
    d = m.declared_absent_paths(root, sha, ROWS, {"absent_at_head": ["src/gone.py", "src/old.py"]})
    assert m.classify_unresolved("src/new.py:12", d) == m.EXPECTED_ABSENT
    assert m.classify_unresolved("see `src\\other.py`", d) == m.EXPECTED_ABSENT
    assert m.classify_unresolved("src/gone.py", d) == m.EXPECTED_ABSENT
    assert m.classify_unresolved("src/old.py", d) == m.DECLARED_ABSENT_BUT_PRESENT
    assert m.classify_unresolved("src/unrelated.py", d) == m.BLOCKS
    assert m.classify_unresolved("src/new.py.bak", d) == m.BLOCKS
    assert m.classify_unresolved("xsrc/new.py", d) == m.BLOCKS
    assert m.classify_unresolved(None, d) == m.BLOCKS


def test_empty_declared_set_blocks_everything():
    assert m.classify_unresolved("src/new.py", m.DeclaredAbsent()) == m.BLOCKS
