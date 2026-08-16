"""coordinator_core.benchmarks.shim_fanin_measure -- CORRECTED 2nd
measurement, the plan's actual fan-in thesis (docs/plans/2026-08-16-a-
process-per-predicate.md, § Problem, M-05: "a ceremony running eight of
them pays 16 processes to evaluate eight small predicates that are eight
function calls in one interpreter").

Compares N=FAN_IN_N separate process spawns against ONE process evaluating
the same N predicates in-interpreter, via `interleave.run_interleaved` (the
same interleaved-sampling primitive `shim_decision_rule`'s own comparisons
use, so ambient-load drift lands on both arms roughly equally rather than
biasing whichever is measured first -- see `interleave.py`'s own
docstring).

n_separate_processes arm: spawns one `python -c "import <module>"` child
process PER predicate module in `PREDICATE_MODULES` (FAN_IN_N of them,
sequentially, within ONE timed draw) -- each child pays a real predicate
import, not a bare `pass`, so the comparison is not comparing process
creation against nothing.

fan_in_one_process arm: spawns exactly ONE `python -c "import a; import
b; ..."` child process that imports ALL of `PREDICATE_MODULES` in the
same interpreter -- one process, all N predicates. Deliberately NOT
measured by calling `importlib.import_module` inside the ALREADY-RUNNING
benchmark interpreter: this package (`coordinator_core.ops`) eagerly
registers its full ~200-module op registry the first time any submodule
under it is imported (`coordinator_core/ops/__init__.py::
_eager_import_all`), and the benchmark process has ALREADY paid that cost
once (transitively, importing `shim_decision_rule` pulls in
`coordinator_core.ops._git_root_util`) before this arm ever runs. Timing
individual re-imports against that already-warm package would measure
"warm cache vs. cold cache", not "N processes vs. one process" -- a
different confound than the modulo-arithmetic one this module was
corrected to fix. Spawning ONE process per fan-in draw, exactly as the
baseline spawns N, keeps both arms cold and keeps the only axis of
difference the number of interpreter starts.

Both arms perform the SAME logical work (importing the SAME FAN_IN_N real
predicate modules from `coordinator_core.ops`, each import a fresh cold
import in its own process); the only axis of difference is whether that
work happens across N process spawns or 1 -- mirrors `shim_decision_
rule.py`'s own "only permitted axis of difference" constraint for its
shim-vs-direct comparison.

Judged by the SAME, unchanged `shim_decision_rule.evaluate()` (p90
statistic, CHEAPER_THAN_MARGIN threshold) and writes a THIRD, distinct
record file (`shim_decision_record_fanin.json`) -- never touches
`shim_decision_record.json` or `shim_decision_record_inprocess.json`.
Whatever verdict `evaluate()` returns is written and printed as-is -- no
smoothing, no retry-until-pass, no N adjustment.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7
(corrected 2nd measurement), § Problem M-05.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from coordinator_core.benchmarks.interleave import Primitive, _time_callable, run_interleaved
from coordinator_core.benchmarks.shim_decision_rule import N_ROUNDS, ShimDecisionRecord, evaluate
from coordinator_core.benchmarks.timer import SUBPROCESS_CREATIONFLAGS, SUBPROCESS_TIMEOUT_S

_HERE = os.path.dirname(os.path.abspath(__file__))

RECORD_PATH = os.path.join(_HERE, "shim_decision_record_fanin.json")

FAN_IN_N = 8
"""Predicate count, per the plan's own M-05 example ("eight of them...
eight small predicates")."""

N_SEPARATE_PROCESSES_PRIMITIVE_NAME = "n_separate_processes__predicate_eval"
FAN_IN_ONE_PROCESS_PRIMITIVE_NAME = "fan_in_one_process__predicate_eval"

