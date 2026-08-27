"""
coordinator_core.benchmarks.catering_path_bench -- C1's relay-boundary
baseline (docs/plans/2026-08-21-catering-costs-what-the-work-costs.md).

Purpose: the harness every later chunk in this plan reports against, and
the only artifact that can discharge AC2/AC3. Measures at the RELAY
boundary the plan's Hard constraints name -- both ops
(`hooks.track_dispatched_agents` then `hooks.cater_subagent_start`) through
one `ipc.dispatch_ops_from_hook` call, a FRESH cold interpreter per sample,
n>=30, under whatever load the box is carrying at measurement time. A
figure for `compose_catering`'s own entry point alone does not discharge
either AC (plan Hard constraints: "the child waits on both legs").

Reports, per phase and in total, spawn count (the deciding figure per
CLAUDE.md's brightline and `op_budget_suspension.py`'s own reinstatement
contract), process time, and wall alongside -- p50 and p90 over n samples,
never a single sample. Spawn attribution reuses the technique
`benchmarks/bash_dispatch_probe.py :: enumerate_spawn_set` already applies
to the guard chain (patch `subprocess.Popen.__init__`, walk the real call
stack at construction time) -- the same one the git-state-reader plan used
to trace 21 spawns to their call sites, cited by this chunk's own dispatch
brief.

Two measurement instruments, composed rather than one invented from
scratch:

  1. `time.process_time()` deltas taken INSIDE a fresh child interpreter
     around `import coordinator_core.ipc` and around the
     `dispatch_ops_from_hook` call itself, self-reported by the child over
     stdout because `time.process_time()` only ever sees the calling
     process, never a child it spawns. NOTE, measured building this
     harness: this boundary does NOT reproduce the plan Problem section's
     390.6ms/15.6ms import/compose split byte-for-byte -- `coordinator_
     core.ipc`'s own module body is cheap, and the eager `hooks` package
     import (the actual 390ms cost) is triggered lazily, during op
     resolution INSIDE `dispatch_ops_from_hook`, not at the top-level
     `import ipc` line. So here `import_cpu_ms` measures `ipc`'s own
     bare-module cost and `compose_cpu_ms` carries the hooks-package import
     plus both ops' real work combined -- a different, still-honest split
     of the same total, not the earlier session's split relabelled.
  2. `process_time.batched_process_time_ms` at `k=1` -- a Windows job
     object assigned to the SAME child before it runs, which accounts
     `TotalUserTime + TotalKernelTime` and `TotalProcesses` across the
     WHOLE process tree the child spawns (catching the `git rev-parse`
     leg `time.process_time()` structurally cannot see). `procs_per_call`
     at `k=1`, minus the one process for the interpreter itself, is this
     sample's spawn count.

Every sample therefore costs two cold subprocess launches -- one untimed
(`subprocess.run`, captures the self-reported JSON breakdown), one timed
(`batched_process_time_ms`, captures total process time / wall / spawn
count) -- both running the byte-identical script. Two launches per sample
is deliberate: `batched_process_time_ms` discards stdout by construction
(job-object accounting has no per-invocation output channel), so the two
concerns cannot share one launch without inventing a second IPC channel
this baseline does not need.

CONTROL (plan body: "prefer a CONTROL where one exists"): the SAME relay
call, `ipc.dispatch_ops_from_hook`, against ONE stubbed no-op op registered
directly via `ipc.register_op` -- never through `coordinator_core.hooks`,
so the control's import graph excludes the eager hooks package entirely.
This isolates the floor cost of `dispatch_ops_from_hook` itself (event
loop spin-up, JSON-RPC envelope construction/parsing, registry lookup)
from what catering's own two ops add on top of it.

`spawn_count`'s `- 1` (AC22): both platforms' `procs_per_call` are
ROOT-INCLUSIVE, not exclusive -- Windows because the job object counts the
parent interpreter itself (`TotalProcesses`, `process_time.py`), Darwin
because `_darwin_one_invocation` seeds `seen = {root_pid}` before counting
forked children (`process_time.py`). This is checked here against a
known-truth fixture at runtime (`_verify_spawn_count_derivation`, the same
`python -> git --version` oracle `test_process_time_posix.py ::
test_python_to_git_oracle_count_is_exactly_two` asserts at exactly `2.0`),
not assumed from the Windows framing alone -- a platform whose count ever
stopped including the root would make every one of that platform's
`spawn_count` figures silently off by one otherwise.

Negative spec:
    - Does NOT assert a pass/fail threshold against AC2/AC3 -- this module
      only measures and records. C6 re-runs this same harness post-fix and
      is the chunk that discharges those criteria.
    - Does NOT reuse `SUSPENDED_OPS`' `measured` figures as a baseline --
      those are wall-clock occupancy from the op-latency sink (plan
      Anti-scope), a different measurement for a different question.
    - Does NOT run on Linux or any platform besides Windows/Darwin:
      `batched_process_time_ms` has primitives for Windows (job object) and
      Darwin (kqueue) only (module docstring), and DR-344's brightline is
      itself scoped to this box.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from coordinator_core.benchmarks.process_time import IS_DARWIN, IS_WINDOWS, batched_process_time_ms
from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "budget-manifest.json")

#: Session-measured starting point (plan C1 body), carried here so a reader
#: sees what this harness re-establishes rather than inherits.
SESSION_MEASURED_STARTING_POINT = {
    "import_cpu_p50_ms": 390.6,
    "compose_cpu_p50_ms": 15.6,
    "total_wall_p50_ms": 744.6,
    "total_wall_p90_ms": 1321.2,
    "spawns_per_fire": 1,
}

#: Real relay payload shape (plan Problem section): `contract_blocks` as a
#: LIST, the shape `track-dispatched-agents.py ::
#: _dispatch_subagent_start_ops` actually sends today.
_ELIGIBLE_TYPE = "coordinator:code-reviewer"
_AGENT_ID = "abcdef0123456789"
_SESSION_ID = "catering-path-bench-session"

_CONTROL_OP_NAME = "benchmarks.catering_relay_control_noop"


def _bookkeeping_params() -> Dict[str, Any]:
    return {
        "session_id": _SESSION_ID,
        "dispatched_agent_id": _AGENT_ID,
        "dispatched_model": "claude-x",
        "subagent_type": _ELIGIBLE_TYPE,
    }


def _resolve_bench_block_names() -> List[str]:
    """The BLOCK names `_ELIGIBLE_TYPE` maps to, read from the same policy
    file the relay reads.

    `contract_blocks` is a list of snippet names (`provisioned-scaffold-
    precedence`, ...), NOT of agent types -- `track-dispatched-agents.py ::
    _resolve_contract_blocks` resolves type -> list shim-side and sends the
    RESULT. Passing the agent type here instead produced
    `contract block 'coordinator:code-reviewer' unreadable at
    <plugin_root>/snippets/coordinator:code-reviewer.md` and, because
    `_assemble_contract_blocks` is all-or-nothing, made every sample measure
    a compose whose blocks leg had already failed -- benchmarking the empty
    path while reporting it as the real one.

    Resolved from policy rather than hardcoded so the bench keeps measuring
    the real block set as that policy changes. Falls back to `[]` on any
    read failure, which yields an honestly EMPTY blocks leg rather than a
    silently wrong one; the caller asserts a non-empty compose, so a
    degraded resolution surfaces instead of scoring well by doing less work.
    """
    plugin_root = resolve_plugin_root()
    if not plugin_root:
        return []
    policy_path = Path(plugin_root) / "subagent-sandbox-policy.yaml"
    try:
        import yaml

        data = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    rows = data.get("contract_blocks") or {}
    names = rows.get(_ELIGIBLE_TYPE) or []
    return [n for n in names if isinstance(n, str)]


def _cater_params(cwd: str) -> Dict[str, Any]:
    return {
        "agent_id": _AGENT_ID,
        "session_id": _SESSION_ID,
        "cwd": cwd,
        "agent_type": _ELIGIBLE_TYPE,
        "contract_blocks": _resolve_bench_block_names(),
    }


# ---------------------------------------------------------------------------
# Per-sample child script bodies -- one for the real relay, one for the
# stubbed-op control. Each prints exactly one JSON line to stdout:
# {"import_cpu_ms": float, "compose_cpu_ms": float}
# ---------------------------------------------------------------------------

_REAL_RELAY_SCRIPT = """
import json
import time

