"""Census of a default `/update-docs` sweep: process time, spawns, agent phases, archived-handoff coverage.

Calls each bookkeeping op (`OPS`: the worklist, `docindex.emit`, and the composed
`ceremony.update_docs_bookkeeping`) in-process at default params (one discarded warm-up, then N
measured runs) through the reentry seam. Per op, `ops[<op>]` carries `process_ms` and `spawns`
(max over runs; `time.process_time` and the shared Popen-patching counter, never wall clock), or
`status: "absent"` when the op is not registered. Top-level `process_ms`/`spawns` are the maxima
over measured ops; `pass` needs every op measured (an absent op fails, listed in `absent_ops`) and
under `BAR_MS`. `reply_tokens` counts the composite's reply once (it embeds the other two). From the worklist's last run:
`agent_phases` (phase_verdicts entries equal to "agent"), `agent_worklist_items`, and
`archived_handoff_coverage` (classified archived handoffs / `archive/handoffs/**/*.md` on disk).

`token_axis` is the ritual's token cost: `before` is the recorded baseline, `after` is the
`ceremony_token_census` totals of `--transcript` (an /update-docs session run after the ops landed),
else "unmeasured" with the live `agent_phases`.

An op that predates `phase_verdicts` or the `archived_handoffs` cohort reads as `agent_phases: 0`,
`phase_verdicts: "absent"`, coverage `"absent"`; the census never fails on that. `token_baseline` is
the recorded EM measurement of the Sonnet phase-1-11d agent (not measured here); a recorded
transcript's token axis is `ceremony_token_census.py`.

Run: `python -m coordinator_core.benchmarks.update_docs_sweep_census [--runs N] [--repo PATH]
[--transcript JSONL [--subagents-dir DIR]]`.
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
INDEX_OP = "docindex.emit"
BOOKKEEPING_OP = "ceremony.update_docs_bookkeeping"
OPS = (OP, INDEX_OP, BOOKKEEPING_OP)
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
    """`{classified, total, ratio}` from `decided_by_op.archived_handoffs` (the op's uncapped
    prune/keep/indeterminate counts), or "absent" when the op emits no such block."""
    counts = (result.get("decided_by_op") or {}).get(ARCHIVED_HANDOFFS_KEY)
    if not isinstance(counts, dict):
        return ABSENT
    classified = sum(v for s in STATES if isinstance(v := counts.get(s, 0), int))
    return {"classified": classified, "total": total, "ratio": round(classified / total, 4) if total else 1.0}


def reply_tokens(result: dict) -> int:
    """Estimated tokens an agent reads for one op reply (compact JSON bytes / 4)."""
    return len(json.dumps(result, separators=(",", ":"))) // 4


def total_reply_tokens(ops: dict[str, Any]) -> int:
    """Reply tokens an agent reads for one sweep. The composite's reply embeds the worklist's and
    `docindex.emit`'s, so when it is measured it alone is counted; otherwise the base ops sum."""
    def tokens(op: str) -> int:
        return ops.get(op, {}).get("reply_tokens", 0)

    if ops.get(BOOKKEEPING_OP, {}).get("status") == "measured":
        return tokens(BOOKKEEPING_OP)
    return tokens(OP) + tokens(INDEX_OP)


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


def _op_calls(repo: Path) -> dict[str, Callable[[], dict]]:
    from coordinator_core.lifecycle import git_common_dir
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    common = git_common_dir(repo)
    params = {OP: {}, INDEX_OP: {"target_root": str(repo)}, BOOKKEEPING_OP: {}}
    # Trap: invoke through the reentry seam, never a raw handler (warm/tests/test_reentry_goes_through_the_seam.py).
    return {op: (lambda op=op: reentrant_dispatch(op, params[op], repo_root=common)) for op in OPS}


def token_axis(agent_phases: int, transcript: Optional[Path], subagents_dir: Optional[Path]) -> dict[str, Any]:
    """The ritual's token cost: recorded `before`, and `after` from a transcript's census totals
    (`ceremony_token_census`) or "unmeasured" with the live agent-phase count."""
    after: Any = {"tokens": "unmeasured", "agent_phases": agent_phases}
    if transcript is not None:
        from coordinator_core.benchmarks.ceremony_token_census import census

        totals = census(transcript, subagents_dir, "update-docs")["totals"]
        after = {"transcript": str(transcript), **totals, "tokens": totals["input_tokens"] + totals["output_tokens"]}
    return {"before": TOKEN_BASELINE, "after": after}


def run(
    repo: Path,
    runs: int = DEFAULT_RUNS,
    call: Optional[Callable[[], dict]] = None,
    calls: Optional[dict[str, Callable[[], dict]]] = None,
    transcript: Optional[Path] = None,
    subagents_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Measure each bookkeeping op over `repo`. `calls` ({op: callable}) replaces the op
    invocations and `call` replaces the worklist's alone (test seams); an op raising
    `OpUnavailableError` reads `status: "absent"` and is not measured."""
    from coordinator_core.warm.entry_seam import OpUnavailableError

    if calls is None:
        calls = {OP: call} if call is not None else _op_calls(repo)
    ops: dict[str, Any] = {}
    results: dict[str, dict] = {}
    for op, fn in calls.items():
        try:
            m = measure(fn, runs)
        except OpUnavailableError:
            ops[op] = {"op": op, "status": ABSENT}
            continue
        results[op] = m.pop("_result")
        ops[op] = {"op": op, "status": "measured", "reply_tokens": reply_tokens(results[op]), **m}
    measured = [v for v in ops.values() if v["status"] == "measured"]
    absent = sorted(op for op, v in ops.items() if v["status"] == ABSENT)
    result = results.get(OP, {})
    verdicts = result.get("phase_verdicts")
    agent_phases = agent_phase_count(result)
    process_ms = max((v["process_ms"] for v in measured), default=0.0)
    return {
        "op": OP,
        "bar_ms": BAR_MS,
        "measured_runs": runs,
        "ops": ops,
        "absent_ops": absent,
        "reply_tokens": total_reply_tokens(ops),
        "process_ms": process_ms,
        "spawns": max((v["spawns"] for v in measured), default=0),
        "phase_verdicts": verdicts if isinstance(verdicts, dict) else ABSENT,
        "agent_phases": agent_phases,
        "agent_worklist_items": len(result.get("agent_worklist") or []),
        "archived_handoff_coverage": archived_handoff_coverage(result, archived_handoff_total(repo)),
        "token_baseline": TOKEN_BASELINE,
        "token_axis": token_axis(agent_phases, transcript, subagents_dir),
        "pass": not absent and process_ms < BAR_MS,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--transcript", type=Path, default=None, help="post-landing /update-docs session JSONL (token axis `after`)")
    parser.add_argument("--subagents-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    report = run(args.repo.resolve(), args.runs, transcript=args.transcript, subagents_dir=args.subagents_dir)
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
