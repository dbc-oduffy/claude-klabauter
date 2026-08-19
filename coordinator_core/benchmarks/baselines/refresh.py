"""
coordinator_core.benchmarks.baselines.refresh -- the named, runnable
refresh action for the tracked baseline partition (qsub-01 C8).

Purpose: `harness.py::run()` (C9) is the single stamping site for `machine`
(C2) and the ambient-context trio (`ambient_before`/`ambient_after`/
`ambient_delta`, also C2) -- every `ConformanceRecord` it returns already
carries them, sampled once per run via `ambient_sampler.take_sample()`
(C2's in-process contract -- never raises, degrades to null). This module
no longer post-processes `harness.run()`'s output to add those fields; it
only adds `baseline_id`, a store-entry key that is a caller concern (see
`__main__.py::_stamp_baseline_id`, which does the same thing for the
non-refresh CLI path), via `dataclasses.replace` (records are frozen)
before writing anything to disk.

What this module does: drives `harness.run()` directly (the same
entrypoint `__main__.py` drives), stamps each resulting record with
`baseline_id`, appends every stamped record to this box's run-history
partition (`baseline_store.append`, same target `__main__.py` uses), and
writes the latest record per op wholesale to the curated, tracked
partition (`baseline_store.write_tracked_baseline`) -- this module's own
two-step mirrors `__main__.py`'s append path plus the deliberate refresh
action `baseline_store.py`'s module docstring names as C8's job.

`coverage.gate` is excluded from the default op set: it is not currently a
registered JSON-RPC method (`Method not found: 'coverage.gate'`, verified
on disk against `coordinator_core/ops/_registry_map.py`), a pre-existing
gap in the op registry unrelated to this plan and out of both this
module's and `op_fixtures.py`'s file scope -- flagged, not fixed here.

Usage (the documented refresh command; this IS the refresh trigger the
module-level Refresh discipline section of `baseline_store.py` names,
made concrete):

    python -m coordinator_core.benchmarks.baselines.refresh

Cadence / re-run condition (C3's refresh-discipline paragraph, made
concrete): re-run this command whenever the tracked partition's
`ambient_before`/`ambient_after` band no longer represents this box's
typical load (a sustained shift in `live_sessions`/`claude_procs`/`cpu_pct`
away from the band the current tracked entries were measured under -- see
`ambient_sampler.py`'s field shapes), OR whenever `code_sha` drift makes
the tracked entries' `code_sha` field stale relative to a change that could
plausibly move an op's latency (a `coordinator_core` change touching the
IPC dispatch path, an op's own implementation, or the invoke transport).
A bare age cap is deliberately NOT the trigger -- see `baseline_store.py`'s
Refresh discipline section for why (an age-only check answers "is this
recent", never "was it measured at a representative load"). There is no
automatic scheduler for this command (repo-wide grep of every non-test
`baseline_store` reference found none, per C8's own review-note record);
running it is a deliberate, human/CI-triggered action, per the module
docstring above.

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C8.
"""

from __future__ import annotations

import dataclasses
import sys
from typing import List, Optional

from coordinator_core.benchmarks import baseline_store
from coordinator_core.benchmarks import harness
from coordinator_core.benchmarks import op_fixtures
from coordinator_core.benchmarks.record import ConformanceRecord, compose_machine_id

#: Not a registered JSON-RPC op today (see module docstring) -- excluded
#: from the default refresh op set until the registry gap is fixed
#: elsewhere. Named here, not silently dropped, so a future run stops
#: excluding it the moment it becomes real.
_UNREGISTERED_OPS = frozenset({"coverage.gate"})


def _default_refresh_ops() -> List[str]:
    """The full COMPUTE_ONLY fixture set minus `_UNREGISTERED_OPS`."""
    return [
        op
        for op in op_fixtures.COMPUTE_ONLY_FIXTURES.keys()
        if op not in _UNREGISTERED_OPS
    ]


def _stamp(record: ConformanceRecord) -> ConformanceRecord:
    """Return a copy of `record` carrying `baseline_id` -- `harness.run()`
    (C9) already stamps `machine`/`ambient_before`/`ambient_after`/
    `ambient_delta`; this is the one field that stays a caller concern
    (see module docstring). Mirrors `__main__.py::_stamp_baseline_id`'s
    `<code_sha>:<op>:<run_id>` scheme."""
    baseline_id = f"{record.code_sha}:{record.op}:{record.run_id}"
    return dataclasses.replace(record, baseline_id=baseline_id)


def refresh(
    ops: Optional[List[str]] = None,
    n: int = 40,
) -> List[ConformanceRecord]:
    """Run the benchmark sweep (which stamps machine + ambient context onto
    every record itself, see `harness.run()`), stamp `baseline_id`, append
    each to this box's run-history partition, and overwrite the curated
    tracked partition with the latest record per op.

    Returns the list of stamped records written (append order == sweep
    order, one entry per op in `ops`).
    """
    target_ops = ops if ops is not None else _default_refresh_ops()
    machine = compose_machine_id()

    records = harness.run(ops=target_ops, n=n)

    stamped = [_stamp(r) for r in records]

    for record in stamped:
        baseline_store.append(record)

    baseline_store.write_tracked_baseline(stamped, machine=machine)

    return stamped


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint: `python -m coordinator_core.benchmarks.baselines.refresh`."""
    stamped = refresh()
    print(f"refreshed {len(stamped)} op(s) for machine={compose_machine_id()!r}")
    for record in sorted(stamped, key=lambda r: r.op):
        print(f"  {record.op}: verdict={record.verdict} min_ms={record.min:.2f}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
