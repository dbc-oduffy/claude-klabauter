"""coordinator_core.benchmarks.shim_inprocess_measure -- CORRECTED 2nd
measurement, stage 2, for C7's "in-process shim vs direct entry point"
comparison.

Why this module exists alongside `shim_prototype_measure.py`: that module
measured a subprocess-spawning forwarder+dispatcher shape and produced the
committed `shim_decision_record.json` (verdict: fail). That result is
correct about spawning forwarders and stays on disk exactly as committed
-- it is NOT deleted, edited, or invalidated here. But `exec_cli` (the
shipped forwarder pattern C8's own body says to follow) never spawns a
second interpreter on Windows; it runs the target IN-PROCESS via
`runpy.run_path`. This module measures THAT shape --
`shim_prototype_inprocess.run_in_process` -- against the SAME baseline
(`shim_decision_rule.build_baseline_primitive`), judged by the SAME,
unchanged `shim_decision_rule.evaluate()`, and writes a DISTINCT record
file (`shim_decision_record_inprocess.json`) -- never overwrites
`shim_decision_record.json`.

Expected verdict, per this comparison's own nature: both arms spawn
exactly ONE Python process each -- the baseline spawns
`coordinator/bin/plan-assemble.py` directly, the shim arm spawns
`shim_prototype_inprocess.py`, which itself `runpy.run_path`s that SAME
target IN-PROCESS (no child of its own). The only difference between the
two spawned processes' work is the `runpy` indirection layered on top of
identical target work, so a "wash" (shim p90 within ~10% of baseline p90,
per `shim_decision_rule`'s own margin) is the EXPECTED and desirable
result here -- a compat shim's job is to be free, not cheaper than the
thing it wraps. A "wash" verdict here reads as a PASS of the in-process
shim's actual job even though `shim_decision_rule`'s three-way vocabulary
calls that outcome "wash". Whatever `evaluate()` actually returns is
written and printed as-is -- no smoothing, no retry-until-pass, no N
inflation.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7
(corrected 2nd measurement).
"""

from __future__ import annotations

import os
import sys
import tempfile

from coordinator_core.benchmarks import declare_benchmark_origin
from coordinator_core.benchmarks.interleave import Primitive, _time_subprocess, run_interleaved
from coordinator_core.benchmarks.shim_decision_rule import (
    N_ROUNDS,
    ShimDecisionRecord,
    build_baseline_primitive,
    evaluate,
)

_HERE = os.path.dirname(os.path.abspath(__file__))

RECORD_PATH = os.path.join(_HERE, "shim_decision_record_inprocess.json")

_SHIM_PROTOTYPE_PATH = os.path.join(_HERE, "shim_prototype_inprocess.py")

INPROCESS_SHIM_PRIMITIVE_NAME = "assemble_entrypoint_via_inprocess_shim__plan"
"""Distinct from `shim_decision_rule.SHIM_PRIMITIVE_NAME` -- that name is
reserved for the forwarder-subprocess prototype `shim_prototype_measure.py`
already measured. This is a different arm (in-process, not a spawned
forwarder), so it carries its own name rather than reusing that reserved
one under a mismatched shape."""


def build_inprocess_shim_primitive(repo_root: "str | None" = None) -> Primitive:
    """Builds the in-process shim arm's `interleave.Primitive`: spawns ONE
    `python shim_prototype_inprocess.py` process -- that process then
    `runpy.run_path`s the baseline target IN-PROCESS (no child of its
    own). Mirrors `build_baseline_primitive`'s own single-process-spawn
    shape via the same generic `interleave._time_subprocess` timer, so
    both arms pay exactly one interpreter start and the only remaining
    difference is the `runpy` indirection. See module docstring."""
    argv = [sys.executable, _SHIM_PROTOTYPE_PATH]
    return Primitive(
        name=INPROCESS_SHIM_PRIMITIVE_NAME,
        invoke=lambda: _time_subprocess(argv, cwd=repo_root),
    )


def run_and_record() -> ShimDecisionRecord:
    from pathlib import Path

    from coordinator_core.ops._git_root_util import git_root_zero_spawn

    repo_root = git_root_zero_spawn(Path(__file__))
    baseline = build_baseline_primitive(repo_root)
    shim = build_inprocess_shim_primitive(repo_root)
    stats = run_interleaved([baseline, shim], n=N_ROUNDS)
    record = evaluate(
        baseline_name=baseline.name,
        baseline_stats=stats[baseline.name],
        shim_name=shim.name,
        shim_stats=stats[shim.name],
    )
    # Review (2026-08-16): atomic mkstemp + os.replace, not a bare open(..., "w")
    # -- a kill mid-write must never leave this committed-artifact JSON truncated.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(RECORD_PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(record.to_json())
        os.replace(tmp_path, RECORD_PATH)
        tmp_path = None
    finally:
        if tmp_path is not None:
            os.remove(tmp_path)
    return record


if __name__ == "__main__":  # pragma: no cover
    declare_benchmark_origin()
    _record = run_and_record()
    print(f"baseline={_record.baseline_name} p90={_record.baseline_stat_ms:.2f}ms n={_record.baseline_sample_count}")
    print(f"shim={_record.shim_name} p90={_record.shim_stat_ms:.2f}ms n={_record.shim_sample_count}")
    print(f"reduction_fraction={_record.reduction_fraction}")
    print(f"margin={_record.margin}")
    print(f"verdict={_record.verdict}")
    print(f"record_path={RECORD_PATH}")
    sys.exit(0)