t0 = time.process_time()
import coordinator_core.ipc as ipc
t1 = time.process_time()

# Sanctioned unstamped-dispatch caller (ipc.py :: allow_unstamped_dispatch
# docstring, clause 2 -- "deliberate manual testing"): this benchmark
# dispatches directly against a dev tree with no build stamp, never through
# coordinator-invoke.
ipc.allow_unstamped_dispatch()

# Sanctioned suspended-op exercise (conftest.py :: exercise_suspended_op's
# own documented carve-out, reproduced here since this runs as a standalone
# child process, not under pytest): a benchmark measuring THIS op's own
# reinstatement case is not the box a real caller is queued behind, and
# this is process-local, never a write to the real SUSPENDED_OPS table.
import coordinator_core.op_budget_suspension as _obs
_obs.SUSPENDED_OPS = {}

results = ipc.dispatch_ops_from_hook(
    [
        ("hooks.track_dispatched_agents", %(bookkeeping)s),
        ("hooks.cater_subagent_start", %(cater)s),
    ],
    origin_worktree=%(cwd)r,
)
t2 = time.process_time()

for r in results:
    if isinstance(r, ipc.HookDispatchError):
        raise SystemExit("catering_path_bench: op errored: %%r" %% (r,))

print(json.dumps({"import_cpu_ms": (t1 - t0) * 1000.0, "compose_cpu_ms": (t2 - t1) * 1000.0}))
"""

_CONTROL_RELAY_SCRIPT = """
import json
import time

