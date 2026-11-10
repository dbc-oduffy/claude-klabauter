"""stage_dirt_snapshot.run_edits lists tracked and untracked-new paths edited at or after the stage marker."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as snap
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    )


def _age(path, seconds=3600):
    t = time.time() - seconds
    os.utime(path, (t, t))


@pytest.fixture
def staged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    for rel in ("pkg/__init__.py", "pkg/b.py", "pkg/c.py", "old.py", "deep/inner/z.py", "scratch/keep.txt"):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("v = 1\n", encoding="utf-8")
    (repo / "bare").mkdir()
    (repo / "bare" / "sub").mkdir()
    (repo / "bare" / "sub" / "t.py").write_text("t = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "old.py").write_text("peer = 1\n", encoding="utf-8")
    for p in repo.rglob("*"):
        if ".git" not in p.relative_to(repo).parts:
            _age(p)
    record = tmp_path / "record"
    assert snap.write_snapshot(repo, record) is not None
    marker = snap.read_snapshot(record)
    for p in (repo / "pkg", repo / "deep", repo / "deep" / "inner", repo / "bare", repo / "bare" / "sub", repo):
        os.utime(p, ns=(marker - 10**9, marker - 10**9))
    return repo, record


def _edit(path, text="edited = 1\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_run_edits_covers_tracked_untracked_and_new_dirs(staged):
    repo, record = staged
    _edit(repo / "pkg" / "b.py")
    _edit(repo / "pkg" / "c.py")
    _edit(repo / "pkg" / "new_mod.py")
    _edit(repo / "newpkg" / "sub" / "x.py")
    _edit(repo / "deep" / "inner" / "fresh.py")
    _edit(repo / "bare" / "newdir" / "y.py")
    got = snap.run_edits(repo, record, [])
    assert got == sorted(
        [
            "pkg/b.py",
            "pkg/c.py",
            "pkg/new_mod.py",
            "newpkg/sub/x.py",
            "deep/inner/fresh.py",
            "bare/newdir/y.py",
        ]
    )


def test_declared_paths_are_not_excluded(staged):
    repo, record = staged
    _edit(repo / "pkg" / "b.py")
    assert snap.run_edits(repo, record, ["pkg/b.py"]) == ["pkg/b.py"]
    assert snap.undeclared_edits(repo, record, ["pkg/b.py"]) == []


def test_omits_pre_marker_dirt_scratch_record_dir_and_pycache(staged):
    repo, record = staged
    _edit(repo / "scratch" / "new.txt")
    _edit(repo / "pkg" / "__pycache__" / "b.cpython-311.pyc")
    _edit(repo / ".pytest_cache" / "v" / "x")
    _edit(repo / "state" / "x.md")
    _edit(repo / "pkg" / "kept.py")
    assert snap.run_edits(repo, record, []) == ["pkg/kept.py"]


def test_record_dir_inside_the_repo_is_skipped(staged):
    repo, _ = staged
    inside = repo / "recdir" / "run"
    assert snap.write_snapshot(repo, inside) is not None
    _edit(repo / "pkg" / "kept.py")
    assert snap.run_edits(repo, inside, []) == ["pkg/kept.py"]


def test_untracked_older_than_marker_is_not_a_run_edit(staged):
    repo, record = staged
    old = repo / "pkg" / "preexisting.py"
    _edit(old)
    _age(old)
    assert snap.run_edits(repo, record, []) == []


def test_no_snapshot_or_no_index_reads_empty(staged, tmp_path):
    repo, record = staged
    _edit(repo / "pkg" / "b.py")
    assert snap.run_edits(repo, tmp_path / "missing", []) == []
    (repo / ".git" / "index").unlink()
    assert snap.run_edits(repo, record, []) == []


def test_run_edits_spawns_no_process(staged, monkeypatch):
    repo, record = staged
    _edit(repo / "pkg" / "new_mod.py")

    def boom(*a, **k):
        raise AssertionError("run_edits spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert snap.run_edits(repo, record, []) == ["pkg/new_mod.py"]
