"""coordinator_core.benchmarks.tree_axis_screen — warm-server liveness and sink reads.

WHAT IS LEFT HERE. This module used to drive ops synthetically and bracket a warm
server's process tree to charge their child-inclusive cost. That screen is
gravestoned at the foot of this file. What survives are the two primitives it was
built on, which a successor still needs: how many warm servers are live
(``live_warm_server_pids``), and how to read op-latency sink rows (``_sink_path`` /
``_sink_offset`` / ``_sink_rows_since``).

WHY THE SCREEN WENT. Not because it was wrong to want the number. It bracketed ONE
warm server while the box runs several — 7 live when measured, 20 distinct pids in
8000 sink rows, invocations alternating across them — so it charged an unbounded
fraction of each op's work to servers it never watched. That error runs one way
only, toward UNDERSTATEMENT, and an understated figure against a kill bar is a
false acquittal nobody re-checks. See the gravestone below for where the
requirement went.

PLATFORM. Windows only — ``live_warm_server_pids`` uses ``OpenProcess`` for the
liveness check. The sink readers are portable.
"""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
from typing import Dict, List


def live_warm_server_pids(repo_root: Path) -> List[int]:
    """Every still-alive warm-server pid the sink knows, newest-first.

    Why the sink and not a process scan: every ``route: warm_server`` row already
    carries the pid that served it, so the answer is on disk and costs no process
    enumeration on a box running ~50 sessions.

    NEGATIVE SPEC -- this returns a LIST because the box runs more than one server
    and dispatch spreads invocations across them. Measured 2026-08-29: eight
    invocations each of ``ping`` and ``commit.anchors`` alternated 50/50 between
    two live servers, and the live sink carried 20 distinct warm-server pids in
    its last 8000 rows with no dominant one. A caller that collapses this to a
    single pid and brackets that one server measures a fraction of the work and
    reports the fraction as the whole -- an UNDERSTATEMENT, which against a kill
    bar manufactures false under-bar readings. Do not re-add a
    ``resolve_warm_server_pid`` that returns one pid.
    """
    import ctypes.wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]

    seen: Dict[int, float] = {}
    path = _sink_path(repo_root)
    try:
        blob = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in blob.splitlines():
        if '"warm_server"' not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        pid, t = row.get("pid"), row.get("t_start") or 0
        if isinstance(pid, int) and t >= seen.get(pid, 0):
            seen[pid] = t

    live: List[int] = []
    for pid, _ in sorted(seen.items(), key=lambda kv: -kv[1]):
        h = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if h:
            k32.CloseHandle(h)
            live.append(pid)
    return live


def _sink_path(repo_root: Path) -> Path:
    return repo_root / ".git" / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def _sink_offset(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _sink_rows_since(path: Path, offset: int) -> List[dict]:
    """Rows the driven invocations appended during one window.

    Reading by byte offset rather than by timestamp is deliberate: it attributes
    exactly the rows this window produced, with no clock-skew window to tune and
    no risk of sweeping in a concurrent session's rows.
    """
    rows: List[dict] = []
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            blob = fh.read()
    except OSError:
        return rows
    for line in blob.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# GRAVESTONE — run_screen (single-warm-server bracketing) deleted 2026-08-29.
#
# It could only ever raise on this box: `live_warm_server_pids` measured 7 live
# warm servers with invocations alternating across them (20 distinct pids in 8000
# sink rows), and its >1-server branch refused unconditionally rather than
# bracket a fraction of the work and report it as the whole. The spinoff baton's
# anti-scope freezes that refusal permanently ("leave it refusing"), so the
# function had no remaining path to success — dead code with a 4-test fence
# (`test_tree_axis_screen_server_count_guard.py`, also deleted).
#
# The requirement this discharged does not die with it: the tree axis still needs
# a child-inclusive cost figure per op. That requirement is owned by
# `state/handoffs/2026-08-29-readjudicate-the-op-budget-index.md` § 1, against a
# private per-op warm server rather than a shared one nobody can safely bracket.
# Evidence: docs/research/spike-verdicts/2026-08-29-write-safe-fixtures-for-the-spawning-op-population.md.
#
# `live_warm_server_pids` and the sink row-reading helpers above (`_sink_path`,
# `_sink_offset`, `_sink_rows_since`) are preserved: § 1 needs "how many servers
# are live" and a way to read sink rows, and both are genuinely reusable.
#
# `_sink_offset` and `_sink_rows_since` therefore read as unused to a linter, and
# that is deliberate, not an oversight: they are kept FOR the successor named
# above, not because something calls them today. If § 1 lands without using them,
# delete them then — an unused helper kept past the moment its named consumer
# declined it is exactly the "survived because it existed" shape.
