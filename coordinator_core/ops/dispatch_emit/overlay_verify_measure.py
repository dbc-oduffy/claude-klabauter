"""Per-leg process time and spawn count of `clean_overlay_verify.verify`.

`python -m coordinator_core.ops.dispatch_emit.overlay_verify_measure --repo-root .` drives `verify` over the
live repo at HEAD and prints one JSON line per leg (clone, checkout, pytest, base_rerun) with `process_ms`
and `spawns`, the bounded figures: child CPU (user+kernel) of the leg's process tree. `wall_ms` and
`files_written` ride along per leg, recorded only so IO wait (wall far above process time) and the paths a leg
wrote can be told apart; no bound reads them. The first run on a repo builds the persistent mirror (the clone
leg runs); a second run is warm. `--cold` drops the mirror first.

Windows reads the harness process's own Job Object (`LiveTreeAccountant`: a snapshot delta covers every
descendant, exited or not, and `spawns` is the job's TotalProcesses delta); POSIX reads the RUSAGE_CHILDREN
delta and counts `subprocess.Popen` calls, so POSIX `spawns` is direct children only (a lower bound)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Sequence

LEGS = ("clone", "checkout", "pytest", "base_rerun")
DEFAULT_SEEDS = ("coordinator_core/ops/dispatch_emit/tests/test_land_run_clean_overlay_verify.py",)
_IS_WINDOWS = sys.platform == "win32"


class LegMeter:
    """A `leg` factory for `clean_overlay_verify.verify`: `with meter("clone") as extra: ...` appends one record
    `{"leg", "process_ms", "spawns", "wall_ms", "files_written"}` to `records`; the body may set
    `extra["files_written"]` (default 0: a leg that writes no tree path). `process_ms`/`spawns` are None when
    the platform accounting could not be attached (the record then carries `error`). Use as a context manager
    so the accounting handle is released."""

    def __init__(self) -> None:
        self.records: list = []
        self._acct = None
        self._error: Optional[str] = None
        self._popen_count = 0
        self._orig_popen_init = None

    def __enter__(self) -> "LegMeter":
        if _IS_WINDOWS:
            try:
                from coordinator_core.benchmarks.process_time import LiveTreeAccountant

                self._acct = LiveTreeAccountant(os.getpid())
            except (OSError, NotImplementedError) as exc:
                self._error = f"job accounting unavailable: {type(exc).__name__}: {exc}"
        else:
            self._orig_popen_init = subprocess.Popen.__init__
            meter = self

            def counting_init(popen_self, *a, **kw):
                meter._popen_count += 1
                return meter._orig_popen_init(popen_self, *a, **kw)

            subprocess.Popen.__init__ = counting_init  # type: ignore[method-assign]
        return self

    def __exit__(self, *_exc) -> None:
        if self._acct is not None:
            self._acct.close()
            self._acct = None
        if self._orig_popen_init is not None:
            subprocess.Popen.__init__ = self._orig_popen_init  # type: ignore[method-assign]
            self._orig_popen_init = None

    def _snap(self) -> Optional[tuple]:
        if _IS_WINDOWS:
            if self._acct is None:
                return None
            s = self._acct.snapshot()
            return s["process_time_ms"], s["procs"]
        import resource

        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        return (ru.ru_utime + ru.ru_stime) * 1000.0, self._popen_count

    @contextmanager
    def __call__(self, name: str) -> Iterator[dict]:
        before = self._snap()
        started = time.perf_counter()
        extra: dict = {}
        try:
            yield extra
        finally:
            after = self._snap()
            tail = {"wall_ms": round((time.perf_counter() - started) * 1000.0, 1), "files_written": extra.get("files_written", 0)}
            if before is None or after is None:
                self.records.append({"leg": name, "process_ms": None, "spawns": None, "error": self._error, **tail})
            else:
                self.records.append(
                    {"leg": name, "process_ms": round(after[0] - before[0], 1), "spawns": int(after[1] - before[1]), **tail}
                )


def _total(legs: Sequence[dict], wall_ms: float) -> dict:
    ran = [leg for leg in legs if leg.get("ran")]
    cpu = [leg["process_ms"] for leg in ran]
    spawns = [leg["spawns"] for leg in ran]
    return {
        "total_process_ms": None if any(v is None for v in cpu) else round(sum(cpu), 1),
        "total_spawns": None if any(v is None for v in spawns) else sum(spawns),
        "total_wall_ms": round(wall_ms, 1),
        "total_files_written": sum(leg.get("files_written") or 0 for leg in ran),
    }


def measure(
    root: Path, seeds: Sequence[str], *, candidate_sha: Optional[str] = None, base_sha: Optional[str] = None,
    touched: Sequence[str] = (), cold: bool = False,
) -> dict:
    """Run `verify` over `seeds` at `candidate_sha` (HEAD when None) with a `LegMeter`; returns
    `{"verdict", "legs": [...], "total": {...}}` with one entry per name in `LEGS` (`ran: false` for a leg
    `verify` skipped: "clone" on a warm mirror). `cold` drops the repo's mirror first."""
    from coordinator_core.git.run import run_git
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    sha = candidate_sha or run_git(["rev-parse", "HEAD"], cwd=str(root)).stdout.strip()
    run_id = f"measure-{uuid.uuid4().hex[:8]}"
    if cold:
        cov.reset_mirror(root)
    started = time.perf_counter()
    with LegMeter() as meter:
        outcome = cov.verify(root, run_id, sha, seeds, base_sha=base_sha, leg=meter, touched=touched)
    wall_ms = (time.perf_counter() - started) * 1000.0
    by_name = {r["leg"]: {**r, "ran": True} for r in meter.records}
    legs = [
        by_name.get(n, {"leg": n, "process_ms": None, "spawns": None, "wall_ms": None, "files_written": None, "ran": False})
        for n in LEGS
    ]
    return {"verdict": outcome.verdict, "detail": outcome.detail[-300:], "legs": legs, "total": _total(legs, wall_ms)}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="overlay_verify_measure")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--seeds", nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--base-sha", default=None)
    parser.add_argument("--touched", nargs="*", default=[], help="repo paths whose test importers join the seeds")
    parser.add_argument("--cold", action="store_true", help="drop the persistent mirror first (a cold build)")
    args = parser.parse_args(argv)
    result = measure(
        Path(args.repo_root).resolve(), args.seeds, base_sha=args.base_sha, touched=args.touched, cold=args.cold,
    )
    for leg in result["legs"]:
        print(json.dumps(leg, sort_keys=True))
    print(json.dumps({"verdict": result["verdict"], "detail": result["detail"], **result["total"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
