"""Tree-walk census: one `git ls-files` bucketed by the atlas's census_buckets.

The example-retrieval-repo-absent producer of the census-shaped payload; `census_shape` pins the shape.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from coordinator_core.architecture_survey import census_shape
from coordinator_core.win_portability import no_console_creationflags


def _prefix(raw: str) -> str:
    """Normalize a bucket dir to a POSIX prefix without a trailing slash ('' matches everything)."""
    norm = raw.replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    norm = norm.strip("/")
    return "" if norm == "." else norm


def _owner(path: str, owners: dict[str, str]) -> str | None:
    """Bucket id of the longest prefix of `path` in `owners`: O(depth) dict probes, not O(buckets)."""
    probe = path
    while True:
        hit = owners.get(probe)
        if hit is not None:
            return hit
        cut = probe.rfind("/")
        if cut < 0:
            return owners.get("")
        probe = probe[:cut]


def build(repo_root: str | Path, census_buckets: list[dict[str, Any]]) -> dict[str, Any]:
    """Bucket the repo's tracked files; every supplied bucketId appears, empty when nothing matches.

    Spawns exactly one `git ls-files -z`. A bucket's `dirs` are all candidate prefixes and the
    longest match across every bucket wins; ties go to the first-supplied bucket.
    `loc` is None and `oversized` False for every file (no oversized signal on this path).
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )
    paths = [p.decode("utf-8", "surrogateescape") for p in proc.stdout.split(b"\0") if p]

    files_by_bucket: dict[str, list[dict[str, Any]]] = {}
    owners: dict[str, str] = {}
    for bucket in census_buckets:
        bucket_id = bucket["bucketId"]
        files_by_bucket.setdefault(bucket_id, [])
        for raw in bucket.get("dirs") or []:
            owners.setdefault(_prefix(raw), bucket_id)

    unbucketed = 0
    for path in paths:
        owner = _owner(path, owners)
        if owner is None:
            unbucketed += 1
        else:
            files_by_bucket[owner].append({"path": path, "loc": None, "oversized": False})

    results = [
        {"bucket_id": bucket_id, "files": files, "anomalies": []}
        for bucket_id, files in files_by_bucket.items()
    ]
    counts = {
        "files": len(paths),
        "bucketed": len(paths) - unbucketed,
        "unbucketed": unbucketed,
    }
    return census_shape.build(
        results,
        source="tree",
        chunk_table_path=None,
        counts=counts,
        unbucketed=unbucketed,
        oversized_signal_available=False,
    )