t0 = time.process_time()
import coordinator_core.ipc as ipc

def _noop_handler(params, repo_root=None):
    return {}

ipc.register_op(%(op_name)r, _noop_handler)
ipc.allow_unstamped_dispatch()
t1 = time.process_time()

results = ipc.dispatch_ops_from_hook(
    [
        (%(op_name)r, {}),
        (%(op_name)r, {}),
    ],
    origin_worktree=%(cwd)r,
)
t2 = time.process_time()

for r in results:
    if isinstance(r, ipc.HookDispatchError):
        raise SystemExit("catering_path_bench: control op errored: %%r" %% (r,))

print(json.dumps({"import_cpu_ms": (t1 - t0) * 1000.0, "compose_cpu_ms": (t2 - t1) * 1000.0}))
"""


def _real_relay_argv_and_script(cwd: str) -> str:
    return _REAL_RELAY_SCRIPT % {
        "bookkeeping": repr(_bookkeeping_params()),
        "cater": repr(_cater_params(cwd)),
        "cwd": cwd,
    }


def _control_relay_script(cwd: str) -> str:
    return _CONTROL_RELAY_SCRIPT % {"op_name": _CONTROL_OP_NAME, "cwd": cwd}


def _percentile(sorted_samples: List[float], pct: float) -> float:
    """Nearest-rank percentile over an already-sorted sample list. Small,
    dependency-free helper deliberately duplicated rather than imported from
    `harness.py :: _percentile` -- that name is module-private to a
    different harness measuring a different contract (per-op wall-clock SLA
    conformance), and this module's own negative spec keeps this baseline
    from acquiring a cross-harness import for an eight-line function."""
    if not sorted_samples:
        raise ValueError("_percentile: empty sample list")
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    rank = pct / 100.0 * (len(sorted_samples) - 1)
    lower = int(rank)
    upper = min(lower + 1, len(sorted_samples) - 1)
    frac = rank - lower
    return sorted_samples[lower] + (sorted_samples[upper] - sorted_samples[lower]) * frac


@dataclass
class PhaseStats:
    """p50/p90 over one metric's samples, plus the raw sample list (AC2/AC3
    both need the MAX, not only the quantiles -- plan Hard constraints: "a
    single sample is refused", and C6's AC3 discharge is a MAX across the
    whole sample, so the max is carried here rather than dropped after the
    quantiles are taken)."""

    p50: float
    p90: float
    max: float
    samples: List[float] = field(repr=False)

    @classmethod
    def from_samples(cls, samples: List[float]) -> "PhaseStats":
        ordered = sorted(samples)
        return cls(
            p50=round(_percentile(ordered, 50), 3),
            p90=round(_percentile(ordered, 90), 3),
            max=round(ordered[-1], 3),
            samples=[round(s, 3) for s in samples],
        )

    def to_dict(self, *, with_samples: bool = False) -> Dict[str, Any]:
        out = {"p50": self.p50, "p90": self.p90, "max": self.max}
        if with_samples:
            out["samples"] = self.samples
        return out


@dataclass
class RelayMeasurement:
    """One phase's (real relay or control) full n-sample measurement."""

    n: int
    import_cpu_ms: PhaseStats
    compose_cpu_ms: PhaseStats
    total_process_ms: PhaseStats
    total_wall_ms: PhaseStats
    spawn_count: PhaseStats
    procs_per_call: PhaseStats

    def to_dict(self, *, with_samples: bool = False) -> Dict[str, Any]:
        return {
            "n": self.n,
            "import_cpu_ms": self.import_cpu_ms.to_dict(with_samples=with_samples),
            "compose_cpu_ms": self.compose_cpu_ms.to_dict(with_samples=with_samples),
            "total_process_ms": self.total_process_ms.to_dict(with_samples=with_samples),
            "total_wall_ms": self.total_wall_ms.to_dict(with_samples=with_samples),
            "spawn_count": self.spawn_count.to_dict(with_samples=with_samples),
            "procs_per_call": self.procs_per_call.to_dict(with_samples=with_samples),
        }


