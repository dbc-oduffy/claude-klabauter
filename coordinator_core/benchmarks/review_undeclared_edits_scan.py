"""Benchmark for the review-time undeclared-edits scan, by process time, spawns and result digest.

    python coordinator_core/benchmarks/review_undeclared_edits_scan.py [--root DIR] [--marker-ns N] [--runs K]

Calls ``stage_dirt_snapshot.undeclared_edits(root, record, [])`` -- the default call
``review_prep.undeclared_edits_for_run`` makes before its peer-hold filter -- against a throwaway
record holding one stage marker, then prints process time (``time.process_time``, never wall clock),
the ``subprocess.Popen`` spawn count and a sha256 digest of the returned list. Run before and after a
cut: the digest must match for the same tree.

The default marker is 0 so every dirty tracked path is a candidate (the stat pass and byte compare
both run at full width); pass ``--marker-ns`` for a real stage time. The digest tracks the live tree,
so compare digests only across runs with no peer edit between them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402


def _write_marker(record: Path, marker_ns: int) -> None:
    from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as sds

    (record / sds.SNAPSHOT_FILE).write_text(
        json.dumps({"schema": sds._SCHEMA, "recorded_at": "bench", "marker_ns": marker_ns}), encoding="utf-8"
    )


def measure(root: Path, marker_ns: int = 0, runs: int = 5) -> dict:
    """Run the default call ``runs`` times; ``cpu_ms`` per run (first run cold), ``spawns`` per run,
    and the digest of the first run's list (``digest_stable`` False when later runs differ)."""
    from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as sds

    spawned = [0]
    real_init = subprocess.Popen.__init__

    def counting_init(self, *args, **kwargs):
        spawned[0] += 1
        return real_init(self, *args, **kwargs)

    cpu_ms, spawns, digests, sizes = [], [], [], []
    from coordinator_core.temp_layout import managed_temp_dir

    with managed_temp_dir(prefix="undeclared-edits-bench-") as tmp:
        record = Path(tmp)
        _write_marker(record, marker_ns)
        subprocess.Popen.__init__ = counting_init
        try:
            for _ in range(max(1, runs)):
                spawned[0] = 0
                t0 = time.process_time()
                found = sds.undeclared_edits(root, record, [])
                cpu_ms.append((time.process_time() - t0) * 1000.0)
                spawns.append(spawned[0])
                sizes.append(len(found))
                digests.append(hashlib.sha256("\n".join(found).encode("utf-8")).hexdigest())
        finally:
            subprocess.Popen.__init__ = real_init
    return {
        "root": str(root),
        "marker_ns": marker_ns,
        "runs": len(cpu_ms),
        "cpu_ms": [round(v, 1) for v in cpu_ms],
        "cpu_ms_first": round(cpu_ms[0], 1),
        "cpu_ms_median": round(statistics.median(cpu_ms), 1),
        "cpu_ms_min": round(min(cpu_ms), 1),
        "spawns": max(spawns),
        "result_count": sizes[0],
        "digest": digests[0],
        "digest_stable": len(set(digests)) == 1,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--marker-ns", type=int, default=0)
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(argv)
    declare_benchmark_origin()
    result = measure(args.root, args.marker_ns, args.runs)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
