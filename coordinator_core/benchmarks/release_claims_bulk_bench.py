"""Bench: process time and git spawns of releasing a session's claims, and of a dead owner's claims at the commit-scope read.

Three axes over a throwaway repo seeded under a tempfile root, N claims spread across the own sink and
three agent sinks back-pointed to the own session (half absolute paths, half relative):

- `full_release`: `claims._release_path_claims_bulk(None)`, the CLI bulk release.
- `per_path_remedy`: N calls of `claims.release_artifact("artifact", p)`, the per-path remedy the safe-commit
  refusal names. `--remedy-cap` bounds the calls actually run and the rest is extrapolated, flagged in the row.
- `dead_owner_blocking`: `scope.contested_by_live_peers` over N paths held by an ended owner, cold then warm.

`full_release_relative_only` repeats `full_release` with relative claims only (the cost of the release when
absolute entries do not abort it); `full_release_ineligible` uses absolute claims the zero-spawn normaliser
cannot prove (reached through a symlinked directory), so the git-per-path arm shows up. A release that
raises is reported in the row's `error`, with `retained_after` the claims still held. Clocks are
`time.process_time` deltas (children excluded); `spawn_count` counts every `subprocess.Popen`
created during the timed call. `--json` prints one document.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from coordinator_core.git.run import run_git
from coordinator_core.session import claim_index, claims, core as session_core, scope, touch_record

OWN_SID = "release-bench-own"
DEAD_SID = "release-bench-dead-owner"
AGENT_SINKS = 3
ENDED_AGE_SECS = 30 * 86400  # past any recency window: the owner reads ended, as a terminated session does
BAR_MS = 500.0
AXES = ("full_release", "per_path_remedy", "dead_owner_blocking")


class _SpawnCounter:
    """Counts `subprocess.Popen` constructions while active."""

    def __init__(self) -> None:
        self.n = 0

    def __enter__(self) -> "_SpawnCounter":
        counter = self
        self._orig = subprocess.Popen.__init__

        def counting(popen_self, *a, **k):
            counter.n += 1
            return counter._orig(popen_self, *a, **k)

        subprocess.Popen.__init__ = counting  # type: ignore[method-assign]
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen.__init__ = self._orig  # type: ignore[method-assign]


class _WalkCounter:
    """Counts `claim_index._walk` calls (a full scan of every sink) while active."""

    def __init__(self) -> None:
        self.n = 0

    def __enter__(self) -> "_WalkCounter":
        counter = self
        self._orig = claim_index._walk

        def counting(*a, **k):
            counter.n += 1
            return counter._orig(*a, **k)

        claim_index._walk = counting  # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        claim_index._walk = self._orig  # type: ignore[assignment]


@contextmanager
def _session_env(sid: str) -> Iterator[None]:
    prior = os.environ.get("COORDINATOR_SESSION_ID")
    os.environ["COORDINATOR_SESSION_ID"] = sid
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("COORDINATOR_SESSION_ID", None)
        else:
            os.environ["COORDINATOR_SESSION_ID"] = prior


@contextmanager
def _legacy_absolute_entries_allowed() -> Iterator[None]:
    """The writer now rejects absolute paths; the on-disk corpus still carries them, so seeding lifts the check."""
    orig = touch_record._is_out_of_worktree
    touch_record._is_out_of_worktree = lambda _p: False  # type: ignore[assignment]
    try:
        yield
    finally:
        touch_record._is_out_of_worktree = orig  # type: ignore[assignment]


def _claim(sink: Path, sid: str, agent: Optional[str], path: str) -> None:
    touch_record.append_event(
        sink, session_id=sid, agent_id=agent, verb=touch_record.VERB_TOUCH, path=path,
        kind=touch_record.KIND_WRITE,
    )


def seed(root: Path, n: int, ineligible: bool = False, absolute: bool = True) -> dict:
    """Seed `root`: own session claims `n` paths in its own sink and `AGENT_SINKS` agent sinks, alternating
    relative and absolute (absolute via a symlinked directory when `ineligible`; all relative when not
    `absolute`). Returns the layout."""
    if not run_git(["init", "-q", str(root)]).ok:
        raise RuntimeError(f"git init failed in {root}")
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    abs_root = src
    if ineligible:
        link = root / "linked"
        os.symlink(src, link, target_is_directory=True)
        abs_root = link
    base = Path(session_core.sessions_dir(str(root)))
    base.mkdir(parents=True, exist_ok=True)
    name = scope._TOUCH_RECORD_FILENAME
    sinks: List[tuple] = [(base / OWN_SID / name, None)]
    for k in range(AGENT_SINKS):
        adir = base / ".agents" / f"agent-{k:04d}"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "em-session-id.txt").write_text(OWN_SID + "\n", encoding="utf-8")
        sinks.append((adir / name, adir.name))
    paths: List[str] = []
    with _legacy_absolute_entries_allowed():
        for i in range(n):
            rel = f"src/f{i:05d}.py"
            path = rel if (i % 2 == 0 or not absolute) else str(abs_root / f"f{i:05d}.py")
            sink, agent = sinks[(i // 2) % len(sinks)]
            _claim(sink, OWN_SID, agent, path)
            paths.append(path)
    return {"base": str(base), "sinks": [str(s) for s, _a in sinks], "paths": paths}


def seed_dead_owner(root: Path, n: int) -> List[str]:
    """Seed `root` with an ended owner (session dir aged past the recency window) holding `n` write claims."""
    if not run_git(["init", "-q", str(root)]).ok:
        raise RuntimeError(f"git init failed in {root}")
    base = Path(session_core.sessions_dir(str(root)))
    base.mkdir(parents=True, exist_ok=True)
    sink = base / DEAD_SID / scope._TOUCH_RECORD_FILENAME
    paths = [f"src/d{i:05d}.py" for i in range(n)]
    for p in paths:
        _claim(sink, DEAD_SID, None, p)
    (base / OWN_SID).mkdir(parents=True, exist_ok=True)
    aged = time.time() - ENDED_AGE_SECS
    for entry in (*sink.parent.iterdir(), sink.parent):
        os.utime(entry, (aged, aged))
    return paths


def _held(sinks: Sequence[str]) -> int:
    """Claims still `T` across `sinks`."""
    total = 0
    for sink in sinks:
        held, _degraded, _reasons = touch_record._read_stream_claims(sink)
        total += sum(1 for ev in held.values() if getattr(ev, "verb", None) == touch_record.VERB_TOUCH)
    return total


def _timed(fn) -> dict:
    error: Optional[str] = None
    with _SpawnCounter() as spawns:
        p0 = time.process_time()
        try:
            fn()
        except Exception as exc:  # the release aborting is itself a baseline fact
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
        ms = (time.process_time() - p0) * 1000.0
    return {"process_ms": ms, "spawn_count": spawns.n, "error": error}


def _repo(tmp: str) -> Path:
    # realpath: a symlinked temp root (macOS /var) would defeat the zero-spawn arm for every absolute path.
    return Path(os.path.realpath(tmp)) / "repo"


def _scratch(parent: Optional[str]) -> str:
    return tempfile.mkdtemp(prefix="release-claims-bench-", dir=parent)


def _cleanup(tmp: str) -> None:
    from coordinator_core.benchmarks.isolated_clone import rmtree_or_raise

    rmtree_or_raise(Path(tmp), label="release-claims-bulk")


def measure_full_release(n: int, parent: Optional[str], ineligible: bool = False, absolute: bool = True) -> dict:
    tmp = _scratch(parent)
    try:
        root = _repo(tmp)
        layout = seed(root, n, ineligible=ineligible, absolute=absolute)
        cwd = str(root)
        before = _held(layout["sinks"])
        with _session_env(OWN_SID):
            row = _timed(lambda: claims._release_path_claims_bulk(None, cwd=cwd))
        row.update(claims=n, held_before=before, retained_after=_held(layout["sinks"]))
        return row
    finally:
        _cleanup(tmp)


def measure_per_path_remedy(n: int, parent: Optional[str], cap: int) -> dict:
    tmp = _scratch(parent)
    try:
        root = _repo(tmp)
        layout = seed(root, n)
        cwd = str(root)
        calls = min(n, cap) if cap > 0 else n
        chosen = layout["paths"][:calls]

        def run() -> None:
            for p in chosen:
                claims.release_artifact(claims.ARTIFACT_CLASS_PATH, p, cwd=cwd)

        with _session_env(OWN_SID):
            row = _timed(run)
        row.update(claims=n, calls=calls, retained_after=_held(layout["sinks"]))
        row["extrapolated"] = calls < n
        if calls:
            row["process_ms_for_all_claims"] = row["process_ms"] * n / calls
            row["spawn_count_for_all_claims"] = row["spawn_count"] * n / calls
        return row
    finally:
        _cleanup(tmp)


def measure_dead_owner_blocking(n: int, parent: Optional[str]) -> dict:
    tmp = _scratch(parent)
    try:
        root = _repo(tmp)
        paths = seed_dead_owner(root, n)
        cwd = str(root)
        out: dict = {"claims": n}
        for label in ("cold", "warm"):
            with _WalkCounter() as walks:
                row = _timed(lambda: out.__setitem__("contested", scope.contested_by_live_peers(paths, OWN_SID, cwd)))
            out[label] = {**row, "claim_index_full_scans": walks.n}
        contested = out.pop("contested")
        out["blocking_paths"] = None if contested is None else len(contested)
        out["process_ms"] = out["warm"]["process_ms"]
        out["spawn_count"] = out["warm"]["spawn_count"]
        out["claim_index_falls_back_to_full_scan"] = out["warm"]["claim_index_full_scans"] > 0
        return out
    finally:
        _cleanup(tmp)


def measure(n: int, parent: Optional[str] = None, remedy_cap: int = 0) -> dict:
    """`{axis: row}` for the three axes plus `full_release_ineligible`, at `n` claims."""
    return {
        "claims": n,
        "full_release": measure_full_release(n, parent),
        "per_path_remedy": measure_per_path_remedy(n, parent, remedy_cap),
        "dead_owner_blocking": measure_dead_owner_blocking(n, parent),
        "full_release_relative_only": measure_full_release(n, parent, absolute=False),
        "full_release_ineligible": measure_full_release(n, parent, ineligible=True),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claims", type=int, default=2000)
    ap.add_argument("--remedy-cap", type=int, default=0, help="run at most this many per-path calls and extrapolate (0: all)")
    ap.add_argument("--scratch-dir", default=None, help="parent for the throwaway repos (default: system temp)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    result = measure(args.claims, args.scratch_dir, args.remedy_cap)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for axis in (*AXES, "full_release_relative_only", "full_release_ineligible"):
            row = result[axis]
            print(f"{axis}: process_ms {row['process_ms']:.1f}  spawn_count {row['spawn_count']}  error {row.get('error')}")
    over = [a for a in ("full_release", "dead_owner_blocking") if result[a]["process_ms"] > BAR_MS]
    held = [a for a in ("full_release", "full_release_relative_only", "full_release_ineligible") if result[a]["retained_after"]]
    return 1 if over or held else 0


if __name__ == "__main__":
    sys.exit(main())
