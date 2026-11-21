"""pre_existing_dirt drops gitignored untracked scratch; tracked or non-ignored dirt still counts."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import write_baseline as wb


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("*.scratch\nscratch/\n")
    (tmp_path / "tracked.scratch").write_text("v1")
    (tmp_path / "base.txt").write_text("v1")
    _git(tmp_path, "add", ".gitignore", "base.txt")
    _git(tmp_path, "add", "-f", "tracked.scratch")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_ignored_untracked_path_is_not_dirt(tmp_path):
    root = _repo(tmp_path)
    (root / "scratch").mkdir()
    (root / "scratch" / "n.md").write_text("x")
    (root / "loose.scratch").write_text("x")
    paths = ["scratch/n.md", "loose.scratch"]
    base = wb.capture(root, paths)
    assert wb.unwritten(root, base, paths) == paths
    assert wb.pre_existing_dirt(root, base, paths) == []


def test_tracked_ignored_path_still_dirt(tmp_path):
    root = _repo(tmp_path)
    (root / "tracked.scratch").write_text("v2")
    base = wb.capture(root, ["tracked.scratch"])
    assert wb.pre_existing_dirt(root, base, ["tracked.scratch"]) == ["tracked.scratch"]


def test_untracked_non_ignored_path_still_dirt(tmp_path):
    root = _repo(tmp_path)
    (root / "doc.md").write_text("x")
    (root / "scratch").mkdir()
    (root / "scratch" / "n.md").write_text("x")
    paths = ["doc.md", "scratch/n.md"]
    base = wb.capture(root, paths)
    assert wb.pre_existing_dirt(root, base, paths) == ["doc.md"]
