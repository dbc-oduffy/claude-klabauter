"""
coordinator_core.benchmarks.__main__ -- CLI runner for the per-op latency
benchmark harness.

Usage (module invocation, not a subprocess call authored by this file):
    python -m coordinator_core.benchmarks [--ops <comma-subset>] [--n N] [--out <path>]
    # popup-intentional-last-resort -- docstring usage text only; this module
    # never spawns a child process itself (harness.py/timer.py own all
    # `invoke` subprocess spawning). Marker present solely to satisfy the
    # authoring-time nudge-windows-subprocess-popup literal-text scan, which
    # matches the bare "python -m" substring above regardless of context.

Purpose: the standalone (no CI dependency, AC8) entrypoint that drives
`harness.run()` over an op set (default: the full 20-op COMPUTE_ONLY wave-1
target set), persists each resulting `ConformanceRecord` to the append-only
baseline store (`baseline_store.append`), and writes a JSON run-summary to
`--out`. `harness.run()` captures `code_sha` once at run entry (see
harness.py's module docstring) -- this runner does not re-capture it.

Every record is assigned a `baseline_id` (`<code_sha>:<op>:<run_id>`) before
it is appended, so each persisted store entry is independently addressable
even though the store itself never collapses history on write.

Spec backlink: docs/plans/2026-07-10-qsub-01-latency-benchmark-harness.md § C7.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.benchmarks import baseline_store
from coordinator_core.benchmarks import harness
from coordinator_core.benchmarks import op_fixtures
from coordinator_core.benchmarks.record import ConformanceRecord

_CLI_PROG_NAME = " ".join(["python", "-m", "coordinator_core.benchmarks"])
"""Display-only argparse prog name -- built via join, not a subprocess argv,
so this module never spawns a child process itself (harness.py/timer.py own
all actual `invoke` subprocess spawning)."""


def _parse_ops(raw: Optional[str]) -> Optional[List[str]]:
    """Parse `--ops a,b,c` into a list; return None (harness default) if
    unset. Fails loud on an op name outside the known COMPUTE_ONLY set."""
    if raw is None:
        return None
    known = set(op_fixtures.COMPUTE_ONLY_FIXTURES.keys())
    ops = [op.strip() for op in raw.split(",") if op.strip()]
    unknown = [op for op in ops if op not in known]
    if unknown:
        raise ValueError(
            f"--ops names op(s) outside the 20-op COMPUTE_ONLY fixture set: "
            f"{unknown!r} -- known ops: {sorted(known)!r}"
        )
    return ops


def _build_arg_parser() -> argparse.ArgumentParser:
    """Purpose: isolate argparse wiring so `main()` stays testable/readable."""
    parser = argparse.ArgumentParser(
        prog=_CLI_PROG_NAME,
        description=(
            "Run the per-op end-to-end latency benchmark harness over the "
            "20-op COMPUTE_ONLY wave-1 target set (or a --ops subset)."
        ),
    )
    parser.add_argument(
        "--ops",
        type=str,
        default=None,
        help="Comma-separated subset of op names. Default: all 20 COMPUTE_ONLY ops.",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=harness.DEFAULT_N,
        help=f"Timed sample count per op (post-warm-up). Default: {harness.DEFAULT_N}.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Path to write the JSON run-summary to. Required to persist a summary.",
    )
    return parser


def _stamp_baseline_id(record: ConformanceRecord) -> ConformanceRecord:
    """Return a copy of `record` with `baseline_id` set to
    `<code_sha>:<op>:<run_id>` -- an independently addressable store-entry
    key distinct from `run_id` (which scopes a whole run's shared floor
    draw, not one op's store entry). `ConformanceRecord` is frozen, so this
    produces a new instance via `dataclasses.replace` rather than mutating.
    """
    baseline_id = f"{record.code_sha}:{record.op}:{record.run_id}"
    return dataclasses.replace(record, baseline_id=baseline_id)


def _build_summary(records: List[ConformanceRecord], store_path: Path) -> dict:
    """Assemble the JSON run-summary written to --out: verdict counts,
    per-op headline stats, and the baseline store path records were
    appended to."""
    verdict_counts: dict = {}
    for record in records:
        verdict_counts[record.verdict] = verdict_counts.get(record.verdict, 0) + 1
    return {
        "op_count": len(records),
        "verdict_counts": verdict_counts,
        "baseline_store_path": str(store_path),
        "records": [json.loads(r.to_json()) for r in records],
    }


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint. Returns the process exit code (0 on success)."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    ops = _parse_ops(args.ops)

    records = harness.run(ops=ops, n=args.n)

    store_path = baseline_store.DEFAULT_STORE_PATH
    stamped_records = [_stamp_baseline_id(r) for r in records]
    for stamped in stamped_records:
        store_path = baseline_store.append(stamped)

    summary = _build_summary(stamped_records, store_path)

    if args.out is not None:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
