"""
coordinator_core.telemetry.traffic_manifest — fold the op-latency sink into a
traffic manifest (engine-served leg measured, resident-bash leg pending).

Run: ``python -m coordinator_core.telemetry.traffic_manifest [--since EPOCH] [--out PATH]``.
Prints the manifest to stdout; writes a file only under ``--out``.

Not a registered IPC op and never imported from ``ipc.py`` or ``invoke/``
(same reasoning as ``engine_report``). Counts only complete rows; a row with
no ``kind`` is a legacy complete row. Rows with no ``origin`` count under
"unknown" rather than defaulting to production.

Spec: docs/plans/2026-07-20-invocation-traffic-manifest-spike.md (T2)
"""

from __future__ import annotations

import argparse
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from coordinator_core.telemetry import engine_report
from coordinator_core.telemetry.op_latency import _NON_PRODUCTION_ORIGINS, UNKNOWN, sink_generations

SCHEMA_VERSION = "1"

GENERATES = []  # writes only to the caller-supplied `--out` path, never a tracked artifact


def build_manifest(
    repo_root: Path,
    since: Optional[float] = None,
    *,
    sink_paths: Optional[List[Path]] = None,
) -> dict:
    """Return the traffic manifest dict. `sink_paths` overrides generation discovery."""
    if sink_paths is None:
        sink_paths = sink_generations(repo_root)
    cap = engine_report.MAX_ROWS_SCANNED
    rows_scanned = 0
    non_complete = 0
    non_production = 0
    t_first: Optional[float] = None
    t_last: Optional[float] = None
    ops: dict = {}
    total = 0

    for entry in engine_report.iter_sink_entries(sink_paths=sink_paths, since=since, max_rows=cap):
        rows_scanned += 1
        if entry.get("kind", "complete") != "complete":
            non_complete += 1
            continue
        origin = entry.get("origin")
        if origin in _NON_PRODUCTION_ORIGINS:
            non_production += 1
            continue
        op = entry.get("op")
        if not isinstance(op, str):
            continue
        origin_key = origin if isinstance(origin, str) and origin else UNKNOWN
        caller = entry.get("caller")
        caller_key = caller if isinstance(caller, str) and caller else "null"
        t = entry.get("t_start")
        if isinstance(t, (int, float)) and not isinstance(t, bool):
            t_first = t if t_first is None else min(t_first, t)
            t_last = t if t_last is None else max(t_last, t)
        slot = ops.setdefault(op, {"count": 0, "by_origin": {}, "by_caller": {}, "errors": 0})
        slot["count"] += 1
        slot["by_origin"][origin_key] = slot["by_origin"].get(origin_key, 0) + 1
        slot["by_caller"][caller_key] = slot["by_caller"].get(caller_key, 0) + 1
        if entry.get("outcome") != "ok":
            slot["errors"] += 1
        total += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "machine": socket.gethostname(),
        "source": {
            "sink": "op-latency.jsonl",
            "generations": len(sink_paths),
            "rows_scanned": rows_scanned,
            "rows_truncated": rows_scanned >= cap,
            "window": {"t_first": t_first, "t_last": t_last},
        },
        "excluded": {
            "non_complete_kinds": non_complete,
            "non_production_origins": non_production,
        },
        "legs": {
            "engine_served": {"status": "measured", "total": total, "ops": ops},
            "resident_bash": {
                "status": "pending",
                "blocked_on": "B-β",
                "total": None,
                "entry_points": None,
            },
        },
    }


def _find_repo_root(start: Path) -> Optional[Path]:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="traffic_manifest")
    parser.add_argument("--since", type=float, default=None)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    repo_root = _find_repo_root(Path.cwd())
    if repo_root is None:
        print("traffic_manifest: could not resolve repo root (no .git found)")
        return 1
    text = json.dumps(build_manifest(repo_root, since=args.since), indent=2)
    print(text)
    if args.out is not None:
        args.out.write_text(text + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
