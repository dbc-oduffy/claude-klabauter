"""Bytes read, process time and spawns of one `land_run._dirty_among` call over a dirty tracked tree.

Fixture: a tmp git repo with 405 committed files; 5 are rewritten with new bytes (content-changed) and
`os.utime` moves the other 400 forward (content unchanged, stat dirty). The `stat_clean` variant leaves
those 400 untouched. Bytes are counted by wrapping `Path.read_bytes` for the call's duration, so the
figure is independent of any ledger counter inside `land_run`; spawns come from `spawn_counter`;
process time is `time.process_time()` across the call.

    python coordinator_core/benchmarks/land_run_dirty_scan_bytes.py [--files N] [--changed N]

Prints one JSON object per variant: `bytes_read`, `files_read`, `total_bytes` (sum of every file's size),
`process_ms`, `spawns`, `changed`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import pytest  # noqa: E402

from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed  # noqa: E402

FILES = 405
CHANGED = 5
VARIANTS = ("stat_dirty", "stat_clean")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BODY_LINES = 40


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )


def _rel(i: int) -> str:
    return f"src/pkg{i % 9}/mod_{i:04d}.txt"


def _body(i: int, salt: str) -> bytes:
    """Distinct per-file bytes; size varies with `i` so the byte total is not a multiple of one size."""
    line = f"file {i} {salt} " + "x" * (40 + i % 17) + "\n"
    return (line * (_BODY_LINES + i % 11)).encode("utf-8")


def build_fixture(root: Path, files: int = FILES, changed: int = CHANGED, touch: bool = True) -> dict:
    """Init `root` as a repo with `files` committed files, then dirty it.

    Returns `{"paths": [...], "changed": [...], "touched": [...], "total_bytes": int}`. The first `changed`
    paths get new bytes; with `touch` the rest get an mtime moved 10s forward. Every file is back-dated
    before the commit so the index entries are never racily clean.
    """
    root.mkdir(parents=True, exist_ok=True)
    paths = [_rel(i) for i in range(files)]
    old = time.time() - 600
    for i, rel in enumerate(paths):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_body(i, "base"))
        os.utime(target, (old, old))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "update-index", "--refresh")
    changed_paths = paths[:changed]
    touched = paths[changed:] if touch else []
    for i, rel in enumerate(paths[:changed]):
        (root / rel).write_bytes(_body(i, "rewritten"))
    for rel in touched:
        os.utime(root / rel, (old + 10, old + 10))
    return {
        "paths": paths,
        "changed": changed_paths,
        "touched": touched,
        "total_bytes": sum((root / p).stat().st_size for p in paths),
    }


def measure(root: Path, paths: list, total_bytes: int) -> dict:
    """One `_dirty_among(root, paths)` call under a `Path.read_bytes` byte counter and a spawn counter."""
    from coordinator_core.ops.dispatch_emit import land_run

    read: list = []
    real = Path.read_bytes

    def counting(self: Path) -> bytes:
        data = real(self)
        read.append((str(self), len(data)))
        return data

    with pytest.MonkeyPatch.context() as mp:
        with _count_spawns_attributed(mp) as spawns:
            mp.setattr(Path, "read_bytes", counting)
            t0 = time.process_time()
            changed, _ignored, _head = land_run._dirty_among(root, paths)
            process_ms = (time.process_time() - t0) * 1000.0
            mp.setattr(Path, "read_bytes", real)
    return {
        "bytes_read": sum(n for _p, n in read),
        "files_read": len({p for p, _n in read}),
        "total_bytes": total_bytes,
        "process_ms": round(process_ms, 3),
        "spawns": len(spawns),
        "changed": sorted(changed),
    }


def run_variant(variant: str, base: Path, files: int = FILES, changed: int = CHANGED) -> dict:
    """Build the `variant` fixture under `base` and measure it; `changed` in the result is a count."""
    fx = build_fixture(base / variant, files, changed, touch=variant == "stat_dirty")
    res = measure(base / variant, fx["paths"], fx["total_bytes"])
    res.update(variant=variant, files=files, changed_expected=sorted(fx["changed"]))
    res["changed_ok"] = res["changed"] == res["changed_expected"]
    res["changed"] = len(res["changed"])
    del res["changed_expected"]
    return res


def main(argv: list | None = None) -> int:
    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--files", type=int, default=FILES)
    ap.add_argument("--changed", type=int, default=CHANGED)
    args = ap.parse_args(argv)
    scratch = _REPO / "scratch"
    scratch.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="dirty-scan-bytes-", dir=scratch))
    try:
        for variant in VARIANTS:
            print(json.dumps(run_variant(variant, base, args.files, args.changed), sort_keys=True))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
