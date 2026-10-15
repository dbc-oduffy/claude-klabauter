"""Tests for the shared verified tree-kill and stale-child sweep."""

from __future__ import annotations

from coordinator_core.bash_guards._taskstop_contract import ProcRow
from coordinator_core.bash_guards._taskstop_kill import kill_tree_verified


class _Kill:
    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.calls = []

    def terminate_verified(self, pid, ctime):
        self.calls.append((pid, ctime))
        return (pid, ctime) not in self.refuse

    def command_line(self, pid, ctime):
        return None


def _tree():
    return [ProcRow(10, 1, 100, "a"), ProcRow(11, 10, 110, "b")]


def test_complete_tree_kill_reports_every_pair():
    kill = _Kill()
    killed, complete, _ = kill_tree_verified(_tree(), lambda: [ProcRow(1, 0, 1, "init")], kill)
    assert killed == [(10, 100), (11, 110)] and complete is True


def test_refused_member_is_incomplete_and_unlisted():
    kill = _Kill(refuse=[(11, 110)])
    killed, complete, _ = kill_tree_verified(_tree(), lambda: [ProcRow(1, 0, 1, "init")], kill)
    assert killed == [(10, 100)] and complete is False


def test_stale_child_of_killed_parent_is_swept():
    kill = _Kill()
    late = ProcRow(12, 10, 120, "late")
    killed, complete, _ = kill_tree_verified(_tree(), lambda: [*_tree(), late], kill)
    assert (12, 120) in killed and complete is True
    assert kill.calls.count((10, 100)) == 1


def test_stale_sweep_refusal_marks_incomplete():
    kill = _Kill(refuse=[(12, 120)])
    late = ProcRow(12, 10, 120, "late")
    killed, complete, _ = kill_tree_verified(_tree(), lambda: [late], kill)
    assert (12, 120) not in killed and complete is False


def test_pid_reuse_younger_than_parent_is_not_swept():
    kill = _Kill()
    reused = ProcRow(12, 10, 50, "reused")
    killed, complete, _ = kill_tree_verified(_tree(), lambda: [reused], kill)
    assert (12, 50) not in killed and (12, 50) not in kill.calls and complete is True


def test_child_of_unkilled_parent_is_not_swept():
    kill = _Kill(refuse=[(10, 100)])
    orphan = ProcRow(13, 10, 130, "x")
    killed, complete, _ = kill_tree_verified(_tree(), lambda: [orphan], kill)
    assert (13, 130) not in kill.calls and complete is False


def test_unreadable_resnapshot_is_not_verified():
    kill = _Kill()
    killed, complete, reason = kill_tree_verified(_tree(), lambda: None, kill)
    assert killed == [(10, 100), (11, 110)]
    assert complete is False and reason and "unreadable" in reason
