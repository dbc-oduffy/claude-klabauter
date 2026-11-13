"""Process-time falsifier for the update-docs scan op and `distill.scope`, on both example-retrieval-repo axes.

Each op runs warm in-process at default parameters against this repo: one discarded warm-up call,
then N measured runs (default 3). Per run: `time.process_time` ms (never wall clock), process spawns
counted through the shared `spawn_counter` Popen patch, output bytes (`len(json.dumps(result))`) and
worklist length. The rag-off axis makes the structure client's POST raise `StructureUnavailable`;
the rag-on axis leaves it real and reports `rag_calls` / `rag_answered` so a daemon that was not
there reads as such. Never starts a daemon.

The update-docs op resolves to `ceremony.update_docs_worklist` when registered, else
`ceremony.update_docs_scan`; the resolved name is in the JSON. Exit 1 when any max process_ms is
>= BAR_MS. `estimate_before_tokens` is an ESTIMATE (`estimate: true`), derived from the current ops'
result bytes and `project_run_cost` fan-out; no live before-run.

distill.scope writes its run files under `state/scratch/artifact-distillation/<RUN_ID>/`
(gitignored); this script removes that directory when it finishes.

Run: `python -m coordinator_core.benchmarks.update_docs_distill_process_time [--runs N] [--repo PATH]`.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

BAR_MS = 500.0
DEFAULT_RUNS = 3
RUN_ID = "bench-update-docs-distill"
BYTES_PER_TOKEN = 4
UPDATE_DOCS_OPS = ("ceremony.update_docs_worklist", "ceremony.update_docs_scan")
DISTILL_OP = "distill.scope"
AXES = ("rag_on", "rag_off")


def resolve_update_docs_op(lookup: Optional[Callable[[str], Any]] = None) -> tuple[str, Callable]:
    """The first of UPDATE_DOCS_OPS the registry resolves, as (name, handler). Raises when none does."""
    if lookup is None:
        from coordinator_core.ipc import get_op_handler as lookup
    for name in UPDATE_DOCS_OPS:
        handler = lookup(name)
        if handler is not None:
            return name, handler
    raise RuntimeError(f"none of {UPDATE_DOCS_OPS} is registered")


@contextlib.contextmanager
def rag_axis(axis: str) -> Iterator[dict[str, Any]]:
    """Patch the structure client's POST for `axis`; yield a stats dict {calls, answered}.

    rag_off: the POST raises StructureUnavailable. rag_on: the real POST, counted; a call that
    raises leaves `answered` False and re-raises. Patches every loaded module holding `_default_post`.
    """
    if axis not in AXES:
        raise ValueError(f"axis must be one of {AXES}; got {axis!r}")
    from coordinator_core.architecture_survey import structure

    stats: dict[str, Any] = {"calls": 0, "answered": False}
    real = structure._default_post

    def wrapped(url: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        stats["calls"] += 1
        if axis == "rag_off":
            raise structure.StructureUnavailable("rag forced unavailable by the benchmark")
        out = real(url, payload, timeout_s)
        stats["answered"] = True
        return out

    holders = [m for m in list(sys.modules.values())
               if getattr(m, "__name__", "").startswith("coordinator_core") and getattr(m, "_default_post", None) is real]
    for mod in holders:
        mod._default_post = wrapped
    try:
        yield stats
    finally:
        for mod in holders:
            mod._default_post = real


def _count_spawns(call: Callable[[], Any]) -> tuple[Any, int]:
    """Run `call` under the shared Popen-patching spawn counter; return (result, spawn count)."""
    import pytest

    from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

    mp = pytest.MonkeyPatch()
    with _count_spawns_attributed(mp) as spawns:
        result = call()
    return result, len(spawns)


def _one_run(call: Callable[[], dict], worklist_len: Callable[[dict], int]) -> dict[str, Any]:
    start = time.process_time()
    result, spawns = _count_spawns(call)
    elapsed_ms = (time.process_time() - start) * 1000.0
    return {
        "process_ms": round(elapsed_ms, 2),
        "spawns": spawns,
        "output_bytes": len(json.dumps(result)),
        "worklist_len": worklist_len(result),
        "_result": result,
    }


def measure_op(
    op_name: str,
    call: Callable[[], dict],
    worklist_len: Callable[[dict], int],
    axis: str,
    runs: int = DEFAULT_RUNS,
) -> dict[str, Any]:
    """One op on one rag axis: a discarded warm-up, then `runs` measured calls. Entry the pin test drives."""
    with rag_axis(axis) as stats:
        call()
        measured = [_one_run(call, worklist_len) for _ in range(runs)]
    last = measured[-1].pop("_result")
    for row in measured[:-1]:
        row.pop("_result")
    return {
        "op_name": op_name,
        "rag": axis,
        "runs": measured,
        "process_ms": max(r["process_ms"] for r in measured),
        "spawns": max(r["spawns"] for r in measured),
        "output_bytes": max(r["output_bytes"] for r in measured),
        "worklist_len": measured[-1]["worklist_len"],
        "rag_calls": stats["calls"],
        "rag_answered": stats["answered"],
        "_last_result": last,
    }


def update_docs_worklist_len(result: dict) -> int:
    """Rows the EM must still judge: a `worklist` list when the op has one, else the prune rows."""
    worklist = result.get("worklist")
    if isinstance(worklist, list):
        return len(worklist)
    return len(result.get("phase8b_prune") or [])


def distill_worklist_len(result: dict) -> int:
    """Files handed to scan agents: counts.scannable, else counts.batches."""
    counts = result.get("counts") or {}
    return int(counts.get("scannable", counts.get("batches", 0)))


def estimate_before_tokens(update_docs_result: dict, distill_result: dict, repo: Path) -> dict[str, Any]:
    """ESTIMATE of the agent-read tokens the current ops cause; never a live before-run.

    update-docs: the scan result read once (bytes/4), a lower bound since the update-docs agent
    fan-out is not recorded in-process. distill: `project_run_cost` over the Workflow input the
    op wrote (load + scan input bytes /4) and its recorded dispatch count.
    """
    est: dict[str, Any] = {
        "estimate": True,
        "basis": "bytes/4 over the ops' current result and project_run_cost fan-out; not a measured spend",
        "update_docs": {
            "estimate": True,
            "tokens": len(json.dumps(update_docs_result)) // BYTES_PER_TOKEN,
            "lower_bound": True,
        },
    }
    try:
        from coordinator_core.benchmarks.distill_run_cost import load_workflow_input
        from coordinator_core.distill.run_cost import project_run_cost

        payload = load_workflow_input(repo, distill_result["workflow_input_path"])
        cost = project_run_cost(payload, repo)
        total = sum(cost["input_bytes_by_phase"].values())
        est["distill"] = {
            "estimate": True,
            "tokens": total // BYTES_PER_TOKEN,
            "dispatches": sum(cost["dispatches_by_phase"].values()),
        }
    except Exception as exc:  # noqa: BLE001 - the estimate is advisory; the timing verdict must still print
        est["distill"] = {"estimate": True, "unavailable": f"{type(exc).__name__}: {exc}"}
    return est


def verdict(rows: list[dict[str, Any]], bar_ms: float = BAR_MS) -> bool:
    """True when every row's max process_ms is under `bar_ms`."""
    return all(r["process_ms"] < bar_ms for r in rows)