def _verify_single_invocation_succeeds(script: str) -> None:
    """One untimed, unbatched invocation with `check=True` first -- a script
    that cannot even complete once must fail loud here, not hide inside a
    batched/looped average (mirrors `bash_dispatch_probe.py`'s own
    precedent)."""
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _measure_one_sample(script: str) -> Tuple[float, float, float, float, int, float]:
    """Two cold launches of the byte-identical *script*: one untimed to
    capture the self-reported `(import_cpu_ms, compose_cpu_ms)` JSON line,
    one timed via `batched_process_time_ms(k=1)` to capture
    `(total_process_ms, total_wall_ms, spawn_count)`.

    Returns the RAW `procs_per_call` as a sixth term alongside the derived
    `spawn_count`. The derivation `max(0, round(procs_per_call) - 1)` is NOT
    injective at the low end -- `procs_per_call` of 0.0 (the primitive observed
    no process at all, a reading `budget-manifest.json`'s
    `_hook_seam_http_transport` warm arms genuinely produce) and 1.0 (the
    interpreter itself, zero children) BOTH derive to `spawn_count` 0. A
    recorded 0 therefore cannot on its own distinguish "this path spawns
    nothing" from "the instrument saw nothing", which is the same
    figure-without-its-invocation-shape defect this plan has withdrawn three
    figures for. Record both; interpret the raw one.

    `spawn_count` is `procs_per_call - 1`: both the Windows job object's
    `TotalProcesses` and Darwin's `_darwin_one_invocation` seen-set count
    the interpreter child itself as one process (module docstring,
    `_verify_spawn_count_derivation`), and this baseline reports additional
    processes spawned FROM it (e.g. `resolve_git_root`'s `git rev-parse`),
    matching the plan's "1 spawn/fire" framing.
    """
    if not (IS_WINDOWS or IS_DARWIN):
        raise NotImplementedError(
            "catering_path_bench requires Windows (job-object timing) or "
            "Darwin (kqueue timing) -- batched_process_time_ms has no "
            "spawn-count primitive on this platform"
        )

    reported = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    breakdown = json.loads(reported.stdout.strip().splitlines()[-1])

    timed = batched_process_time_ms([sys.executable, "-c", script], k=1, cwd=_REPO_ROOT)
    if timed["rc"] != 0:
        raise RuntimeError(
            "catering_path_bench: timed invocation exited %r running the same script "
            "the untimed leg just verified" % (timed["rc"],)
        )

    return (
        float(breakdown["import_cpu_ms"]),
        float(breakdown["compose_cpu_ms"]),
        float(timed["process_time_ms"]),
        float(timed["wall_ms"]),
        max(0, int(round(timed["procs_per_call"])) - 1),
        float(timed["procs_per_call"]),
    )


