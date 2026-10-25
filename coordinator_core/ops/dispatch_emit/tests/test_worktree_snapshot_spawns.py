"""worktree_snapshot reads against a real tmp_path git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.worktree_snapshot import tracked_at_head, worktree_snapshot
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("junk/\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("1\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


def test_snapshot_reads_modified_untracked_deleted_and_ignored(repo):
    (repo / "kept.py").write_text("2\n", encoding="utf-8")
    (repo / "new.py").write_text("n\n", encoding="utf-8")
    (repo / "gone.py").unlink()
    (repo / "junk").mkdir()
    (repo / "junk" / "x.py").write_text("x\n", encoding="utf-8")
    snap = worktree_snapshot(repo, ["kept.py", "new.py", "gone.py", "clean.py"], [], [])
    assert snap is not None
    assert snap.changed == {"kept.py", "new.py", "gone.py"}
    assert snap.covers("clean.py") and "clean.py" not in snap.changed


def test_snapshot_over_a_root_reports_ignored_directories(repo):
    (repo / "junk").mkdir()
    (repo / "junk" / "x.py").write_text("x\n", encoding="utf-8")
    snap = worktree_snapshot(repo, [], ["junk"], [])
    assert snap is not None and snap.ignored_among(["junk/x.py"]) == ["junk/x.py"]


def test_tracked_at_head_keeps_only_paths_head_tracks(repo):
    assert tracked_at_head(repo, ["gone.py", "never.py"]) == ["gone.py"]