def run(repo: Path, runs: int = DEFAULT_RUNS) -> dict[str, Any]:
    """Measure both ops on both rag axes over `repo`; return the JSON-ready report."""
    from coordinator_core.lifecycle import git_common_dir

    common = git_common_dir(repo)
    ud_name, ud_handler = resolve_update_docs_op()
    from coordinator_core.ipc import get_op_handler

    distill_handler = get_op_handler(DISTILL_OP)
    if distill_handler is None:
        raise RuntimeError(f"{DISTILL_OP} is not registered")
    rows: list[dict[str, Any]] = []
    last: dict[str, dict] = {}
    run_dir = repo / "state" / "scratch" / "artifact-distillation" / RUN_ID
    try:
        for axis in AXES:
            row = measure_op(ud_name, lambda: ud_handler({}, repo_root=common), update_docs_worklist_len, axis, runs)
            last["update_docs"] = row.pop("_last_result")
            rows.append(row)
            row = measure_op(
                DISTILL_OP, lambda: distill_handler({"run_id": RUN_ID}, repo_root=common),
                distill_worklist_len, axis, runs)
            last["distill"] = row.pop("_last_result")
            rows.append(row)
        estimate = estimate_before_tokens(last["update_docs"], last["distill"], repo)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return {
        "bar_ms": BAR_MS,
        "runs": runs,
        "update_docs_op": ud_name,
        "ops": rows,
        "estimate_before_tokens": estimate,
        "pass": verdict(rows),
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
