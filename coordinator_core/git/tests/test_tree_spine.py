"""Unit tests for `coordinator_core.git.tree_spine`, relocated (2026-08-26,
C1 of docs/plans/2026-08-26-the-archival-commit-helper-computes-its-own-tree.md)
out of `coordinator_core.ops.ceremony.git_native`. The sha-identity-against-
real-git oracle for `_rewrite_head_spine` travels WITH the code and stays in
`coordinator_core/ops/ceremony/tests/test_git_native.py` (its
`test_rewrite_head_spine_prunes_emptied_dirs_like_git`) -- these tests cover
the module's own basic shape (import surface, sentinel identity, simple
level-write/rewrite behaviour) without duplicating that oracle.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import tree_spine
from coordinator_core.win_portability import no_console_creationflags


def _git_out(cwd, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return result.stdout.strip()


def test_absent_is_a_distinct_sentinel_object():
    assert tree_spine._ABSENT is tree_spine._ABSENT
    assert tree_spine._ABSENT is not object()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_write_tree_level_matches_git_mktree(tmp_path):
    """Real `git mktree` is the assertion here (sha-identity), not a
    convenience -- the oracle this test checks against IS a git spawn.
    """
    subprocess.run(
        ["git", "init", "-q"],
        cwd=tmp_path,
        check=True,
        **no_console_creationflags(),
    )
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    blob_sha = _git_out(tmp_path, "hash-object", "-w", "--", "a.txt")

    gitdir = tmp_path / ".git"
    ours = tree_spine._write_tree_level(gitdir, {"a.txt": (0o100644, blob_sha)})

    mktree_input = f"100644 blob {blob_sha}\ta.txt\n".encode("utf-8")
    theirs = subprocess.run(
        ["git", "mktree"],
        cwd=tmp_path,
        input=mktree_input,
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    ).stdout.decode("utf-8").strip()

    assert ours == theirs


def test_rewrite_head_spine_returns_none_for_missing_parent_in_spine():
    spine = {"": {}}
    assembled = {"missing_dir/file.txt": (0o100644, "0" * 40)}
    assert tree_spine._rewrite_head_spine(None, spine, assembled) is None


def test_synthesize_absent_spine_dirs_fills_new_directory():
    spine = {"": {}}
    assembled = {"newdir/newfile.txt": (0o100644, "0" * 40)}
    result = tree_spine._synthesize_absent_spine_dirs(spine, assembled)
    assert result is spine
    assert "newdir" in spine
    assert spine["newdir"] == {}


def test_synthesize_absent_spine_dirs_refuses_on_absent_deletion_with_missing_parent():
    spine = {"": {}}
    assembled = {"missing_dir/file.txt": tree_spine._ABSENT}
    assert tree_spine._synthesize_absent_spine_dirs(spine, assembled) is None


def test_synthesize_absent_spine_dirs_refuses_when_name_occupied_by_non_directory():
    spine = {"": {"occupied": (0o100644, "1" * 40)}}
    assembled = {"occupied/newfile.txt": (0o100644, "0" * 40)}
    assert tree_spine._synthesize_absent_spine_dirs(spine, assembled) is None
