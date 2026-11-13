"""Code-intel build cost: process time, spawns and tokens of the ``code_intel.*`` ops.

Prints one JSON line per op (``code_intel.freshness``, ``code_intel.workstate_index``); an op whose
body module is not built yet prints ``{"op": ..., "status": "not_built"}``. ``--pin-baseline``
measures the full-tree re-extract reference at ``--baseline-ref`` and writes
``state/measurements/code-intel-build-cost-baseline.json``; without it the file is never touched.
Exit 0 always: this measures, it does not gate.

Usage: python coordinator_core/benchmarks/code_intel_build_cost.py --repo . --sha HEAD [--pin-baseline]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.symbol_knowledge import build_cost, contract

BASELINE_REF = "3310265ec61003563bdd1dc048ffa32f58f8dcaf"
BASELINE_FILE = Path("state") / "measurements" / "code-intel-build-cost-baseline.json"
BRIGHTLINE_MS = 500


def _count(result: Any) -> int:
    value = result.get("files_reextracted", 0) if isinstance(result, dict) else 0
    return len(value) if isinstance(value, (list, tuple)) else int(value or 0)


def _tokens(result: Any, key: str) -> int:
    return int(result.get(key, 0) or 0) if isinstance(result, dict) else 0


def measure_op(op: str, root: Path, sha: str) -> Dict[str, Any]:
    """One op's ``BuildCost`` row as a dict, or a ``not_built`` row when its body is absent."""
    call = {
        "code_intel.freshness": lambda: contract.freshness(root, sha),
        "code_intel.workstate_index": lambda: contract.workstate_index(root),
    }[op]
    try:
        result, cost = build_cost.measure(op, call)
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("coordinator_core.symbol_knowledge."):
            raise
        return {"op": op, "status": "not_built", "missing": exc.name}
    cost = type(cost)(
        op=cost.op,
        process_ms=cost.process_ms,
        spawns=cost.spawns,
        tokens_in=_tokens(result, "tokens_in"),
        tokens_out=_tokens(result, "tokens_out"),
        files_reextracted=_count(result),
    )
    row = asdict(cost)
    if isinstance(result, dict) and result.get("status") == "not_applicable":
        row["status"] = "not_applicable"
    row["within_brightline"] = cost.process_ms < BRIGHTLINE_MS
    return row


def baseline_row(root: Path, baseline_ref: str) -> Dict[str, Any]:
    """The full-tree re-extract reference row at *baseline_ref*."""
    cost, absent = build_cost.full_tree_reextract(root, baseline_ref)
    return {
        "baseline_ref": baseline_ref,
        "measured_at_head": subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip(),
        "files_listed_but_absent_in_worktree": absent,
        "row": asdict(cost),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="code_intel_build_cost")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--sha", default="HEAD")
    ap.add_argument("--baseline-ref", default=BASELINE_REF)
    ap.add_argument("--pin-baseline", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.repo).resolve()

    for op in ("code_intel.freshness", "code_intel.workstate_index"):
        print(json.dumps(measure_op(op, root, args.sha), sort_keys=True))

    if args.pin_baseline:
        out = root / BASELINE_FILE
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(baseline_row(root, args.baseline_ref), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
