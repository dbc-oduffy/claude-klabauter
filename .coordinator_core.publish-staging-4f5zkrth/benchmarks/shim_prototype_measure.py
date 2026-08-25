"""coordinator_core.benchmarks.shim_prototype_measure -- STAGE 2 of C7.
Runs the actual `interleave.run_interleaved` measurement the C7 decision
rule (`shim_decision_rule.py`) was written to be judged against, and
emits/commits the resulting `ShimDecisionRecord`.

This module performs the live measurement `shim_decision_rule.py`
explicitly declines to (see that module's docstring, 'Shim arm': "DOES
NOT EXIST YET and is NOT measured in this stage" -- that was stage 1;
this is stage 2). It measures the THROWAWAY prototype forwarder+
dispatcher pair (`shim_prototype_forwarder.py` / `shim_prototype_
dispatcher.py`), not any C8 production shim -- C8 has not landed. See
those two modules' docstrings for exactly what is and is not shared with
whatever C8 eventually builds.

Baseline arm: `shim_decision_rule.build_baseline_primitive()` (a bare
spawn of `coordinator/bin/plan-assemble.py`, no subcommand token).

Shim arm: a bare spawn of `shim_prototype_forwarder.py`, which itself
spawns `shim_prototype_dispatcher.py` as a child process; the dispatcher
performs the IDENTICAL target work the baseline arm performs (resolve
MAKIMA_ROOT, import `coordinator_core.plan_assemble`, call `main([])`).
The only axis of difference between the two arms is process-spawn shape
(one spawn vs. a forwarder spawning a dispatcher spawn) -- both arms
reach the same underlying `plan-assemble` behaviour, per
`shim_decision_rule.py`'s own constraint on the shim arm.

Running this module (`python -m coordinator_core.benchmarks.
shim_prototype_measure`) performs the N_ROUNDS-round interleaved
measurement, calls `shim_decision_rule.evaluate()` on the resulting
stats (never computes a verdict itself), writes the resulting
`ShimDecisionRecord` to `shim_decision_record.json` alongside this
module, and prints a human-readable summary. Whatever verdict comes back
-- pass, wash, or fail -- is recorded as-is; this module applies no
smoothing, retry-until-pass, or N inflation. See `shim_decision_rule.py`
module docstring 'Wash handling' for why wash and fail are both
non-passing stop conditions.

Spec backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C7.
"""

from __future__ import annotations

import os
import sys
import tempfile

from coordinator_core.benchmarks.interleave import Primitive, _time_subprocess, run_interleaved
from coordinator_core.benchmarks.shim_decision_rule import (
    N_ROUNDS,
    SHIM_PRIMITIVE_NAME,
    ShimDecisionRecord,
    build_baseline_primitive,
    evaluate,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
_FORWARDER_PATH = os.path.join(_HERE, "shim_prototype_forwarder.py")

RECORD_PATH = os.path.join(_HERE, "shim_decision_record.json")


def build_shim_primitive() -> Primitive:
    """Builds the shim arm's `interleave.Primitive`: a bare spawn of the
    throwaway prototype forwarder (`shim_prototype_forwarder.py`), which
    itself spawns the throwaway prototype dispatcher. See module
    docstring 'Shim arm'."""
    return Primitive(
        name=SHIM_PRIMITIVE_NAME,
        invoke=lambda: _time_subprocess([sys.executable, _FORWARDER_PATH]),
    )


def run_and_record() -> ShimDecisionRecord:
    baseline = build_baseline_primitive()
    shim = build_shim_primitive()
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
    _record = run_and_record()
    print(f"baseline={_record.baseline_name} p90={_record.baseline_stat_ms:.2f}ms n={_record.baseline_sample_count}")
    print(f"shim={_record.shim_name} p90={_record.shim_stat_ms:.2f}ms n={_record.shim_sample_count}")
    print(f"reduction_fraction={_record.reduction_fraction}")
    print(f"margin={_record.margin}")
    print(f"verdict={_record.verdict}")
    print(f"record_path={RECORD_PATH}")
    sys.exit(0)
