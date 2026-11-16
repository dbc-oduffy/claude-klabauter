"""A landing leaves the shared index equal to the new HEAD for exactly the landed paths and
leaves a peer's deliberate staging for any other path alone. Throwaway repos under `tmp_path`."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit as commit_mod
from coordinator_core.git import run as run_mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MSG = "m\n\nSession-Id: 11111111-1111-4111-8111-111111111111"


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).write_bytes(text.encode("utf-8"))


def _cached(repo: Path) -> list[str]:
    return _git(repo, "diff", "--cached", "--name-only", "HEAD").split()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    for rel in ("a.py", "gone.py", "peer.py"):
        _write(root, rel, f"{rel} one\n")
    _git(root, "add", "--", "a.py", "gone.py", "peer.py")
    _git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture
def spawns(monkeypatch):
    seen: list[list[str]] = []
    real = run_mod.run_git

    def counting(args, *a, **kw):
        seen.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(run_mod, "run_git", counting)
    return seen


def _stage_peer_change(repo: Path) -> None:
    _write(repo, "peer.py", "peer.py staged by a peer\n")
    _git(repo, "add", "--", "peer.py")


def test_landed_edit_and_new_path_are_clean_and_peer_staging_survives(repo, spawns):
    _stage_peer_change(repo)
    _write(repo, "a.py", "a.py two\n")
    _write(repo, "new.py", "new\n")

    commit_mod.commit_paths(repo, ["a.py", "new.py"], _MSG, untracked_paths=())

    assert _cached(repo) == ["peer.py"]
    assert _git(repo, "show", ":peer.py") == "peer.py staged by a peer\n"
    assert _git(repo, "status", "--short", "--", "a.py", "new.py") == ""
    assert len(spawns) <= 1


def test_landed_deletion_is_clean_and_peer_staging_survives(repo):
    _stage_peer_change(repo)
    (repo / "gone.py").unlink()

    commit_mod.commit_paths(repo, [], "remove gone.py" + _MSG[1:], deleted_paths=["gone.py"])

    assert _cached(repo) == ["peer.py"]
    assert _git(repo, "ls-files", "--", "gone.py") == ""


def test_a_stale_shared_entry_for_a_landed_path_is_synced_to_head(repo):
    _write(repo, "a.py", "a.py two\n")
    _git(repo, "add", "--", "a.py")
    _write(repo, "a.py", "a.py three\n")

    commit_mod.commit_paths(repo, ["a.py"], _MSG)

    assert "a.py" not in _cached(repo)
    assert _git(repo, "show", "HEAD:a.py") == "a.py three\n"


def test_a_failed_splice_publish_falls_back_to_one_batched_reset_of_the_landed_paths(
    repo, monkeypatch, spawns
):
    from coordinator_core.git import index_write

    _stage_peer_change(repo)
    _write(repo, "a.py", "a.py two\n")
    real_prepare = commit_mod._prepare_index_splice

    def failing_publish(*args, **kwargs):
        prepared = real_prepare(*args, **kwargs)
        prepared.abort()

        class _Dead:
            def held(self):
                return True

            def abort(self):
                return None

            def publish(self):
                raise index_write.IndexWriteError("simulated rename failure")

        return _Dead()

    monkeypatch.setattr(commit_mod, "_prepare_index_splice", failing_publish)

    commit_mod.commit_paths(repo, ["a.py"], _MSG)

    assert _cached(repo) == ["peer.py"]
    resets = [a for a in spawns if "reset" in a]
    assert len(resets) == 1 and "--pathspec-from-file=-" in resets[0]


def test_the_reset_matches_landed_paths_literally_never_as_globs(repo):
    for rel in ("x1.py", "x[1].py"):
        _write(repo, rel, "v1\n")
    _git(repo, "--literal-pathspecs", "add", "--", "x1.py", "x[1].py")
    _git(repo, "commit", "-q", "-m", "more")
    for rel in ("x1.py", "x[1].py"):
        _write(repo, rel, "v2\n")
    _git(repo, "--literal-pathspecs", "add", "--", "x1.py", "x[1].py")

    assert commit_mod._reset_index_paths(repo, ["x[1].py"]) is True

    assert _cached(repo) == ["x1.py"]


def test_private_index_hand_land_leaves_the_shared_index_behind_head(repo):
    """The hand-land recipe moves HEAD without the shared index; the engine's reset repairs
    exactly the landed paths in one spawn."""
    _stage_peer_change(repo)
    _write(repo, "a.py", "a.py two\n")
    idx = repo / ".git" / "private.index"
    env = {**os.environ, "GIT_INDEX_FILE": str(idx)}
    _git(repo, "read-tree", "HEAD", env=env)
    blob = _git(repo, "hash-object", "-w", "a.py").strip()
    _git(repo, "update-index", "--cacheinfo", f"100644,{blob},a.py", env=env)
    _git(repo, "commit", "-q", "-m", "hand land", env=env)
    assert set(_cached(repo)) == {"a.py", "peer.py"}

    assert commit_mod._reset_index_paths(repo, ["a.py"]) is True

    assert _cached(repo) == ["peer.py"]
    assert _git(repo, "show", ":peer.py") == "peer.py staged by a peer\n"
