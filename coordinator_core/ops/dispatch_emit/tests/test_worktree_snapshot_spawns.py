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


def _large_run_record_root(repo: Path, n: int) -> None:
    for i in range(n):
        d = repo / "state" / "warp" / f"run-{i:04d}"
        d.mkdir(parents=True)
        (d / "r.md").write_text("r\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "warp")


def test_snapshot_over_a_large_run_record_root_answers_in_process(repo, monkeypatch):
    import time

    from coordinator_core.ops.dispatch_emit import worktree_snapshot as ws

    _large_run_record_root(repo, 1100)
    (repo / "state" / "warp" / "run-0003" / "r.md").write_text("changed\n", encoding="utf-8")
    (repo / "state" / "warp" / "run-9999").mkdir()
    (repo / "state" / "warp" / "run-9999" / "new.md").write_text("n\n", encoding="utf-8")
    spawns: list = []
    monkeypatch.setattr(ws, "run_git", lambda *a, **k: spawns.append(a) or pytest.fail("git spawned"))
    start = time.process_time()
    snap = worktree_snapshot(repo, [], ["state/warp"], [])
    elapsed = time.process_time() - start
    assert snap is not None and not spawns
    assert snap.changed == {"state/warp/run-0003/r.md", "state/warp/run-9999/new.md"}
    assert elapsed < 0.5


def test_fallback_over_a_root_still_reports_ignored(repo, monkeypatch):
    from coordinator_core.git import scoped_worktree_status as sws
    from coordinator_core.ops.dispatch_emit import worktree_snapshot as ws

    monkeypatch.setattr(sws, "scoped_worktree_status", lambda *a, **k: None)
    seen: list = []
    real = ws.run_git
    monkeypatch.setattr(ws, "run_git", lambda argv, **k: seen.append(argv) or real(argv, **k))
    (repo / "junk").mkdir()
    (repo / "junk" / "x.py").write_text("x\n", encoding="utf-8")
    (repo / "new.py").write_text("n\n", encoding="utf-8")
    snap = worktree_snapshot(repo, ["new.py"], [], [])
    assert snap is not None and snap.changed == {"new.py"}
    assert "--ignored=matching" in seen[-1]
    snap = worktree_snapshot(repo, [], ["junk"], [])
    assert snap is not None and snap.changed == set()
    assert snap.ignored_among(["junk/x.py"]) == ["junk/x.py"]


def test_fallback_over_a_root_does_not_enumerate_ignored(repo, monkeypatch):
    from coordinator_core.git import scoped_worktree_status as sws
    from coordinator_core.ops.dispatch_emit import worktree_snapshot as ws

    monkeypatch.setattr(sws, "scoped_worktree_status", lambda *a, **k: None)
    seen: list = []
    real = ws.run_git
    monkeypatch.setattr(ws, "run_git", lambda argv, **k: seen.append(argv) or real(argv, **k))
    (repo / "sub" / "junk").mkdir(parents=True)
    (repo / "sub" / "junk" / "x.py").write_text("x\n", encoding="utf-8")
    (repo / "sub" / "n.py").write_text("n\n", encoding="utf-8")
    snap = worktree_snapshot(repo, [], ["sub"], [])
    assert snap is not None and snap.changed == {"sub/n.py"}
    assert not any("--ignored=matching" in a for a in seen)
    assert snap.ignored_among(["sub/junk/x.py", "sub/n.py", "kept.py"]) == ["sub/junk/x.py"]
    assert not any("--ignored=matching" in a for a in seen)
    assert any("check-ignore" in a for a in seen)
    assert snap.ignored == ["sub/junk/"]
