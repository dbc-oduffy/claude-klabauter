"""coordinator_core.benchmarks.tree_axis_screen — screen the live op surface on a
child-inclusive (process-tree) axis, and write the result where it is joinable.

WHY THIS EXISTS

Every ``process_ms`` figure the engine publishes comes from ``time.process_time()``,
which is single-process CPU and cannot see a child process at all. Ops that do
their work by spawning ``git`` therefore report near-zero, while ops doing the same
work in-process report their real cost. The 2026-08-23 kill batch was selected
against those figures, so it is not the set of over-bar ops — it is the set of ops
that happened to be measured on an axis that flatters spawning code. The error
direction produces only false negatives, so the batch is a lower bound on what
should have been in it.

This module measures the other axis, and writes its output INTO the op-latency
sink schema rather than into a private report. That placement is the point:
``spawn_counter``'s module docstring records that every spawn figure quoted in the
kill sweep came from a bespoke external probe and that *none of them are joinable
against the op ledger*. A figure that cannot be joined is a figure nobody can use
twice.

READ SHAPE — FIXED BY SPIKE, NOT BY TASTE

``docs/research/spike-verdicts/2026-08-28-tree-axis-read-shape-and-resolution-floor.md``

Job accounting lands on ~15.625ms scheduler ticks, so a snapshot pair around ONE
invocation returns a tick COUNT, not a cost, and a median over such samples picks
the low mode — biased downward, which is the same direction as the defect this
screen exists to correct. The fix is prescribed by ``LiveTreeAccountant``'s own
docstring: bracket N invocations in ONE window and divide.

The spike fixed the numbers:

  - ``N = 8`` invocations per window. Quantisation floor is ``tick / N`` ~ 1.95ms.
  - ``WINDOWS = 3``, and the SPREAD across them is reported beside the mean.
  - Practical resolution floor ~25ms under load — the observed spread, which
    dominates quantisation by an order of magnitude. Above N~8 this measurement is
    VARIANCE-limited, not tick-limited, so a larger N spends child spawns on a box
    carrying ~50 concurrent sessions and buys nothing.

NEGATIVE SPEC

  - Never report a per-call median off per-call job snapshots. A different and
    wrong quantity, not a refinement.
  - Never raise N above 8 expecting precision (see variance-limited, above).
  - Never write rows with a production origin. These are benchmark rows and must
    say so: ``invocation_origin``'s docstring names mislabelling production traffic
    as benchmark the worse failure, because it deletes real ops from their own
    census. The inverse — this module's rows entering the production census —
    would inflate every op it touches.
  - Never treat the harness's fixed per-invocation offset as instrument error to be
    silently subtracted. It is measured against ``ping`` (the null op) and BOTH the
    raw and offset-adjusted figures are reported.
  - Never report an op absent from the run as under-bar. Absent is UNSCREENED, and
    the distinction is the whole coverage leg of the plan's exit criterion.

PLATFORM

Windows only — job-object accounting. On any other platform the tree axis is
``None``, never ``0``: a zero would manufacture exactly the false-negative reading
this screen exists to find.
"""

from __future__ import annotations

import ctypes
import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.benchmarks import op_fixtures
from coordinator_core.benchmarks.process_time import (
    IS_WINDOWS,
    LiveTreeAccountant,
    _env_with_benchmark_origin,
)

#: Spike-fixed read shape. Changing either invalidates the published floor —
#: re-run the spike rather than editing these in place.
N_PER_WINDOW = 8
WINDOWS = 3

#: Windows scheduler tick. Job accounting lands on multiples of it.
TICK_MS = 15.625

#: The null op. Its figure IS this harness's per-invocation overhead (client
#: interpreter start plus dispatch), and every other op's own cost is read as the
#: difference from it. Must be a member of the fixture set.
BASELINE_OP = "ping"

#: A per-op figure this close to a tick multiple is reported as instrument-limited
#: rather than as a cost.
TICK_PROXIMITY = 0.03


@dataclass
class OpResult:
    op: str
    windows_ms: List[float] = field(default_factory=list)
    invocations: int = 0
    failures: int = 0
    process_ms_rows: List[float] = field(default_factory=list)
    spawns_rows: List[int] = field(default_factory=list)
    unscreened_reason: Optional[str] = None

    @property
    def screened(self) -> bool:
        return self.unscreened_reason is None and len(self.windows_ms) == WINDOWS

    @property
    def tree_mean_ms(self) -> Optional[float]:
        return statistics.mean(self.windows_ms) if self.windows_ms else None

    @property
    def tree_spread_ms(self) -> Optional[float]:
        if len(self.windows_ms) < 2:
            return None
        return max(self.windows_ms) - min(self.windows_ms)

    @property
    def process_mean_ms(self) -> Optional[float]:
        return statistics.mean(self.process_ms_rows) if self.process_ms_rows else None

    @property
    def near_tick(self) -> bool:
        m = self.tree_mean_ms
        if not m:
            return False
        ratio = m / TICK_MS
        return abs(ratio - round(ratio)) < TICK_PROXIMITY


