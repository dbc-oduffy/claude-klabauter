"""Tests for the app-session shared-tree build guard."""

import json

from coordinator_core.ops._app_session_build_guard import (
    acquire_tree_lock,
    check_launch_conflicts,
    release_tree_lock,
    transfer_tree_lock,
)

ALIVE = lambda pid, ep: True  # noqa: E731
DEAD = lambda pid, ep: False  # noqa: E731


def test_of_two_acquirers_one_wins(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    a = acquire_tree_lock(tmp_path / "h", wt, 1, 10.0, ALIVE)
    b = acquire_tree_lock(tmp_path / "h", wt, 2, 11.0, ALIVE)
    assert a.acquired and not b.acquired
    assert b.holder["pid"] == 1


def test_stale_reclaim(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    a = acquire_tree_lock(tmp_path / "h", wt, 1, 10.0, ALIVE)
    b = acquire_tree_lock(tmp_path / "h", wt, 2, 11.0, DEAD)
    assert b.acquired and b.path == a.path
    assert json.loads(b.path.read_text())["pid"] == 2
    assert [p.name for p in b.path.parent.iterdir()] == [b.path.name]


def test_transfer_then_release_by_new_holder(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    a = acquire_tree_lock(tmp_path / "h", wt, 1, 10.0, ALIVE)
    assert transfer_tree_lock(a.path, 5, 50.0)
    assert not release_tree_lock(a.path, 1)
    assert a.path.exists()
    assert release_tree_lock(a.path, 5)
    assert not a.path.exists()


def test_release_by_non_holder_is_noop(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    a = acquire_tree_lock(tmp_path / "h", wt, 1, 10.0, ALIVE)
    assert not release_tree_lock(a.path, 99)
    assert a.path.exists()


def test_conflict_on_same_cwd(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    handles = {
        "app": {"cwd": str(wt), "pid": 3, "start_epoch": 1.0},
        "app@validate": {"cwd": str(wt), "pid": 4, "start_epoch": 1.0},
    }
    got = check_launch_conflicts(handles, wt, False, ALIVE)
    assert [h["key"] for h in got] == ["app"]
    assert check_launch_conflicts(handles, wt, False, DEAD) == []


def test_no_conflict_when_build_isolated(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    handles = {"app": {"cwd": str(wt), "pid": 3, "start_epoch": 1.0}}
    assert check_launch_conflicts(handles, wt, True, ALIVE) == []


def test_no_conflict_on_sibling_worktree(tmp_path):
    wt, sib = tmp_path / "wt", tmp_path / "sib"
    wt.mkdir()
    sib.mkdir()
    handles = {"app": {"cwd": str(sib), "pid": 3, "start_epoch": 1.0}}
    assert check_launch_conflicts(handles, wt, False, ALIVE) == []
