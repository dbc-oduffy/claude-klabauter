"""Fail-safe attribution of a TaskStop to the one orphan tree its background launch left.

Pure function over a process snapshot; no I/O, no spawn. Any doubt yields no tree.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.bash_guards._session_census import _children, _is_orphan_root, _stem, _subtree
from coordinator_core.bash_guards._taskstop_contract import LaunchRecord, ReapOutcome

# Measured in docs/research/spike-verdicts/2026-10-10-taskstop-background-task-id-probe.md (b):
# a launch's orphan root is created ~30ms before hook receipt; peer processes appear from +0.48s.
PRE_SLACK_S = 2.0
POST_WINDOW_S = 0.25

_TICKS_PER_S = 10_000_000
_PRE_TICKS = int(PRE_SLACK_S * _TICKS_PER_S)
_POST_TICKS = int(POST_WINDOW_S * _TICKS_PER_S)

_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s;&|()]+")

CmdLine = Callable[[int, int], Optional[str]]
Attribution = Tuple[Optional[List[ProcRow]], ReapOutcome, Tuple[str, ...], int]


_STAGE_TOKEN = re.compile(r"\"[^\"]*\"|'[^']*'|[;&|()]+|[^\s;&|()]+")


def _tokens(text: str) -> List[str]:
    return [t.strip("\"'") for t in _TOKEN.findall(text)]


def _stages(command: str) -> List[List[str]]:
    """Token lists of each stage of a compound command, split on unquoted ; & | ( )."""
    stages: List[List[str]] = [[]]
    for t in _STAGE_TOKEN.findall(command):
        if t[0] in ";&|()":
            stages.append([])
        else:
            stages[-1].append(t.strip("\"'"))
    return [s for s in stages if s]


def _argv(cmdline: str) -> List[str]:
    """Tokenised command line with the image reduced to its stem (directory and .exe dropped)."""
    toks = _tokens(cmdline)
    if not toks:
        return []
    image = re.split(r"[\\/]", toks[0])[-1]
    return [_stem(image)] + toks[1:]


def _contiguous(needle: Sequence[str], hay: Sequence[str]) -> bool:
    n = len(needle)
    return n > 0 and any(list(hay[i:i + n]) == list(needle) for i in range(len(hay) - n + 1))


def _depths(tree: Sequence[ProcRow]) -> Dict[int, int]:
    by_pid = {p.pid: p for p in tree}
    out: Dict[int, int] = {}
    for p in tree:
        d, cur = 0, p
        while cur.ppid in by_pid and cur.ppid != cur.pid and d <= len(tree):
            cur = by_pid[cur.ppid]
            d += 1
        out[p.pid] = d
    return out


def _window(mark: int) -> Tuple[int, int]:
    return mark - _PRE_TICKS, mark + _POST_TICKS


def attribute(
    rec: LaunchRecord,
    others: Sequence[LaunchRecord],
    rows: Sequence[ProcRow],
    cmdline: CmdLine,
) -> Attribution:
    """(tree deepest-first, KILLED-eligible outcome, competing task ids, candidate count).

    The tree is non-None only when no other record's launch window overlaps rec's and exactly
    one orphan root created in rec's window holds a process whose command line corroborates
    rec.command. The outcome is then KILLED as the claim marker; the reaper replaces it with
    the real result after killing. Otherwise None with AMBIGUOUS or NO_CANDIDATE.
    An unreadable command line in an orphan tree the window admits cannot be excluded and makes
    the result AMBIGUOUS.
    """
    competing = tuple(
        o.task_id for o in others
        if o.task_id != rec.task_id and abs(o.mark - rec.mark) <= _PRE_TICKS + _POST_TICKS
    )
    if competing:
        return None, ReapOutcome.AMBIGUOUS, competing, 0

    lo, hi = _window(rec.mark)
    by_pid = {r.pid: r for r in rows}
    children = _children(rows)
    want = _stages(rec.command)
    claimed: List[List[ProcRow]] = []
    unexcludable = 0
    for r in rows:
        if not (lo <= r.ctime <= hi) or not _is_orphan_root(r, by_pid):
            continue
        tree = _subtree(r, children)
        corroborated = False
        unreadable = False
        for p in tree:
            text = cmdline(p.pid, p.ctime)
            if text is None:
                unreadable = True
            elif any(_contiguous(_argv(text), w) for w in want):
                corroborated = True
        if corroborated:
            claimed.append(tree)
        elif unreadable:
            unexcludable += 1

    count = len(claimed) + unexcludable
    if count == 0:
        return None, ReapOutcome.NO_CANDIDATE, (), 0
    if count > 1 or unexcludable:
        return None, ReapOutcome.AMBIGUOUS, (), count
    tree = claimed[0]
    depth = _depths(tree)
    return sorted(tree, key=lambda p: depth[p.pid], reverse=True), ReapOutcome.KILLED, (), 1
