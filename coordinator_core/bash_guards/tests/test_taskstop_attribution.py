"""Table tests for the fail-safe TaskStop attribution: window, overlap, corroboration."""

from __future__ import annotations

from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.bash_guards._taskstop_attribution import (
    POST_WINDOW_S,
    PRE_SLACK_S,
    attribute,
)
from coordinator_core.bash_guards._taskstop_contract import LaunchRecord, ReapOutcome

S = 10_000_000
MARK = 1_000 * S
LIVE_PARENT = ProcRow(pid=1, ppid=0, ctime=1 * S, name="claude.exe")


def rec(task="t1", mark=MARK, command="sleep 3001"):
    return LaunchRecord(task_id=task, session_id="s", tool_use_id="u", command=command, mark=mark)


def cl(table):
    return lambda pid, ctime: table.get(pid)


SLEEP = '"C:\\Program Files\\Git\\usr\\bin\\sleep.exe" 3001'  # abs-path-ok: probe-verbatim fixture


def test_unique_corroborated_tree_is_claimed_deepest_first():
    rows = [
        LIVE_PARENT,
        ProcRow(10, 999, MARK - S // 10, "bash.exe"),
        ProcRow(11, 10, MARK - S // 10 + 1, "sleep.exe"),
    ]
    tree, outcome, competing, n = attribute(rec(), [], rows, cl({10: "bash -c x", 11: SLEEP}))
    assert outcome is ReapOutcome.KILLED and n == 1 and competing == ()
    assert [p.pid for p in tree] == [11, 10]


def test_two_corroborated_trees_are_ambiguous():
    rows = [
        ProcRow(10, 999, MARK - S // 10, "sleep.exe"),
        ProcRow(20, 998, MARK - S // 5, "sleep.exe"),
    ]
    tree, outcome, _, n = attribute(rec(), [], rows, cl({10: SLEEP, 20: SLEEP}))
    assert tree is None and outcome is ReapOutcome.AMBIGUOUS and n == 2


def test_overlapping_record_window_is_ambiguous_even_with_one_candidate():
    rows = [ProcRow(10, 999, MARK - S // 10, "sleep.exe")]
    other = rec(task="t2", mark=MARK + S)
    tree, outcome, competing, n = attribute(rec(), [other], rows, cl({10: SLEEP}))
    assert tree is None and outcome is ReapOutcome.AMBIGUOUS and competing == ("t2",)
    assert n == 0


def test_record_outside_window_does_not_compete():
    far = rec(task="t2", mark=MARK + int((PRE_SLACK_S + POST_WINDOW_S) * S) + 1)
    rows = [ProcRow(10, 999, MARK - S // 10, "sleep.exe")]
    tree, outcome, _, _ = attribute(rec(), [far, rec()], rows, cl({10: SLEEP}))
    assert outcome is ReapOutcome.KILLED and [p.pid for p in tree] == [10]


def test_uncorroborated_peer_tree_alone_is_no_candidate():
    rows = [ProcRow(10, 999, MARK, "node.exe")]
    tree, outcome, _, n = attribute(rec(), [], rows, cl({10: '"node.exe" server.js'}))
    assert tree is None and outcome is ReapOutcome.NO_CANDIDATE and n == 0


def test_unreadable_cmdline_in_window_is_ambiguous():
    rows = [ProcRow(10, 999, MARK, "node.exe")]
    tree, outcome, _, n = attribute(rec(), [], rows, cl({}))
    assert tree is None and outcome is ReapOutcome.AMBIGUOUS and n == 1


def test_unreadable_peer_beside_corroborated_tree_is_ambiguous():
    rows = [ProcRow(10, 999, MARK, "sleep.exe"), ProcRow(20, 998, MARK, "node.exe")]
    tree, outcome, _, n = attribute(rec(), [], rows, cl({10: SLEEP}))
    assert tree is None and outcome is ReapOutcome.AMBIGUOUS and n == 2


def test_root_outside_window_is_ignored():
    rows = [ProcRow(10, 999, MARK + 5 * S, "sleep.exe")]
    _, outcome, _, _ = attribute(rec(), [], rows, cl({10: SLEEP}))
    assert outcome is ReapOutcome.NO_CANDIDATE


def test_window_edges_are_inclusive_and_one_tick_beyond_is_excluded():
    lo = MARK - int(PRE_SLACK_S * S)
    hi = MARK + int(POST_WINDOW_S * S)
    for ctime, expected in ((lo, ReapOutcome.KILLED), (hi, ReapOutcome.KILLED),
                            (lo - 1, ReapOutcome.NO_CANDIDATE), (hi + 1, ReapOutcome.NO_CANDIDATE)):
        _, outcome, _, _ = attribute(rec(), [], [ProcRow(10, 999, ctime, "sleep.exe")], cl({10: SLEEP}))
        assert outcome is expected


def test_live_parent_makes_row_not_an_orphan_root():
    rows = [LIVE_PARENT, ProcRow(5, 1, MARK - 1, "bash.exe"), ProcRow(10, 5, MARK, "sleep.exe")]
    _, outcome, _, _ = attribute(rec(), [], rows, cl({10: SLEEP}))
    assert outcome is ReapOutcome.NO_CANDIDATE


def test_reused_pid_child_is_not_descended():
    rows = [
        ProcRow(10, 999, MARK, "bash.exe"),
        ProcRow(11, 10, MARK - 100 * S, "sleep.exe"),
    ]
    tree, outcome, _, _ = attribute(rec(), [], rows, cl({10: "bash -c x", 11: SLEEP}))
    assert tree is None and outcome is ReapOutcome.NO_CANDIDATE


def test_nested_claude_is_not_descended():
    rows = [
        ProcRow(10, 999, MARK, "bash.exe"),
        ProcRow(11, 10, MARK + 1, "claude.exe"),
        ProcRow(12, 11, MARK + 2, "sleep.exe"),
    ]
    tree, outcome, _, _ = attribute(rec(), [], rows, cl({10: "bash", 11: "claude", 12: SLEEP}))
    assert tree is None and outcome is ReapOutcome.NO_CANDIDATE


def test_compound_command_is_corroborated_by_the_live_stage():
    rows = [ProcRow(10, 999, MARK, "sleep.exe")]
    tree, outcome, _, _ = attribute(
        rec(command="echo hi && sleep 3001; true"), [], rows, cl({10: SLEEP})
    )
    assert outcome is ReapOutcome.KILLED and [p.pid for p in tree] == [10]


def test_argument_mismatch_does_not_corroborate():
    rows = [ProcRow(10, 999, MARK, "sleep.exe")]
    _, outcome, _, _ = attribute(rec(command="sleep 3002"), [], rows, cl({10: SLEEP}))
    assert outcome is ReapOutcome.NO_CANDIDATE