def resolve_warm_server_pid(repo_root: Path) -> Optional[int]:
    """Newest still-alive warm-server pid, read off the sink's own rows.

    Why the sink and not a process scan: every ``route: warm_server`` row already
    carries the pid that served it, so the answer is on disk and costs no process
    enumeration on a box running ~50 sessions.
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
        return None
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

    for pid, _ in sorted(seen.items(), key=lambda kv: -kv[1]):
        h = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if h:
            k32.CloseHandle(h)
            return pid
    return None


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


def published_invoke_door() -> Path:
    """The shipped `coordinator-invoke` door.

    NOT `python -m coordinator_core.invoke` from this tree. Claude-klabauter carries
    no engine build stamp, so a call from here is refused as a warm-server host by
    ruling (DR-315 s2, corroborated by DR-326/DR-331) and would only run at all
    with `--allow-unstamped-dispatch`, which forces the COLD path. Cold is a
    different axis from the one the sink records (`route: warm_server`), so
    measuring it would answer a question nobody asked.
    """
    import os

    home = os.environ.get("COORDINATOR_SETTINGS_HOME") or str(
        Path.home() / ".coordinator-claude-settings")
    return Path(home) / "bin" / "coordinator-invoke.exe"


def _invoke_argv(op: str, worktree_root: Path) -> List[str]:
    params = op_fixtures.params_json_for(op, worktree_root)
    argv = [str(published_invoke_door()), op, params]
    repo = op_fixtures.repo_arg_for(op, worktree_root)
    if repo is not None:
        argv += ["--repo", str(repo)]
    return argv


def screen_op(op: str, worktree_root: Path, accountant: LiveTreeAccountant,
              repo_root: Path) -> OpResult:
    """Drive one op for WINDOWS x N_PER_WINDOW and read both axes.

    The tree axis is read off the WARM SERVER's job, not off this process's. A
    warm-server op's real work -- and every git child it spawns -- is charged to
    the long-lived server, which is not in the client's tree at all; bracketing
    the client would report the JSON-RPC framer and call it the op.
    """
    res = OpResult(op=op)
    sink = _sink_path(repo_root)
    argv = _invoke_argv(op, worktree_root)
    # Child-scoped benchmark origin. Reuses process_time's own helper rather than
    # declaring globally: a global write would stamp THIS process and every later
    # subprocess as benchmark traffic (the C1d defect at 0c1baedf8). Every row the
    # driven invocations write must carry the benchmark origin, or this screen
    # inflates the production census of every op it touches.
    env = _env_with_benchmark_origin(None)

    for _ in range(WINDOWS):
        offset = _sink_offset(sink)
        before = accountant.snapshot()["process_time_ms"]
        for _ in range(N_PER_WINDOW):
            try:
                proc = subprocess.run(
                    argv, capture_output=True,
                    timeout=op_fixtures.SUBPROCESS_TIMEOUT_S,
                    creationflags=op_fixtures.SUBPROCESS_CREATIONFLAGS,
                    env=env,
                )
                res.invocations += 1
                if proc.returncode != 0:
                    res.failures += 1
            except subprocess.TimeoutExpired:
                res.invocations += 1
                res.failures += 1
        after = accountant.snapshot()["process_time_ms"]
        res.windows_ms.append((after - before) / N_PER_WINDOW)

        for row in _sink_rows_since(sink, offset):
            if row.get("op") != op or row.get("kind") != "process_time":
                continue
            pm = row.get("process_ms")
            if isinstance(pm, (int, float)):
                res.process_ms_rows.append(float(pm))
            sp = row.get("spawns")
            if isinstance(sp, int):
                res.spawns_rows.append(sp)

    if res.failures == res.invocations and res.invocations:
        res.unscreened_reason = "every invocation failed"
    return res


def run_screen(ops: Optional[List[str]] = None) -> Dict[str, OpResult]:
    """Screen the fixture-backed COMPUTE_ONLY population. Windows only."""
    if not IS_WINDOWS:
        raise NotImplementedError(
            "tree_axis_screen is Windows-only; on this platform the tree axis is "
            "None, never 0 -- a zero would manufacture a false under-bar reading."
        )
    population = ops or sorted(op_fixtures.COMPUTE_ONLY_FIXTURES)
    repo_root = Path(__file__).resolve().parents[2]
    worktree_root = op_fixtures.materialize_fixture_repo()
    server_pid = resolve_warm_server_pid(repo_root)
    if server_pid is None:
        raise RuntimeError(
            "no live warm-server pid found in the sink; the tree axis cannot be "
            "read without one. Refusing to fall back to this process's own job -- "
            "that would measure the client framer and report it as the op."
        )
    print(f"  attaching to warm server pid {server_pid}", flush=True)
    accountant = LiveTreeAccountant(server_pid)

    results: Dict[str, OpResult] = {}
    for i, op in enumerate(population, 1):
        print(f"  [{i:2}/{len(population)}] {op} ...", flush=True)
        t0 = time.time()
        results[op] = screen_op(op, worktree_root, accountant, repo_root)
        r = results[op]
        mean = r.tree_mean_ms
        print(f"        tree={mean:8.2f}ms  spread={r.tree_spread_ms or 0:6.2f}  "
              f"fail={r.failures}/{r.invocations}  ({time.time() - t0:.1f}s)",
              flush=True)
    return results