PREDICATE_MODULES = (
    "coordinator_core.ops.check_harvest_debt",
    "coordinator_core.ops.check_auto_memory_drained",
    "coordinator_core.ops.check_version_consistency",
    "coordinator_core.ops.check_import_budget_staleness",
    "coordinator_core.ops.check_posix_exec_assumptions",
    "coordinator_core.ops.check_posix_tmpdir_fallback",
    "coordinator_core.ops.check_arch_audit_staleness",
    "coordinator_core.ops.check_weekly_staleness",
)
"""The 8 real predicate modules both arms import -- the plan's own M-05
example population ("eight small predicates"). Both arms pay the SAME
import cost per predicate; only the process-spawn axis differs. Import
only (no side-effect execution) is the honest floor: importing is what
dominates a real ceremony's per-predicate cost, and running each
predicate's own side effects live is neither safe nor necessary to time
the axis in dispute (spawn vs. no spawn)."""

assert len(PREDICATE_MODULES) == FAN_IN_N, (
    "PREDICATE_MODULES must have exactly FAN_IN_N entries -- both arms "
    "must evaluate the same predicate count"
)


def _spawn_n_processes(modules: "tuple[str, ...]") -> None:
    """Spawns one bare `python -c "import <module>"` child PER predicate
    module, sequentially, within one timed draw -- reuses the same generic
    subprocess-timeout/creationflags constants `interleave._time_subprocess`
    uses, rather than re-authoring them."""
    for module in modules:
        completed = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_S,
            creationflags=SUBPROCESS_CREATIONFLAGS,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"shim_fanin_measure._spawn_n_processes: child importing "
                f"{module!r} exited {completed.returncode}: "
                f"{completed.stderr[:500]!r}"
            )


def _spawn_one_process_importing_all(modules: "tuple[str, ...]") -> None:
    """Spawns exactly ONE child process that imports every predicate
    module in `modules`, sequentially, all within that one fresh
    interpreter. See module docstring for why this is a spawned child
    (cold import) rather than an in-process `importlib.import_module`
    call against the already-warm benchmark interpreter."""
    import_stmt = "; ".join(f"import {module}" for module in modules)
    completed = subprocess.run(
        [sys.executable, "-c", import_stmt],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_S,
        creationflags=SUBPROCESS_CREATIONFLAGS,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"shim_fanin_measure._spawn_one_process_importing_all: child "
            f"exited {completed.returncode}: {completed.stderr[:500]!r}"
        )


def build_n_separate_processes_primitive(modules: "tuple[str, ...]" = PREDICATE_MODULES) -> Primitive:
    return Primitive(
        name=N_SEPARATE_PROCESSES_PRIMITIVE_NAME,
        invoke=lambda: _time_callable(lambda: _spawn_n_processes(modules)),
    )


def build_fan_in_one_process_primitive(modules: "tuple[str, ...]" = PREDICATE_MODULES) -> Primitive:
    return Primitive(
        name=FAN_IN_ONE_PROCESS_PRIMITIVE_NAME,
        invoke=lambda: _time_callable(lambda: _spawn_one_process_importing_all(modules)),
    )


def run_and_record() -> ShimDecisionRecord:
    n_processes = build_n_separate_processes_primitive()
    fan_in = build_fan_in_one_process_primitive()
    stats = run_interleaved([n_processes, fan_in], n=N_ROUNDS)
    record = evaluate(
        baseline_name=n_processes.name,
        baseline_stats=stats[n_processes.name],
        shim_name=fan_in.name,
        shim_stats=stats[fan_in.name],
    )
    # Review (2026-08-16): atomic mkstemp + os.replace, not a bare open(..., "w")
    # -- a kill mid-write must never leave this committed-artifact JSON truncated.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(RECORD_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(record.to_json())
        os.replace(tmp_path, RECORD_PATH)
        tmp_path = None
    finally:
        if tmp_path is not None:
            os.remove(tmp_path)
    return record


if __name__ == "__main__":  # pragma: no cover
    _record = run_and_record()
    print(f"baseline={_record.baseline_name} p90={_record.baseline_stat_ms:.2f}ms n={_record.baseline_sample_count}")
    print(f"shim={_record.shim_name} p90={_record.shim_stat_ms:.2f}ms n={_record.shim_sample_count}")
    print(f"reduction_fraction={_record.reduction_fraction}")
    print(f"margin={_record.margin}")
    print(f"verdict={_record.verdict}")
    print(f"record_path={RECORD_PATH}")
    sys.exit(0)
