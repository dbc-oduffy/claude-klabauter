"""A plumbing landing (private GIT_INDEX_FILE, commit-tree, update-ref) must leave the SHARED index
at the landed blobs for the landed paths and at the peer's own staging for every other path, so a
plain `git commit` by a peer afterwards cannot revert the land."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.git.index_sync import IndexSyncRefused, sync_shared_index
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "sync-shared-index.py"


def _git(repo: Path, *args: str, env: dict | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=str(repo), capture_output=True, text=True, env=env, check=check,
        **no_console_creationflags(),
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    for name, text in (("a.txt", "a0\n"), ("b.txt", "b0\n"), ("gone.txt", "g0\n"), ("peer.txt", "p0\n")):
        (tmp_path / name).write_text(text)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _plumbing_land(repo: Path, edits: dict[str, str | None]) -> str:
    """Land `edits` ({path: text | None = delete}) through a temp index; the shared index is untouched."""
    old = _git(repo, "rev-parse", "HEAD")
    env = dict(os.environ, GIT_INDEX_FILE=str(repo / ".git" / "tmp-index"))
    _git(repo, "read-tree", old, env=env)
    for rel, text in edits.items():
        if text is None:
            _git(repo, "rm", "-q", "--cached", "--", rel, env=env)
        else:
            blob = subprocess.run(
                ["git", "hash-object", "-w", "--stdin"], cwd=str(repo), input=text,
                capture_output=True, text=True, check=True, **no_console_creationflags(),
            ).stdout.strip()
            _git(repo, "update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env=env)
    tree = _git(repo, "write-tree", env=env)
    new = _git(repo, "commit-tree", tree, "-p", old, "-m", "plumbing land")
    _git(repo, "update-ref", "HEAD", new, old)
    return new


def _stage_peer(repo: Path) -> None:
    (repo / "peer.txt").write_text("p1 staged by peer\n")
    _git(repo, "add", "peer.txt")


def _assert_peer_commit_preserves(repo: Path, new: str, landed: dict[str, str | None]) -> None:
    assert _git(repo, "diff", "--cached", "--name-only") == "peer.txt"
    _git(repo, "commit", "-q", "-m", "peer commit")
    for rel, text in landed.items():
        if text is None:
            assert _git(repo, "ls-tree", "HEAD", "--", rel) == ""
        else:
            assert _git(repo, "show", f"HEAD:{rel}") == text.strip()
        assert _git(repo, "diff", new, "HEAD", "--name-only", "--", rel) == ""
    assert _git(repo, "show", "HEAD:peer.txt") == "p1 staged by peer"


@pytest.mark.parametrize(
    "landed",
    [
        {"a.txt": "a1\n"},
        {"new.txt": "n1\n"},
        {"gone.txt": None},
        {"a.txt": "a2\n", "b.txt": "b2\n", "new.txt": "n2\n", "gone.txt": None},
    ],
    ids=["modify", "add", "delete", "mixed"],
)
def test_helper_preserves_land_and_peer_staging(repo: Path, landed: dict) -> None:
    _stage_peer(repo)
    new = _plumbing_land(repo, landed)
    assert _git(repo, "diff", "--cached", "--name-only")  # the hazard: stale entries are visible
    sync_shared_index(repo, new, list(landed))
    _assert_peer_commit_preserves(repo, new, landed)


def test_unlisted_staged_path_is_never_touched(repo: Path) -> None:
    _stage_peer(repo)
    before = _git(repo, "ls-files", "-s", "--", "peer.txt")
    new = _plumbing_land(repo, {"a.txt": "a1\n"})
    sync_shared_index(repo, new, ["a.txt"])
    assert _git(repo, "ls-files", "-s", "--", "peer.txt") == before
    assert _git(repo, "diff", "--cached", "--name-only", "--", "a.txt") == ""


def test_held_lock_refuses_typed_and_writes_nothing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    new = _plumbing_land(repo, {"a.txt": "a1\n"})
    index = repo / ".git" / "index"
    before = index.read_bytes()
    (repo / ".git" / "index.lock").write_text("peer")
    monkeypatch.setattr("coordinator_core.git.index_sync.DEFAULT_BACKOFF_SCHEDULE_S", (0.0,))
    monkeypatch.setattr("coordinator_core.git.index_write._recover_dead_writer", lambda p: False)
    monkeypatch.setattr("coordinator_core.git.index_write._reclaim_ownerless_lock", lambda p: False)
    with pytest.raises(IndexSyncRefused) as exc:
        sync_shared_index(repo, new, ["a.txt"])
    assert exc.value.reason == "lock-busy"
    assert index.read_bytes() == before
    assert (repo / ".git" / "index.lock").read_text() == "peer"


def test_unreadable_commit_refuses(repo: Path) -> None:
    with pytest.raises(IndexSyncRefused) as exc:
        sync_shared_index(repo, "0" * 40, ["a.txt"])
    assert exc.value.reason == "unreadable-commit"


def test_cli_entry_point_for_hand_lands(repo: Path) -> None:
    _stage_peer(repo)
    new = _plumbing_land(repo, {"a.txt": "a1\n", "gone.txt": None})
    proc = subprocess.run(
        [sys.executable, str(_BIN), "--repo", str(repo), "--commit", new, "--paths-from-commit"],
        capture_output=True, text=True, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["synced"] == ["a.txt", "gone.txt"]
    _assert_peer_commit_preserves(repo, new, {"a.txt": "a1\n", "gone.txt": None})


def test_site_propagate_delivery_leaves_peer_staging(repo: Path) -> None:
    from coordinator_core.ops import propagate_body

    rel = "state/handoffs/h.md"
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / rel).write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "handoff")
    _stage_peer(repo)
    (repo / rel).write_text("one\ntwo\n")
    sha, err = propagate_body._commit_delivery(repo, rel, "slug", "summary")
    assert err is None and sha
    assert _git(repo, "diff", "--cached", "--name-only") == "peer.txt"
    _git(repo, "commit", "-q", "-m", "peer commit")
    assert _git(repo, "show", f"HEAD:{rel}") == "one\ntwo"


def test_site_commit_authored_content_leaves_peer_staging(repo: Path) -> None:
    from coordinator_core.ops.ceremony import git_native

    _stage_peer(repo)
    msg = repo / ".git" / "msg.txt"
    msg.write_text("authored\n")
    res = git_native.commit_authored_content("a.txt", "a-authored\n", msg, repo)
    assert res.returncode == 0, res.stderr
    assert _git(repo, "diff", "--cached", "--name-only") == "peer.txt"
    _git(repo, "commit", "-q", "-m", "peer commit")
    assert _git(repo, "show", "HEAD:a.txt") == "a-authored"


def test_site_commit_scoped_leaves_peer_staging(repo: Path) -> None:
    from coordinator_core.ops.ceremony import git_native

    _stage_peer(repo)
    (repo / "b.txt").write_text("b-scoped\n")
    (repo / "gone.txt").unlink()
    msg = repo / ".git" / "msg.txt"
    msg.write_text("scoped\n")
    res = git_native.commit_scoped(["b.txt", "gone.txt"], msg, repo)
    assert res.returncode == 0, res.stderr
    assert _git(repo, "diff", "--cached", "--name-only") == "peer.txt"
    _git(repo, "commit", "-q", "-m", "peer commit")
    assert _git(repo, "show", "HEAD:b.txt") == "b-scoped"
    assert _git(repo, "ls-tree", "HEAD", "--", "gone.txt") == ""
