"""archive_and_commit_sync: commits a batch on a tmp repo with no event loop.

The fresh-interpreter leg proves the sync core never loads asyncio; the
in-process legs pin the async entry as a thin wrapper over the same core and
the sync retry's failure annotation.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import coordinator_core
from coordinator_core.ops.fleet import _common
from coordinator_core.ops.fleet._common import Move, archive_and_commit, archive_and_commit_sync
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    )


def _seed(tmp_path: Path, name: str) -> tuple[Path, Move]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)
    src = root / "docs" / "plans" / name
    src.parent.mkdir(parents=True)
    src.write_text("---\nstatus: implemented\n---\n\nbody\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    dst = root / "archive" / "specs" / name
    return root, Move(src=src, dst=dst, candidate_id=f"docs/plans/{name}")


def test_sync_core_commits_without_loading_asyncio(tmp_path):
    root, move = _seed(tmp_path, "2026-01-01-p.md")
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from coordinator_core.ops.fleet._common import Move, archive_and_commit_sync

        root, src, dst, cid = (Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
        acted, failed = archive_and_commit_sync(root, [Move(src=src, dst=dst, candidate_id=cid)], "archive p")
        assert failed == [] and acted == [{"id": cid, "archived": True}], (acted, failed)
        loaded = sorted(m for m in ("asyncio", "concurrent.futures") if m in sys.modules)
        assert not loaded, loaded
        """
    )
    tree = Path(coordinator_core.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(tree), os.environ.get("PYTHONPATH")]))}
    proc = subprocess.run(
        [sys.executable, "-c", script, str(root), str(move.src), str(move.dst), move.candidate_id],
        capture_output=True, text=True, env=env, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert move.dst.exists() and not move.src.exists()
    assert _git(["log", "-1", "--format=%s"], root).stdout.strip() == "archive p"
    assert _git(["status", "--porcelain"], root).stdout.strip() == ""


def test_async_entry_delegates_to_the_sync_core(tmp_path, monkeypatch):
    root, move = _seed(tmp_path, "2026-01-02-p.md")
    seen = []

    def spy(worktree_root, moves, subject):
        seen.append((worktree_root, list(moves), subject))
        return [{"id": "x", "archived": True}], []

    monkeypatch.setattr(_common, "archive_and_commit_sync", spy)
    result = asyncio.run(archive_and_commit(root, [move], "subj"))
    assert result == ([{"id": "x", "archived": True}], [])
    assert seen == [(root, [move], "subj")]
    assert move.src.exists()


def test_async_entry_lands_the_commit(tmp_path):
    root, move = _seed(tmp_path, "2026-01-03-p.md")
    acted, failed = asyncio.run(archive_and_commit(root, [move], "via async"))
    assert failed == [] and len(acted) == 1
    assert _git(["log", "-1", "--format=%s"], root).stdout.strip() == "via async"


def test_exhausted_sync_resync_annotates_the_item_and_keeps_the_commit(tmp_path, monkeypatch):
    root, move = _seed(tmp_path, "2026-01-04-p.md")
    monkeypatch.setattr(_common, "_INDEX_RETRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(_common, "_INDEX_RETRY_INITIAL_SLEEP_S", 0.001)
    monkeypatch.setattr(_common, "_INDEX_RETRY_BACKOFF_CAP_S", 0.001)
    from coordinator_core.git import run as git_run

    real_run = git_run.run_git

    def flaky(args, **kw):
        if list(args)[:1] == ["restore"]:
            return git_run.GitResult(128, "", "fatal: index.lock exists", False)
        return real_run(args, **kw)

    monkeypatch.setattr(git_run, "run_git", flaky)
    monkeypatch.setattr(
        _common._index_resync_drain, "drain_pending_resyncs_sync", lambda *_a, **_k: {}
    )
    acted, failed = archive_and_commit_sync(root, [move], "archive with lock")
    assert failed == [] and len(acted) == 1
    assert "index_resync_failed" in acted[0]
    assert _git(["log", "-1", "--format=%s"], root).stdout.strip() == "archive with lock"


def test_sync_retry_counts_a_timed_out_spawn_as_a_failed_attempt(monkeypatch, tmp_path):
    from coordinator_core.git import run as git_run

    seen = []

    def run(args, **kw):
        seen.append(kw.get("timeout"))
        return git_run.GitResult(-1, "", "", True)

    monkeypatch.setattr(git_run, "run_git", run)
    monkeypatch.setattr(_common, "_INDEX_RETRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(_common, "_INDEX_RETRY_INITIAL_SLEEP_S", 0.001)
    err = _common._update_index_with_retry_sync(["git", "restore"], cwd=tmp_path, env={})
    assert "timed out" in err
    assert seen == [None] * 2
