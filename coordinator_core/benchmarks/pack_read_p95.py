"""Pack-read p95: latency of `git_objects.read_object` over a seeded sample of packed shas.

Read-only on the live store, no git spawn. The sample is drawn from the store's own `.idx`
files: each of N seeded 160-bit targets selects the smallest packed sha at or above it across
every pack, so the same seed names the same objects before and after a repack (a sha moves
packs; the target-successor does not). Each sha is read once with `time.perf_counter`.
`first_read_ms` is the cold first read (pack listing and lookup-source build); p50/p95/max
cover the remaining reads. `process_seconds` is `time.process_time` over sampling plus reads.

    python coordinator_core/benchmarks/pack_read_p95.py [--json] [--repo PATH] [--seed 20261114] [--n 200]
"""

from __future__ import annotations

import argparse
import json
import mmap
import random
import struct
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.git import git_objects  # noqa: E402

DEFAULT_SEED = 20261114
DEFAULT_N = 200
_HEADER = b"\xfftOc\x00\x00\x00\x02"
_NAMES_AT = 8 + 256 * 4


def _common_dir(repo: Path) -> Path:
    git = repo / ".git"
    if git.is_file():
        raise SystemExit(f"{git} is a gitfile; pass the common git dir's repo")
    return git if git.is_dir() else repo


def _open_indexes(pack_dir: Path) -> "list[tuple[mmap.mmap, int]]":
    """Every readable v2 `.idx` as (mmap, object count), in sorted name order."""
    out = []
    for idx in sorted(pack_dir.glob("pack-*.idx")):
        try:
            with open(idx, "rb") as fh:
                if fh.seek(0, 2) < _NAMES_AT + 40:
                    continue
                mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        except (OSError, ValueError):
            continue
        if mm[:8] != _HEADER:
            mm.close()
            continue
        out.append((mm, struct.unpack_from(">I", mm, 8 + 255 * 4)[0]))
    return out


def _successor(mm: mmap.mmap, count: int, target: bytes) -> "bytes | None":
    """Smallest sha in this index >= `target`, or None. Bisects only the first-byte bucket."""
    first = target[0]
    lo = struct.unpack_from(">I", mm, 8 + (first - 1) * 4)[0] if first else 0
    hi = struct.unpack_from(">I", mm, 8 + first * 4)[0]
    while lo < hi:
        mid = (lo + hi) // 2
        if mm[_NAMES_AT + mid * 20 : _NAMES_AT + mid * 20 + 20] < target:
            lo = mid + 1
        else:
            hi = mid
    if lo >= count:
        return None
    return bytes(mm[_NAMES_AT + lo * 20 : _NAMES_AT + lo * 20 + 20])


def sample_shas(pack_dir: Path, seed: int, n: int) -> "tuple[list[str], int]":
    """`n` seeded packed shas (hex, sorted, de-duplicated) and the pack count."""
    indexes = _open_indexes(pack_dir)
    try:
        rng = random.Random(seed)
        found: "set[str]" = set()
        for _ in range(n):
            target = rng.getrandbits(160).to_bytes(20, "big")
            best = None
            for mm, count in indexes:
                cand = _successor(mm, count, target)
                if cand is not None and (best is None or cand < best):
                    best = cand
            if best is not None:
                found.add(best.hex())
        return sorted(found), len(indexes)
    finally:
        for mm, _ in indexes:
            mm.close()


def _percentile(sorted_ms: "list[float]", q: float) -> float:
    if not sorted_ms:
        return 0.0
    pos = max(0, min(len(sorted_ms) - 1, int(round(q * (len(sorted_ms) - 1)))))
    return sorted_ms[pos]


def run(repo: Path, seed: int, n: int) -> dict:
    """Sample then read; returns the JSON-shaped result."""
    common = _common_dir(repo)
    cpu0 = time.process_time()
    shas, packs = sample_shas(common / "objects" / "pack", seed, n)
    sample_cpu = time.process_time() - cpu0
    timings: "list[float]" = []
    errors: "list[str]" = []
    first_ms = 0.0
    for i, sha in enumerate(shas):
        t0 = time.perf_counter()
        try:
            if git_objects.read_object(common, sha) is None:
                errors.append(f"{sha[:12]}:missing")
        except Exception as exc:  # a deadline is a datum, not a crash
            errors.append(f"{sha[:12]}:{type(exc).__name__}")
        ms = (time.perf_counter() - t0) * 1000.0
        if i == 0:
            first_ms = ms
        else:
            timings.append(ms)
    timings.sort()
    return {
        "seed": seed,
        "sample_size": len(shas),
        "packs": packs,
        "first_read_ms": round(first_ms, 3),
        "p50_ms": round(_percentile(timings, 0.50), 3),
        "p95_ms": round(_percentile(timings, 0.95), 3),
        "max_ms": round(max(timings + [first_ms]), 3),
        "errors": errors,
        "sample_process_seconds": round(sample_cpu, 4),
        "process_seconds": round(time.process_time() - cpu0, 4),
    }


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=_REPO)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--n", type=int, default=DEFAULT_N)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    result = run(args.repo, args.seed, args.n)
    print(json.dumps(result, indent=None if args.json else 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
