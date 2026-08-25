"""
coordinator_core.benchmarks.boot_backstop_cold -- spawn-and-process-time harness
for the boot path, cold and under load.

Purpose: `docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md` chunk C1.
Every later chunk's AC1/AC3/AC3c/AC3d/AC4 claim about the rebuilt boot backstop
is measured against numbers this module produces -- there is no COLD number for
the boot path anywhere else on disk. "Cold" means a fresh interpreter with no
warm engine, run under whatever live load the box carries at measurement time
(CLAUDE.md § Load norm: the busy box is the average, not the peak) -- never a
bare in-process timing that reuses an already-warm `sys.modules`.

Two instruments, kept deliberately separate because they answer different
questions:

  1. `measure_cold_process_time_n` -- process TIME (not wall clock) for N
     independent cold invocations of a command, reporting best/worst/mean.
     Delegates to `coordinator_core.benchmarks.process_time.batched_process_time_ms`
     (k=1 per sample, called n times) rather than re-deriving Windows job-object
     or Darwin kqueue accounting here -- that module is the single shared
     primitive for this measurement and already documents (and defends against)
     the traps that would otherwise produce a false PASS: `os.times()` children
     fields are always 0.0 on Windows, `psutil.cpu_times()` returns NaN after a
     child process exits on Windows (the plan's own § Anti-scope names this as
     the reason `GetProcessTimes` on the live handle is required, not
     `psutil`), and job-object accounting is tick-quantised at ~15.6ms.

  2. `measure_import_set` -- the `sys.modules` module-COUNT delta from
     importing a target module in a fresh subprocess, under either the
     "armed" (`COORDINATOR_CORE_LAZY_OPS=1`) or "unarmed" (eager-default)
     shape. This is a distinct question from (1) ("how much does this cost"
     vs "how many modules does this pull in") and this module keeps them as
     two functions rather than one, mirroring `import_budget.py`'s own
     module-count-vs-wall-clock split (see that module's "Why module count,
     not wall-clock" docstring section) -- except this probe self-times with
     `time.process_time()`, not wall clock, per this plan's own
     § Anti-scope ("Do not measure in wall clock").

RECONCILING THE IMPORT-SET DISCREPANCY (EM decision 2026-08-23, resolving
staff-eng review F2; binds AC3d). Three different import-set readings for
`coordinator_core.ops.session.boot_sweep` exist on disk before this module:

  - 149 modules -- the predecessor handoff's floor table
    (`state/handoffs/2026-08-21-earn-session-boot-sweep-back-under-2s-co.md`).
  - 567 modules eager-default / 312.5ms -- an EM reading taken 2026-08-23
    (`python -c` with `time.process_time()`, this branch), attributed to
    `coordinator_core.ops`'s eager `_eager_import_all` walking every op module.
  - 115 modules under `COORDINATOR_CORE_LAZY_OPS=1` / 109.4ms -- the same EM
    reading, armed shape.

This is not one number under measurement noise -- it is three different
import-set SHAPES (a stale floor-table reading and two shapes of a live
reading). `IMPORT_SET_HISTORICAL_READINGS` records the three verbatim, tagged
with which shape (armed/unarmed/undated) each was taken under, and this
module's own `measure_import_set`/`reconcile_import_set_readings` re-measure
the *actual* module live on disk rather than silently adopting whichever
historical number is smaller. Per AC3d, the module-count ceiling itself (and
the module this probe targets once C4a/C4b land, e.g.
`coordinator_core.ops.session.boot_backstop`) is that chunk's concern, not
this one's -- this harness's job is to report the three readings honestly and
re-measure on demand, not to gate.

Reference floors from the origin spike, reproduced by `measure_cold_process_time_n`
before any boot-path number is trusted (plan body, chunk C1):

  - bare `python -c pass`: 31.2ms process / 70.4ms wall.
  - 258-file frontmatter scan plus one git query: 78.1ms best / 140.6ms worst,
    over 5 runs.

NEGATIVE SPEC: this module does not implement the boot backstop mechanism
itself (that is C4a/C4b's `coordinator_core/ops/session/boot_backstop.py`) and
does not gate on any budget (AC3/AC3d/AC4's ceilings are those chunks'
concern) -- it is a measurement harness only, callable against whatever
command or module a later chunk names. Wall clock is recorded by
`batched_process_time_ms` as context (`wall_ms`) but this module never
reports it as a verdict, per the plan's own § Anti-scope ("Do not measure in
wall clock").
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
from typing import Dict, List, NamedTuple, Optional, Sequence

from coordinator_core.benchmarks.process_time import batched_process_time_ms
from coordinator_core.benchmarks.timer import SUBPROCESS_CREATIONFLAGS

REFERENCE_FLOOR_BARE_INTERPRETER = {
    "process_ms": 31.2,
    "wall_ms": 70.4,
}
"""Spike floor: `python -c pass`, cold. Reproduce this before trusting any
number this harness reports for real boot-path work."""

REFERENCE_FLOOR_FRONTMATTER_SCAN = {
    "best_ms": 78.1,
    "worst_ms": 140.6,
    "n": 5,
}
"""Spike floor: a 258-file frontmatter scan plus one git query, cold, over 5
runs -- the shape of work the rebuilt backstop's own enumerate/select/move
leg resembles."""

IMPORT_SET_HISTORICAL_READINGS = {
    "coordinator_core.ops.session.boot_sweep": [
        {
            "modules": 149,
            "shape": "undated",
            "elapsed_ms": None,
            "source": (
                "state/handoffs/2026-08-21-earn-session-boot-sweep-back-under-2s-co.md "
                "floor table"
            ),
        },
        {
            "modules": 567,
            "shape": "unarmed",
            "elapsed_ms": 312.5,
            "source": "EM reading 2026-08-23, python -c with time.process_time(), this branch",
        },
        {
            "modules": 115,
            "shape": "armed",
            "elapsed_ms": 109.4,
            "source": "EM reading 2026-08-23, COORDINATOR_CORE_LAZY_OPS=1, this branch",
        },
    ],
}
"""Verbatim record of the three import-set readings this plan's C1 body names
as not being one number under measurement noise (EM decision 2026-08-23,
resolving staff-eng review F2). Never adopt the smallest of these silently --
`measure_import_set`/`reconcile_import_set_readings` re-measure the module
actually on disk instead."""


class ColdProcessTimeSample(NamedTuple):
    """Min/p50/max (plus mean) process-time summary over `n` independent cold
    invocations of one command. `samples`/`wall_ms_samples` are per-invocation
    context; `wall_ms_samples` is recorded only because the plan's own
    § Anti-scope names wall clock as informative-never-verdict, not because
    this module gates on it."""

    best_ms: float
    worst_ms: float
    mean_ms: float
    p50_ms: float
    n: int
    samples: List[float]
    wall_ms_samples: List[float]
    procs_per_call_samples: List[float]
    rc: int


def measure_cold_process_time_n(
    cmd: Sequence[str],
    n: int = 12,
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
) -> ColdProcessTimeSample:
    """Runs `cmd` as `n` independent COLD invocations (a fresh interpreter each
    time, no warm engine) and reports best/worst/mean process time.

    Each invocation is measured with `batched_process_time_ms(cmd, k=1, ...)` --
    k=1 rather than a larger batch because "cold" here means every single one
    of the n samples pays its own interpreter-startup and import cost; batching
    k>1 invocations together would amortise that cost across the batch instead
    of reporting each cold sample honestly. `n` independent single-invocation
    batches is how this module gets a real best/worst spread instead of one
    averaged number (AC3c: "The cold measurement reports min, p50 and max over
    n>=12, not a best-of").

    Raises whatever `batched_process_time_ms` raises (`NotImplementedError` on
    an unsupported platform, `OSError`/`ctypes.WinError`/`RuntimeError` on any
    measurement-mechanism failure) -- no additional degradation.
    """
    if n < 1:
        raise ValueError(f"measure_cold_process_time_n: n must be >= 1, got {n!r}")

    process_samples: List[float] = []
    wall_samples: List[float] = []
    procs_samples: List[float] = []
    rc = 0
    for _ in range(n):
        result = batched_process_time_ms(cmd, k=1, env=env, cwd=cwd)
        process_samples.append(result["process_time_ms"])
        wall_samples.append(result["wall_ms"])
        procs_samples.append(result["procs_per_call"])
        rc = result["rc"]

    return ColdProcessTimeSample(
        best_ms=min(process_samples),
        worst_ms=max(process_samples),
        mean_ms=sum(process_samples) / len(process_samples),
        p50_ms=statistics.median(process_samples),
        n=n,
        samples=process_samples,
        wall_ms_samples=wall_samples,
        procs_per_call_samples=procs_samples,
        rc=rc,
    )


_IMPORT_SET_PROBE_SOURCE = (
    "import sys, time\n"
    "before = set(sys.modules)\n"
    "t0 = time.process_time()\n"
    "__import__({module!r})\n"
    "elapsed_ms = (time.process_time() - t0) * 1000.0\n"
    "after = set(sys.modules)\n"
    "delta = after - before\n"
    "own = [m for m in delta if m.split('.', 1)[0] == 'coordinator_core']\n"
    "print(f'{{len(delta)}} {{elapsed_ms:.4f}} {{len(own)}}')\n"
)
"""Inline probe body run via `python -c` in a fresh subprocess. Self-times
with `time.process_time()` (this process measuring itself -- not the
external-handle GetProcessTimes case `process_time.py` exists for, and not
subject to that module's Windows job-object tick-quantisation trap, which is
about measuring a CHILD from outside) rather than wall clock, per this plan's
own § Anti-scope. Kept inline rather than a third probe script file: this
chunk's declared `writes:` scope is exactly `boot_backstop_cold.py` and its
test, and a bare `python -c` subprocess needs no on-disk script."""

_REPO_ROOT = str(__import__("pathlib").Path(__file__).resolve().parents[2])

_IMPORT_SET_PROBE_TIMEOUT_S = 30


class ImportSetReading(NamedTuple):
    """One fresh-subprocess `sys.modules` delta reading for importing
    `module`, under either the `armed` (`COORDINATOR_CORE_LAZY_OPS=1`) or
    unarmed (eager-default) shape."""

    module: str
    armed: bool
    module_count: int
    own_module_count: int
    elapsed_process_ms: float


def measure_import_set(
    module: str,
    armed: bool,
    python: Optional[str] = None,
) -> ImportSetReading:
    """Measures the `sys.modules` module-count delta (and this process's own
    process time) of `import <module>` in a fresh subprocess, under the
    `armed`/unarmed shape named by `armed`.

    `armed=True` sets `COORDINATOR_CORE_LAZY_OPS=1` in the child's
    environment (the two-channel flag documented at the top of
    `coordinator_core/ops/__init__.py` -- the env-var channel, not the
    `sys._coordinator_core_lazy_ops` attribute channel, since this is a
    fresh subprocess with no attribute to set yet). `armed=False` strips the
    var entirely (rather than setting it to some other value) so no future
    accepted value for the var can quietly change which shape is measured.

    Isolation is load-bearing, same reasoning as `_import_probe.py`'s own
    docstring: measuring in-process after a sibling import already pulled the
    target (or its dependencies) into `sys.modules` silently undercounts the
    delta.

    Runs the probe with `-B` (bypass `.pyc` bytecode caching) -- found
    load-bearing during this chunk's own verification: on this shared,
    heavily concurrent tree a `.pyc` under `coordinator_core/hooks/__pycache__/`
    was newer-mtime than its source yet compiled from a stale prior revision
    (source content had moved on without its mtime changing enough to
    invalidate the cache under concurrent checkouts), so an import through
    the normal cache picked up code that no longer matches disk. `-B` forces
    a fresh compile from the actual source every probe run, which is what a
    COLD measurement means here in the first place.
    """
    python = python or sys.executable
    env = dict(os.environ)
    if armed:
        env["COORDINATOR_CORE_LAZY_OPS"] = "1"
    else:
        env.pop("COORDINATOR_CORE_LAZY_OPS", None)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else _REPO_ROOT
    )

    probe_src = _IMPORT_SET_PROBE_SOURCE.format(module=module)
    result = subprocess.run(
        [python, "-B", "-c", probe_src],
        capture_output=True,
        text=True,
        timeout=_IMPORT_SET_PROBE_TIMEOUT_S,
        check=False,
        creationflags=SUBPROCESS_CREATIONFLAGS,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"import-set probe for module {module!r} (armed={armed}) exited "
            f"{result.returncode}: {result.stderr}"
        )
    fields = result.stdout.strip().split()
    module_count, elapsed_ms_str, own_module_count = fields[0], fields[1], fields[2]
    return ImportSetReading(
        module=module,
        armed=armed,
        module_count=int(module_count),
        own_module_count=int(own_module_count),
        elapsed_process_ms=float(elapsed_ms_str),
    )


def reconcile_import_set_readings(
    module: str,
    python: Optional[str] = None,
) -> Dict[str, object]:
    """Re-measures `module`'s import set under both shapes (armed and
    unarmed) against the module ACTUALLY on disk, and returns them alongside
    any historical reading recorded in `IMPORT_SET_HISTORICAL_READINGS` for
    the same module path -- so a caller sees the live number and the
    historical readings side by side rather than one silently overwriting
    the other.

    A module this plan DELETES (the old composite) is not an error here: its
    live halves come back None with `live_module_absent` True, and its
    historical record still reconciles. A retired shape's numbers are history
    -- refusing to report them because the module is gone would destroy the
    very record AC3d asks be reconciled.

    This is the reconciliation AC3d requires before it can be trusted: a
    fresh measurement, tagged with the shape each half was taken under, next
    to the historical record rather than in place of it.
    """
    def _measure(is_armed: bool) -> Optional[Dict[str, object]]:
        try:
            return measure_import_set(module, armed=is_armed, python=python)._asdict()
        except RuntimeError as exc:
            if "ModuleNotFoundError" not in str(exc):
                raise
            return None

    armed = _measure(True)
    unarmed = _measure(False)
    return {
        "module": module,
        "armed": armed,
        "unarmed": unarmed,
        "historical": list(IMPORT_SET_HISTORICAL_READINGS.get(module, [])),
        "live_module_absent": armed is None and unarmed is None,
    }


def _main() -> None:
    """Manual reproduction entry point -- `python -m
    coordinator_core.benchmarks.boot_backstop_cold [module]`. Prints the
    bare-interpreter reference-floor reproduction and, if a module path is
    given, its reconciled import-set readings. Not a test; a human-facing
    sanity check the plan body asks be run "before trusting the harness"."""
    bare = measure_cold_process_time_n([sys.executable, "-c", "pass"], n=12)
    print(
        f"bare interpreter: best={bare.best_ms:.1f}ms worst={bare.worst_ms:.1f}ms "
        f"mean={bare.mean_ms:.1f}ms (reference floor: "
        f"{REFERENCE_FLOOR_BARE_INTERPRETER['process_ms']}ms process)"
    )
    if len(sys.argv) > 1:
        module = sys.argv[1]
        reconciled = reconcile_import_set_readings(module)
        print(reconciled)


if __name__ == "__main__":
    _main()
