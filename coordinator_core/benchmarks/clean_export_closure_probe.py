"""Measure one clean-export closure scan (`clean_overlay_verify._closure_for`) on this repo's tree: process
milliseconds (`time.process_time`, never wall) and the child processes spawned while it ran.

`python coordinator_core/benchmarks/clean_export_closure_probe.py [--root DIR] [--declared PATH ...] [ARGV ...]`
prints one JSON document: the argv, the closure size (`null` for a full fill), process ms, spawn count and
the tracked-file count. Default argv is the clean-overlay export's own test file.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

DEFAULT_ARGV = (
    "python", "-m", "pytest",
    "coordinator_core/ops/dispatch_emit/tests/test_clean_overlay_scoped_export.py",
)


class _SpawnCount:
    """Counts `subprocess.Popen` constructions (every `run`/`check_output` goes through it) while active."""

    def __init__(self) -> None:
        self.n = 0
        self._orig = subprocess.Popen.__init__

    def __enter__(self) -> "_SpawnCount":
        count = self

        def counted(popen, *a, **k) -> None:
            count.n += 1
            return count._orig(popen, *a, **k)

        subprocess.Popen.__init__ = counted  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._orig  # type: ignore[method-assign]


def measure(root: Path, declared: Sequence[str], argv: Sequence[str]) -> dict:
    """One `_closure_for` call's process ms and spawn count; imports are paid before the clock starts."""
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov, export_closure, test_reach  # noqa: F401

    with _SpawnCount() as spawns:
        t0 = time.process_time()
        found = cov._closure_for(root, declared, argv)
        ms = (time.process_time() - t0) * 1000.0
    tracked: Optional[int]
    try:
        tracked = len(test_reach._tracked(root))
    except Exception:
        tracked = None
    return {
        "argv": list(argv),
        "closure_paths": None if found is None else len(found),
        "tracked_files": tracked,
        "process_ms": round(ms, 1),
        "spawns": spawns.n,
    }


def main(args: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=str(_REPO))
    ap.add_argument("--declared", action="append", default=[])
    ap.add_argument("argv", nargs="*")
    ns = ap.parse_args(args)
    result = measure(Path(ns.root), ns.declared, ns.argv or DEFAULT_ARGV)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