def _verify_spawn_count_derivation() -> None:
    """AC22: `spawn_count = procs_per_call - 1` (`_measure_one_sample`)
    encodes a WINDOWS fact -- the job object counts the parent process, so
    subtracting it yields children. Whether Darwin's `procs_per_call` also
    includes the root is a property of `process_time.py`'s own Darwin
    implementation, not an assumption safe to carry over unchecked -- so
    this checks it against a known-truth fixture and asserts the answer
    rather than trusting the Windows framing on a platform it was never
    derived from.

    The fixture is `python -> python -c pass`, DERIVED (not
    observed-then-blessed) at `1 root interpreter + 1 child = 2.0`
    root-inclusive. If this platform's `procs_per_call` ever excluded the
    root it would measure `1.0` here, `spawn_count` would come out `0`
    instead of the one real child, and every figure this baseline reports
    would be off by one, silently, in a bench nobody would think to
    distrust -- this raises loud instead.

    WHY NOT `python -> git --version`, which this fixture used until
    2026-08-25 and which `test_process_time_posix.py ::
    test_python_to_git_oracle_count_is_exactly_two` still asserts on POSIX:
    it is not single-child on Windows. Measured here, deterministically,
    5/5 runs: `python -> git --version` reads `procs_per_call=3.0` against
    a `1.0` no-child baseline, so Git for Windows spawns a helper of its
    own and the oracle's "exactly one git child" premise is false on this
    platform. The derivation under test was never wrong; its known-truth
    binary was, and the guard fired correctly on a fixture that could not
    hold -- refusing to run the whole bench on Windows, where it is
    needed. A second interpreter is the narrower oracle: its child count is
    a property of CPython rather than of whichever git build is on PATH.
    """
    cmd = [
        sys.executable,
        "-c",
        "import subprocess, sys; subprocess.run([sys.executable, '-c', 'pass'])",
    ]
    timed = batched_process_time_ms(cmd, k=1, cwd=_REPO_ROOT)
    if timed["rc"] != 0:
        raise RuntimeError(
            "catering_path_bench: spawn_count derivation fixture "
            "(python -> python -c pass) exited %r" % (timed["rc"],)
        )
    spawn_count = max(0, int(round(timed["procs_per_call"])) - 1)
    if spawn_count != 1:
        raise RuntimeError(
            "catering_path_bench: spawn_count derivation is WRONG on this "
            "platform -- python -> python -c pass fixture measured "
            "procs_per_call=%r, so spawn_count = procs_per_call - 1 = %r, "
            "expected exactly 1 (the one real child interpreter). If this "
            "platform's process count excludes the root, the '- 1' in "
            "_measure_one_sample silently under-reports every spawn_count "
            "figure by one." % (timed["procs_per_call"], spawn_count)
        )


def measure_relay(script: str, n: int = 30) -> RelayMeasurement:
    """Collects *n* cold, fresh-interpreter samples of *script* (the real
    relay or the control) and returns the per-phase `p50`/`p90`/`max`
    breakdown. `n` defaults to 30, the plan's own stated floor -- a caller
    passing fewer is trusted to know it is producing a non-conforming
    baseline (this function does not itself enforce the floor; C1's own
    `__main__` entry point below always calls it at n>=30)."""
    _verify_single_invocation_succeeds(script)

    import_samples: List[float] = []
    compose_samples: List[float] = []
    process_samples: List[float] = []
    wall_samples: List[float] = []
    spawn_samples: List[float] = []
    procs_samples: List[float] = []

    for _ in range(n):
        import_ms, compose_ms, process_ms, wall_ms, spawns, procs = _measure_one_sample(script)
        import_samples.append(import_ms)
        compose_samples.append(compose_ms)
        process_samples.append(process_ms)
        wall_samples.append(wall_ms)
        spawn_samples.append(float(spawns))
        procs_samples.append(procs)

    return RelayMeasurement(
        n=n,
        import_cpu_ms=PhaseStats.from_samples(import_samples),
        compose_cpu_ms=PhaseStats.from_samples(compose_samples),
        total_process_ms=PhaseStats.from_samples(process_samples),
        total_wall_ms=PhaseStats.from_samples(wall_samples),
        spawn_count=PhaseStats.from_samples(spawn_samples),
        procs_per_call=PhaseStats.from_samples(procs_samples),
    )


