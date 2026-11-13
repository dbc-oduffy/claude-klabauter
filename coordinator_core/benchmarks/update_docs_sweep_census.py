"""Census of a default `/update-docs` sweep: process time, spawns, agent phases, archived-handoff coverage.

Calls `ceremony.update_docs_worklist` in-process at default params (one discarded warm-up, then N
measured runs) through the reentry seam and reports, from the last run: `process_ms` and `spawns`
(max over runs; `time.process_time` and the shared Popen-patching counter, never wall clock),
`agent_phases` (phase_verdicts entries equal to "agent"), `agent_worklist_items`, and
`archived_handoff_coverage` (classified archived handoffs / `archive/handoffs/**/*.md` on disk).

An op that predates `phase_verdicts` or the `archived_handoffs` cohort reads as `agent_phases: 0`,
`phase_verdicts: "absent"`, coverage `"absent"`; the census never fails on that. `token_baseline` is
the recorded EM measurement of the Sonnet phase-1-11d agent (not measured here); a recorded
transcript's token axis is `ceremony_token_census.py`.

Run: `python -m coordinator_core.benchmarks.update_docs_sweep_census [--runs N] [--repo PATH]`.
Exit 1 when max process_ms >= BAR_MS.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

BAR_MS = 500.0
DEFAULT_RUNS = 3
OP = "ceremony.update_docs_worklist"
ARCHIVED_HANDOFFS_KEY = "archived_handoffs"
ABSENT = "absent"
STATES = ("prune", "keep", "indeterminate")
TOKEN_BASELINE = {
    "label": "R2 EM-measured: Sonnet agent, /update-docs phases 1-11d, one dispatch (recorded, not re-measured)",
    "tokens": 148000,
    "agent_dispatches": 1,
}


def archived_handoff_total(repo: Path) -> int:
    """Count of `*.md` files under `<repo>/archive/handoffs` (0 when the directory is absent)."""
    base = repo / "archive" / "handoffs"
    return sum(
        1
        for _dir, _subdirs, names in os.walk(base)
        for name in names
        if name.endswith(".md")
    )


def agent_phase_count(result: dict) -> int:
    """Phase verdicts equal to "agent"; 0 when the op emits no `phase_verdicts`."""
    verdicts = result.get("phase_verdicts")
    if not isinstance(verdicts, dict):
        return 0
    return sum(1 for v in verdicts.values() if (v.get("verdict") if isinstance(v, dict) else v) == "agent")


def archived_handoff_coverage(result: dict, total: int) -> Any:
    """`{classified, total, ratio}` from the cohort's uncapped per-state counts, or "absent".

    Counts are read from the cohort's `counts` (else `totals`, else the cohort itself) keyed by
    prune/keep/indeterminate; a cohort without them reads as classified 0.
    """
    cohort = ((result.get("decided_by_op") or {}).get("prune") or {}).get(ARCHIVED_HANDOFFS_KEY)
    if cohort is None:
        return ABSENT
    counts: Any = cohort
    if isinstance(cohort, dict):
        counts = cohort.get("counts") or cohort.get("totals") or cohort
    classified = 0
    if isinstance(counts, dict):
        classified = sum(int(counts.get(s, 0)) for s in STATES if isinstance(counts.get(s, 0), int))
    return {"classified": classified, "total": total, "ratio": round(classified / total, 4) if total else 1.0}


def _count_spawns(call: Callable[[], Any]) -> tuple[Any, int]:
    import pytest

    from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

    mp = pytest.MonkeyPatch()
    with _count_spawns_attributed(mp) as spawns:
        result = call()
    return result, len(spawns)


def measure(call: Callable[[], dict], runs: int = DEFAULT_RUNS) -> dict[str, Any]:
    """One discarded warm-up, then `runs` measured calls; return maxima plus the last result."""
    call()
    rows = []
    result: dict = {}
    for _ in range(runs):
        start = time.process_time()
        result, spawns = _count_spawns(call)
        rows.append({"process_ms": round((time.process_time() - start) * 1000.0, 2), "spawns": spawns})
    return {
        "runs": rows,
        "process_ms": max(r["process_ms"] for r in rows),
        "spawns": max(r["spawns"] for r in rows),
        "_result": result,
    }


def run(repo: Path, runs: int = DEFAULT_RUNS, call: Optional[Callable[[], dict]] = None) -> dict[str, Any]:
    """Measure the worklist op over `repo`; `call` replaces the op invocation (test seam)."""
    if call is None:
        from coordinator_core.lifecycle import git_common_dir
        from coordinator_core.warm.entry_seam import reentrant_dispatch

        common = git_common_dir(repo)
        # Trap: invoke through the reentry seam, never a raw handler (warm/tests/test_reentry_goes_through_the_seam.py).
        call = lambda: reentrant_dispatch(OP, {}, repo_root=common)  # noqa: E731
    m = measure(call, runs)
    result = m.pop("_result")
    verdicts = result.get("phase_verdicts")
    return {
        "op": OP,
        "bar_ms": BAR_MS,
        "measured_runs": runs,
        **m,
        "phase_verdicts": verdicts if isinstance(verdicts, dict) else ABSENT,
        "agent_phases": agent_phase_count(result),
        "agent_worklist_items": len(result.get("agent_worklist") or []),
        "archived_handoff_coverage": archived_handoff_coverage(result, archived_handoff_total(repo)),
        "token_baseline": TOKEN_BASELINE,
        "pass": m["process_ms"] < BAR_MS,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    report = run(args.repo.resolve(), args.runs)
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
