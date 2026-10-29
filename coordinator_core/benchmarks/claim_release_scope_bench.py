"""Bench: process time and touch-record files opened by one `release_committed_claims` call.

Seeds a throwaway repo whose sessions dir holds N peer sessions (each claiming a few paths, as
`claim_lookup_spike` reads them) plus M agent sinks, then releases a 3-path commit for one own
session. Reports `process_ms` (`time.process_time`, excludes git children) and `files_opened`
(calls to `builtins.open` on a path under the sessions dir, counted by a wrapper installed here
only). The release's cost should not scale with the peer count; `--peers` sweeps it to show that.
"""
from __future__ import annotations

import argparse
import builtins
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.git.run import run_git
from coordinator_core.session import core as session_core
from coordinator_core.session import scope, touch_record

OWN_SID = "own-session"
COMMIT_PATHS = ("src/a.py", "src/b.py", "src/c.py")
BAR_MS = 500.0


class _OpenCounter:
    """Counts `builtins.open` calls whose target lies under `prefix` while active."""

    def __init__(self, prefix: str) -> None:
        self.prefix = os.path.normcase(os.path.abspath(prefix))
        self.n = 0

    def __enter__(self) -> "_OpenCounter":
        counter = self
        self._orig = builtins.open

        def counting(file, *a, **k):
            if isinstance(file, (str, bytes, os.PathLike)):
                p = os.fsdecode(file)
                if os.path.normcase(os.path.abspath(p)).startswith(counter.prefix):
                    counter.n += 1
            return counter._orig(file, *a, **k)

        builtins.open = counting  # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        builtins.open = self._orig  # type: ignore[assignment]


def _claim(sink: Path, sid: str, agent: Optional[str], path: str) -> None:
    touch_record.append_event(
        sink, session_id=sid, agent_id=agent, verb=touch_record.VERB_TOUCH, path=path,
        kind=touch_record.KIND_WRITE,
    )


def seed(root: Path, peers: int, agents: int, own_agents: int = 5, claims_per_peer: int = 4) -> str:
    """Seed `root`'s sessions dir; returns the sessions dir. The own session claims `COMMIT_PATHS`
    in its own sink and in `own_agents` back-pointed agent sinks; the other agent sinks belong to peers."""
    if not run_git(["init", "-q", str(root)]).ok:
        raise RuntimeError(f"git init failed in {root}")
    base = Path(session_core.sessions_dir(str(root)))
    base.mkdir(parents=True, exist_ok=True)
    sink_name = scope._TOUCH_RECORD_FILENAME
    for path in COMMIT_PATHS:
        _claim(base / OWN_SID / sink_name, OWN_SID, None, path)
    for i in range(peers):
        sid = f"peer-{i:04d}"
        for j in range(claims_per_peer):
            _claim(base / sid / sink_name, sid, None, f"peer/{i}/f{j}.py")
        if i % 10 == 0:
            _claim(base / sid / sink_name, sid, None, COMMIT_PATHS[0])
    agents_base = base / ".agents"
    for k in range(agents):
        owner = OWN_SID if k < own_agents else f"peer-{k % max(peers, 1):04d}"
        adir = agents_base / f"agent-{k:04d}"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "em-session-id.txt").write_text(owner + "\n", encoding="utf-8")
        _claim(adir / sink_name, owner, adir.name, COMMIT_PATHS[k % len(COMMIT_PATHS)])
    return str(base)


def measure(peers: int, agents: int, scratch_parent: Optional[str] = None) -> dict:
    """One seeded release: `{process_ms, files_opened, peers, agents}`."""
    tmp = tempfile.mkdtemp(prefix="claim-release-bench-", dir=scratch_parent)
    try:
        root = Path(tmp) / "repo"
        base = seed(root, peers, agents)
        cwd = str(root)
        session_core.sessions_dir(cwd)  # resolve once so the timed call measures the release alone
        with _OpenCounter(base) as opens:
            p0 = time.process_time()
            scope.release_committed_claims(OWN_SID, list(COMMIT_PATHS), cwd=cwd)
            ms = (time.process_time() - p0) * 1000.0
        return {"process_ms": ms, "files_opened": opens.n, "peers": peers, "agents": agents}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--peers", type=int, default=200)
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--scratch-dir", default=None, help="parent for the throwaway repo (default: system temp)")
    args = ap.parse_args(argv)

    runs = [measure(args.peers, args.agents, args.scratch_dir) for _ in range(args.samples)]
    ms = [r["process_ms"] for r in runs]
    opened = sorted({r["files_opened"] for r in runs})
    print(f"shape: peers={args.peers} agents={args.agents} paths={len(COMMIT_PATHS)} samples={args.samples}")
    print(f"process_ms: first {ms[0]:.1f}  median {statistics.median(ms):.1f}  max {max(ms):.1f}")
    print(f"files_opened: {opened[0] if len(opened) == 1 else opened}")
    half = measure(max(args.peers // 2, 1), args.agents, args.scratch_dir)
    print(f"files_opened at peers={half['peers']}: {half['files_opened']}")
    over = statistics.median(ms) > BAR_MS
    print(f"BENCH: {'OVER-BAR' if over else 'AT-BAR'} median {statistics.median(ms):.1f}ms (bar {BAR_MS:.0f}ms)")
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