# ---------------------------------------------------------------------------
# Spawn attribution -- every spawn traced to its call site, in-process
# (bash_dispatch_probe.py :: enumerate_spawn_set's own technique).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpawnRecord:
    argv: Tuple[str, ...]
    stack: Tuple[str, ...]


def _stack_summary(depth: int = 10) -> Tuple[str, ...]:
    frames = traceback.extract_stack()[:-2]  # drop this frame and the patched __init__ frame
    tail = frames[-depth:]
    return tuple("%s:%d:%s" % (os.path.basename(f.filename), f.lineno, f.name) for f in tail)


class _SpawnRecorder:
    """Patches `subprocess.Popen.__init__` to log every construction with
    its call stack, then restores the original unconditionally -- catches
    `run`/`call`/`check_output`/a bare `Popen(...)` alike, in one seam."""

    def __init__(self) -> None:
        self.records: List[SpawnRecord] = []
        self._orig_init: Optional[Callable] = None

    def __enter__(self) -> "_SpawnRecorder":
        self._orig_init = subprocess.Popen.__init__
        orig_init = self._orig_init
        records = self.records

        def _patched_init(popen_self, args, *a, **kw):  # noqa: ANN001
            if isinstance(args, (list, tuple)):
                argv = tuple(str(x) for x in args)
            else:
                argv = (str(args),)
            records.append(SpawnRecord(argv=argv, stack=_stack_summary()))
            return orig_init(popen_self, args, *a, **kw)

        subprocess.Popen.__init__ = _patched_init  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info) -> None:
        subprocess.Popen.__init__ = self._orig_init  # type: ignore[assignment]



def _remove_minted_session_dir(cwd: str) -> None:
    """Remove the real session directory `_SESSION_ID` mints under `cwd`.

    NEGATIVE-SPEC -- do NOT "fix" this by adding `_SESSION_ID` to
    `liveness._NON_SESSION_DIR_NAMES`. That constant's own `hook-observations`
    comment records the 2026-08-19 ruling: phantom-session writers are DELETED,
    never quieted with a passlist entry, per
    `test_every_non_uuid_real_child_is_denylisted_or_a_file`'s instruction.

    Why a real directory exists to remove at all: this bench measures the REAL
    relay against the REAL worktree (`origin_worktree=cwd`), so
    `hooks.track_dispatched_agents` writes `dispatched-agents.txt` under
    `<cwd>/.git/coordinator-sessions/<_SESSION_ID>/` exactly as it would for a
    live session. Pointing the bench at a throwaway worktree instead would stop
    measuring the path it exists to measure. So the side effect is kept and
    reversed rather than avoided.

    Why it matters beyond a stray directory: `live_session_verdicts` enumerates
    every non-denylisted child of the sessions root, and a freshly-mtimed one
    reads LIVE on the Layer-2 recency window. Left behind, this directory is a
    phantom live peer in every peer-claim adjudication on the box for as long as
    its mtime stays warm -- i.e. a benchmark run perturbs claim decisions for
    every concurrent session, not just its own.

    Best-effort by construction: a bench must never fail the measurement it just
    took because cleanup lost a race with a concurrent reader.
    """
    import shutil

    victim = os.path.join(cwd, ".git", "coordinator-sessions", _SESSION_ID)
    if os.path.basename(victim) != _SESSION_ID:
        return
    try:
        shutil.rmtree(victim)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def enumerate_spawn_set(cwd: str) -> List[SpawnRecord]:
    """Runs the real relay call IN-PROCESS (this interpreter, never a
    spawned child -- the patch above only sees `subprocess.Popen`
    constructions on the interpreter it is installed in) against the real
    payload shape, and returns every observed `SpawnRecord`."""
    import coordinator_core.ipc as ipc
    import coordinator_core.op_budget_suspension as obs

    ipc.allow_unstamped_dispatch()
    # Sanctioned suspended-op exercise (conftest.py :: exercise_suspended_op's
    # own documented carve-out): save/restore around the call since this runs
    # inside the long-lived benchmark process, not a throwaway child.
    orig_suspended_ops = obs.SUSPENDED_OPS
    obs.SUSPENDED_OPS = {}
    try:
        with _SpawnRecorder() as recorder:
            results = ipc.dispatch_ops_from_hook(
                [
                    ("hooks.track_dispatched_agents", _bookkeeping_params()),
                    ("hooks.cater_subagent_start", _cater_params(cwd)),
                ],
                origin_worktree=cwd,
            )
    finally:
        obs.SUSPENDED_OPS = orig_suspended_ops
        _remove_minted_session_dir(cwd)
    for r in results:
        if isinstance(r, ipc.HookDispatchError):
            raise RuntimeError("enumerate_spawn_set: op errored: %r" % (r,))
    return recorder.records


