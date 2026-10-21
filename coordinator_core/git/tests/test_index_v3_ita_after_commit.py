"""A peer's intent-to-add entry makes `.git/index` v3; a landed commit must still leave the
committed paths' index entries at HEAD, and a reverted created path must leave no phantom entry."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import index_write
from coordinator_core.git.commit import commit_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for name in ("a.txt", "b.txt", "peer.txt"):
        (root / name).write_text(name + "\n", encoding="utf-8", newline="\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _version(repo: Path) -> int:
    return int.from_bytes((repo / ".git" / "index").read_bytes()[4:8], "big")


def test_commit_over_a_v3_index_leaves_committed_paths_at_head_and_peer_stage(repo):
    (repo / "ghost.md").write_text("g\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-N", "ghost.md")
    (repo / "peer.txt").write_text("peer staged\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "peer.txt")
    assert _version(repo) == 3
    (repo / "a.txt").write_text("edited\n", encoding="utf-8", newline="\n")
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")
    (repo / "b.txt").unlink()

    commit_paths(repo, ["a.txt", "new.txt"], "remove b.txt", deleted_paths=["b.txt"])

    assert _git(repo, "diff", "--cached", "--name-only", "--", "a.txt", "new.txt", "b.txt") == ""
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["peer.txt"]
    assert "ghost.md" in _git(repo, "ls-files", "--", "ghost.md")
    assert _version(repo) == 3
    _git(repo, "fsck", "--strict")


def test_last_extended_entry_committed_drops_back_to_v2(repo):
    (repo / "later.txt").write_text("l\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-N", "later.txt")
    assert _version(repo) == 3

    commit_paths(repo, ["later.txt"], "m")

    assert _version(repo) == 2
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_phantom_intent_to_add_is_dropped_and_real_entries_survive(repo):
    (repo / "ghost.md").write_text("g\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-N", "ghost.md")
    (repo / "peer.txt").write_text("peer staged\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "peer.txt")
    (repo / "ghost.md").unlink()

    index_write.splice_index(
        repo,
        {p: index_write.DROP_IF_PHANTOM_INTENT_TO_ADD for p in ("ghost.md", "peer.txt", "a.txt")},
    )

    assert _git(repo, "ls-files", "--", "ghost.md") == ""
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["peer.txt"]
    assert _version(repo) == 2


def test_intent_to_add_with_a_file_on_disk_is_kept(repo):
    (repo / "live.md").write_text("l\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-N", "live.md")

    index_write.splice_index(repo, {"live.md": index_write.DROP_IF_PHANTOM_INTENT_TO_ADD})

    assert _git(repo, "ls-files", "--", "live.md").strip() == "live.md"
