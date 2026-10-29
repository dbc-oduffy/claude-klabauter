"""Measure what one `claim_index.lookup` reads from disk: index files, claimant sinks folded, bytes
and whether concurrent lookups re-fold the same sinks. Read-only on the sessions dir (one first
lookup may build the index beside it, as production does). Clocks are `time.process_time()` deltas.

`python -m coordinator_core.benchmarks.claim_index_lookup_cost [--base DIR] [--threads N] [--paths K]`
prints one JSON document: the corpus census, then per-path cold/warm lookup costs and the
concurrent-cold fan-in.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Optional, Sequence

from coordinator_core.session import claim_index, touch_record


class Probe:
    """Counters filled by `instrument` while a lookup runs."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.index_reads = 0
        self.index_bytes = 0
        self.fold_calls: List[str] = []
        self.fold_misses: List[str] = []
        self.fold_bytes = 0

    def summary(self) -> dict:
        distinct = set(self.fold_calls)
        return {
            "index_file_reads": self.index_reads,
            "index_bytes": self.index_bytes,
            "sinks_folded": len(distinct),
            "fold_calls": len(self.fold_calls),
            "fold_cache_misses": len(self.fold_misses),
            "fold_bytes_read": self.fold_bytes,
        }


def family_bytes(sink: str) -> int:
    """Total bytes of a sink's live file plus its rotated generations."""
    total = 0
    for member in touch_record.discover_family(sink):
        try:
            total += os.stat(member).st_size
        except OSError:
            continue
    return total


@contextmanager
def instrument(probe: Probe) -> Iterator[Probe]:
    """Wrap the two disk-reading seams of `_lookup_from_index`; restored on exit."""
    orig_fold = touch_record.read_stream_claims_and_held_kind
    orig_names = claim_index._read_index_names

    def fold(sink_path):
        key = str(sink_path)
        cached = touch_record._FOLD_CACHE.get(key)
        miss = cached is None or cached[0] != touch_record._live_signature(sink_path)
        size = family_bytes(key) if miss else 0
        with probe.lock:
            probe.fold_calls.append(key)
            if miss:
                probe.fold_misses.append(key)
                probe.fold_bytes += size
        return orig_fold(sink_path)

    def names(file_path):
        out = orig_names(file_path)
        try:
            size = os.stat(file_path).st_size
        except OSError:
            size = 0
        with probe.lock:
            probe.index_reads += 1
            probe.index_bytes += size
        return out

    touch_record.read_stream_claims_and_held_kind = fold
    claim_index._read_index_names = names
    try:
        yield probe
    finally:
        touch_record.read_stream_claims_and_held_kind = orig_fold
        claim_index._read_index_names = orig_names


def census(base: str) -> dict:
    """Every touch-record file under `base` (live, rotated and archived) by count and bytes."""
    files = 0
    total = 0
    live = 0
    for root, _dirs, names in os.walk(base):
        for name in names:
            if not name.startswith(touch_record.RECORD_FILENAME):
                continue
            try:
                size = os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
            files += 1
            total += size
            if name == touch_record.RECORD_FILENAME:
                live += 1
    pairs, _complete = claim_index._enumerate_claim_sinks(base)
    sizes = sorted(((family_bytes(sink), sink) for sink, _s, _a in pairs), reverse=True)
    return {
        "largest_sink_families": [{"bytes": b, "sink": s} for b, s in sizes[:3]],
        "record_files": files,
        "record_bytes": total,
        "live_sinks": live,
        "enumerated_claim_sinks": len(pairs),
        "enumerated_family_bytes": sum(family_bytes(sink) for sink, _s, _a in pairs),
    }


def pick_paths(base: str, count: int) -> List[str]:
    """The most-claimed paths first, then the median-claimed one, from a read-only walk."""
    claims = claim_index._walk(base).claims
    ranked = sorted((p for p in claims if claims[p]), key=lambda p: (-len(claims[p]), p))
    if not ranked:
        return []
    picked = ranked[: max(count - 1, 1)]
    mid = ranked[len(ranked) // 2]
    if mid not in picked:
        picked.append(mid)
    return picked[:count]


def measure_lookup(base: str, paths: Sequence[str], cold: bool) -> dict:
    """One `lookup(paths)` call's disk cost; `cold` empties the fold and owner caches first."""
    if cold:
        touch_record._FOLD_CACHE.clear()
        touch_record._AGENT_OWNER_CACHE.clear()
    probe = Probe()
    p0 = time.process_time()
    with instrument(probe):
        result = claim_index.lookup(list(paths), sessions_dir=base)
    out = probe.summary()
    out["process_ms"] = round((time.process_time() - p0) * 1000.0, 3)
    out["claimants"] = sum(len(v) for v in result.values())
    out["complete"] = bool(result.complete)
    return out


def measure_concurrent(base: str, paths: Sequence[str], threads: int) -> dict:
    """`threads` cold lookups released together: how many sinks each folds and how many sinks
    more than one thread folds (a miss each, since no thread's fold is visible to the others
    before it lands)."""
    touch_record._FOLD_CACHE.clear()
    touch_record._AGENT_OWNER_CACHE.clear()
    probe = Probe()
    barrier = threading.Barrier(threads)

    incomplete: List[bool] = []

    def run() -> None:
        barrier.wait()
        incomplete.append(not claim_index.lookup(list(paths), sessions_dir=base).complete)

    workers = [threading.Thread(target=run) for _ in range(threads)]
    p0 = time.process_time()
    with instrument(probe):
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    per_sink: dict = {}
    for sink in probe.fold_misses:
        per_sink[sink] = per_sink.get(sink, 0) + 1
    refolded = {s: n for s, n in per_sink.items() if n > 1}
    out = probe.summary()
    out.update(
        threads=threads,
        lookups_degraded_by_time_cap=sum(incomplete),
        process_ms=round((time.process_time() - p0) * 1000.0, 3),
        sinks_missed_by_more_than_one_thread=len(refolded),
        max_misses_on_one_sink=max(per_sink.values(), default=0),
    )
    return out


def run(base: str, paths: Optional[Sequence[str]], npaths: int, threads: int) -> dict:
    chosen = list(paths) if paths else pick_paths(base, npaths)
    if chosen:
        claim_index.lookup(chosen, sessions_dir=base)  # ensure the index exists before measuring
    report = {"base": base, "census": census(base), "paths": chosen, "per_path": []}
    for path in chosen:
        report["per_path"].append(
            {
                "path": path,
                "cold": measure_lookup(base, [path], cold=True),
                "warm": measure_lookup(base, [path], cold=False),
            }
        )
    if chosen:
        report["all_paths_cold"] = measure_lookup(base, chosen, cold=True)
        report["all_paths_warm"] = measure_lookup(base, chosen, cold=False)
        report["concurrent_cold"] = measure_concurrent(base, chosen, threads)
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=None, help="sessions dir; default the cwd repo's")
    ap.add_argument("--paths", type=int, default=3, help="how many claimed paths to sample")
    ap.add_argument("--path", action="append", default=None, help="explicit path (repeatable)")
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args(argv)
    base = args.base
    if base is None:
        from coordinator_core.session import core as session_core

        base = session_core.sessions_dir(os.getcwd())
    if not base:
        raise SystemExit("no sessions dir resolvable")
    print(json.dumps(run(base, args.path, args.paths, args.threads), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