# ---------------------------------------------------------------------------
# budget-manifest.json recording -- the pre-fix baseline as a stored
# comparison, not a remembered one (plan C1 body).
# ---------------------------------------------------------------------------


def _capture_code_sha() -> str:
    from coordinator_core.git.run import run_git

    result = run_git(["rev-parse", "HEAD"], cwd=_REPO_ROOT)
    if not result.ok:
        return "unknown"
    return result.stdout.strip()


def run_and_record(n: int = 30, cwd: Optional[str] = None) -> Dict[str, Any]:
    """Runs the real relay and the control at `n` samples each, enumerates
    the real relay's spawn set once, and returns the full recorded baseline
    block written into `budget-manifest.json`'s `_catering_relay_baseline`
    key (schema modelled on `_phase0_baseline`'s own retained-provenance
    shape)."""
    _verify_spawn_count_derivation()
    resolved_cwd = cwd if cwd is not None else _REPO_ROOT

    real = measure_relay(_real_relay_argv_and_script(resolved_cwd), n=n)
    control = measure_relay(_control_relay_script(resolved_cwd), n=n)
    spawns = enumerate_spawn_set(resolved_cwd)

    return {
        "measured_at": datetime.now(timezone.utc).date().isoformat(),
        "code_sha": _capture_code_sha(),
        "run_id": str(uuid.uuid4()),
        "sample_count": n,
        "starting_point": SESSION_MEASURED_STARTING_POINT,
        "real_relay": real.to_dict(),
        "control_relay": control.to_dict(),
        "spawn_attribution": [
            {"argv": list(s.argv), "stack": list(s.stack)} for s in spawns
        ],
        "doc": "docs/plans/2026-08-21-catering-costs-what-the-work-costs.md",
        "note": (
            "Pre-fix baseline for AC2/AC3, measured at the relay boundary "
            "(both ops through one ipc.dispatch_ops_from_hook), cold "
            "interpreter per sample, real payload shape. control_relay "
            "isolates the floor cost of dispatch_ops_from_hook itself "
            "(a stubbed no-op op registered outside the hooks package's "
            "eager import graph) from what catering's two ops add on top."
        ),
        "provenance": (
            "Frozen point-in-time stamp, not a value any code resolves. Re-run "
            "coordinator_core.benchmarks.catering_path_bench for a fresh "
            "measurement; C6 re-runs this harness post-fix and records its own "
            "block for the AC2/AC3 comparison."
        ),
    }


def record_baseline(n: int = 30, cwd: Optional[str] = None) -> Dict[str, Any]:
    """`run_and_record`, then writes the result into `budget-manifest.json`
    under `_catering_relay_baseline`, preserving every other key
    untouched."""
    baseline = run_and_record(n=n, cwd=cwd)

    manifest_path = Path(_MANIFEST_PATH)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["_catering_relay_baseline"] = baseline
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n"
    )
    return baseline


if __name__ == "__main__":  # pragma: no cover
    result = record_baseline(n=30)
    print(json.dumps(result, indent=2))
