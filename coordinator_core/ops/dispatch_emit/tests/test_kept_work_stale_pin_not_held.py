"""kept_work.held_paths: a pin matching neither the working bytes nor the HEAD blob is stale and not held."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import kept_work
from coordinator_core.ops.dispatch_emit.tests._kept_fixture import git, hold_kept

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = "wf/plan.mjs"


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "wf").mkdir()
    (root / REL).write_bytes(b"pinned\n")
    git(root, "add", REL)
    git(root, "commit", "-q", "-m", "x")
    return root


def _commit(root, data: bytes) -> None:
    (root / REL).write_bytes(data)
    git(root, "add", REL)
    git(root, "commit", "-q", "-m", "y")


def test_pin_matching_neither_head_nor_working_is_not_held(tmp_path):
    root = _repo(tmp_path)
    hold_kept(root, "run-a", [REL])
    _commit(root, b"moved on\n")
    assert kept_work.held_paths(root, [REL]) == {}


def test_pin_matching_working_bytes_still_holds(tmp_path):
    root = _repo(tmp_path)
    hold_kept(root, "run-a", [REL])
    _commit(root, b"moved on\n")
    (root / REL).write_bytes(b"pinned\n")
    assert kept_work.held_paths(root, [REL]) == {REL: "run-a"}


def test_pin_matching_head_blob_still_holds(tmp_path):
    root = _repo(tmp_path)
    hold_kept(root, "run-a", [REL])
    (root / REL).write_bytes(b"worktree drift\n")
    assert kept_work.held_paths(root, [REL]) == {REL: "run-a"}


def test_stale_pin_of_one_run_does_not_hide_a_live_peer(tmp_path):
    root = _repo(tmp_path)
    hold_kept(root, "run-a", [REL])
    _commit(root, b"moved on\n")
    hold_kept(root, "run-b", [REL])
    assert kept_work.held_paths(root, [REL]) == {REL: "run-b"}
