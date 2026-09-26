from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import tree_spine
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


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
