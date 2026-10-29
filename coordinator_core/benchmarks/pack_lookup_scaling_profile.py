"""Pack-lookup scaling profile: process-time cost of `git_objects` hit and miss
lookups as the pack count grows.

Fixtures are N single-object packs built with `git_objects._build_pack` /
`_write_pack`. Per row it reports process ms (time.process_time, never wall
clock) for HITS_N hit and MISSES_N miss lookups through `read_object` and
`packed_contains`, plus `cold_first_lookup_process_ms`: the first lookup after
every module-level cache is dropped, i.e. the per-process cost a short-lived
engine process pays. Distinct shas per lookup, so `_OBJECT_CACHE` never answers.

    python coordinator_core/benchmarks/pack_lookup_scaling_profile.py --json
    python coordinator_core/benchmarks/pack_lookup_scaling_profile.py --real-repo .
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.git import git_objects  # noqa: E402

DEFAULT_PACK_COUNTS = (16, 512, 4096)
LOOKUPS = 1000


def _clear_caches() -> None:
    """Drops every module-level cache `git_objects` holds, to model a fresh process."""
    for name in (
        "_PACK_LISTING_CACHE",
        "_PACK_INDEXES_BY_LISTING",
        "_LAST_HIT_PACK",
        "_LOOKUP_SOURCES",
        "_MIDX_CACHE",
        "_PACK_INDEX_CACHE",
        "_OBJECT_CACHE",
    ):
        cache = getattr(git_objects, name, None)
        if cache is not None:
            cache.clear()


def _blob_sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(payload) + payload).hexdigest()


def _miss_shas(n: int) -> list[str]:
    return [hashlib.sha1(b"miss-%d" % i).hexdigest() for i in range(n)]


def _build_fixture(common_dir: Path, pack_count: int) -> list[str]:
    shas = []
    for i in range(pack_count):
        payload = b"pack-lookup-fixture-%d\n" % i
        sha = _blob_sha(payload)
        git_objects._write_pack(common_dir, {sha: (b"blob", payload)})
        shas.append(sha)
    return shas


def _process_ms(fn) -> float:
    start = time.process_time()
    fn()
    return (time.process_time() - start) * 1000.0


def _measure(common_dir: Path, hit_shas: list[str]) -> dict:
    hits = hit_shas[:LOOKUPS]
    misses = _miss_shas(LOOKUPS)

    _clear_caches()
    cold_probe = hits[0] if hits else misses[0]
    cold_ms = _process_ms(lambda: git_objects.read_object(common_dir, cold_probe))

    # Steady-state legs: caches settled once, then never cleared, so the one-time
    # table build is charged to cold_first_lookup_process_ms only. Distinct shas
    # keep _OBJECT_CACHE from answering a repeat.
    _clear_caches()
    git_objects.read_object(common_dir, "0" * 40)
    row = {"cold_first_lookup_process_ms": round(cold_ms, 3)}

    def run(fn, shas):
        return lambda: [fn(common_dir, s) for s in shas]

    for label, shas in (("hit", hits), ("miss", misses)):
        row[f"read_object_{label}_process_ms"] = round(_process_ms(run(git_objects.read_object, shas)), 3)
        row[f"packed_contains_{label}_process_ms"] = round(_process_ms(run(git_objects.packed_contains, shas)), 3)
    row["hit_lookups"] = len(hits)
    row["miss_lookups"] = len(misses)  # process_time is ~15.6ms-granular on Windows; read sub-50ms legs as noise
    return row


def _real_repo_hits(common_dir: Path) -> list[str]:
    hits: list[str] = []
    indexes = git_objects._pack_indexes(common_dir)
    step = max(1, len(indexes) // LOOKUPS)
    for i in range(0, len(indexes), step):
        pidx = indexes[i][2]
        if pidx is not None and len(pidx.shas) >= 20:
            hits.append(pidx.shas[:20].hex())
        if len(hits) >= LOOKUPS:
            break
    return hits


def profile(pack_counts=DEFAULT_PACK_COUNTS, real_repo: str | None = None) -> dict:
    rows = []
    scratch = _REPO / "scratch"
    scratch.mkdir(exist_ok=True)
    for n in pack_counts:
        tmp = Path(tempfile.mkdtemp(prefix="pack-lookup-scaling-", dir=scratch))
        try:
            common = tmp / ".git"
            (common / "objects" / "pack").mkdir(parents=True)
            hit_shas = _build_fixture(common, n)
            rows.append({"pack_count": n, **_measure(common, hit_shas)})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    out: dict = {"fixture_rows": rows}
    if real_repo:
        common = Path(real_repo).resolve() / ".git"
        _clear_caches()
        pack_count = len(git_objects._iter_pack_files(common))
        hits = _real_repo_hits(common)
        out["real_repo"] = {"pack_count": pack_count, **_measure(common, hits)}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--real-repo", default=None, help="repo root whose .git pack set is measured read-only")
    ap.add_argument("--pack-counts", default=",".join(map(str, DEFAULT_PACK_COUNTS)))
    args = ap.parse_args(argv)
    result = profile(tuple(int(x) for x in args.pack_counts.split(",")), args.real_repo)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for r in result["fixture_rows"] + ([result["real_repo"]] if "real_repo" in result else []):
            print(r)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
