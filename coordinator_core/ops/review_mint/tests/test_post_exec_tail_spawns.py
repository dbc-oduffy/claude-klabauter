"""Tests for the pure post_exec_tail interface."""

from __future__ import annotations


import pytest

from coordinator_core.ops.review_mint import post_exec_tail as pet
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root, *args) -> None:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


def test_group_prefix_refreezes_over_its_own_prior_attempt_but_a_generic_id_still_refuses(tmp_path: Path) -> None:
    from coordinator_core.ops import review_freeze_diff as rfd

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "a.py")
    _git(root, "commit", "-q", "-m", "seed")
    base = _head(root)

    gid = pet.group_slice_prefix("k", 0, base)
    generic = "k.g0-generic"
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert rfd._freeze_diff_worktree(root, base, gid, ["a.py"])["error"] is None
    assert rfd._freeze_diff_worktree(root, base, generic, ["a.py"])["error"] is None

    (root / "a.py").write_text("x = 3\n", encoding="utf-8")
    assert rfd._freeze_diff_worktree(root, base, gid, ["a.py"])["error"] is None
    assert list((root / "state" / "review-trail" / "diffs").glob(f"{gid}.attempt1.diff"))
    assert "already names a frozen diff" in rfd._freeze_diff_worktree(root, base, generic, ["a.py"])["error"]


def _head(root):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()
