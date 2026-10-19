"""Stopped-run attribution: which runs did this session submit that have since ended, and which of
their run artifacts are dirty and uncontested.

Ownership is the run's `scratch/warp/<run>/submission.json` naming the session plus
`warp_live.run_ended_on_evidence` (the harness's own terminal or stop notification), never timing.
A run whose task has no terminal notification is live and contributes nothing, and a missing token
is never proof of an end (a failed mint launches the run tokenless). Runs older than
`STOPPED_RUN_WINDOW_SECONDS` stay the reaper's. No subprocess and no git.
"""

from __future__ import annotations

import json
import os
import re
import time
from calendar import timegm
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import run_artifacts, run_token

STOPPED_RUN_WINDOW_SECONDS: int = run_token.TTL_SECONDS

_STAMP = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})-[0-9a-f]{6}$")


@dataclass(frozen=True)
class StoppedResidue:
    """`carried`: run id -> dirty, uncontested candidate paths. `withheld`: path -> `peer-held`
    or `contest-unknown`."""

    carried: dict[str, tuple[str, ...]]
    withheld: dict[str, str]


def _stamp_epoch(run_id: str) -> Optional[float]:
    match = _STAMP.search(run_id)
    if match is None:
        return None
    try:
        y, mo, d, h, mi, s = (int(g) for g in match.groups())
        return float(timegm((y, mo, d, h, mi, s, 0, 0, 0)))
    except (ValueError, OverflowError):
        return None


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def _read_submission(root: Path, run_id: str) -> Optional[dict]:
    try:
        data = json.loads(warp_live._submission_path(root, run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def stopped_own_runs(repo_root: Path, session_id: str, *, now: Optional[float] = None) -> tuple[str, ...]:
    """Sorted run ids this session submitted, inside the window, that ended on evidence. For an
    unclaimed held token the verdict reads that record; with no held token it reads a record built
    from the submission. A claimed token (mid terminal commit) is live."""
    if not session_id:
        return ()
    root = Path(repo_root)
    current = time.time() if now is None else now
    candidates: list[tuple[str, float]] = []
    try:
        with os.scandir(root / warp_live.RUN_DIR_ROOT) as it:
            for entry in it:
                stamp = _stamp_epoch(entry.name)
                if stamp is None or current - stamp > STOPPED_RUN_WINDOW_SECONDS:
                    continue
                if entry.is_dir():
                    candidates.append((entry.name, stamp))
    except OSError:
        return ()
    if not candidates:
        return ()
    held = {r["run_id"]: r for r in run_token.held_records(root)}
    snapshot = warp_live.LaunchSnapshot(session_id, warp_live._launch_dirs(None))
    out: list[str] = []
    for run_id, stamp in sorted(candidates):
        try:
            submission = _read_submission(root, run_id)
            if not submission or submission.get("session_id") != session_id:
                continue
            claimed = run_token._record_path(root, run_id, claimed=True)
            if claimed is not None and claimed.exists():
                continue
            record = held.get(run_id) or {
                "run_id": run_id,
                "script_path": submission.get("script_path"),
                "minted_at": stamp,
                "session_id": session_id,
                "sizing_rel": submission.get("sizing_rel"),
            }
            if warp_live.run_ended_on_evidence(root, record, snapshot=snapshot):
                out.append(run_id)
        except Exception:
            continue
    return tuple(out)


def residue(
    repo_root: Path, session_id: str, pathspec: Iterable[str], *, now: Optional[float] = None
) -> StoppedResidue:
    """Dirty run-artifact candidates of each `stopped_own_runs` id, outside `pathspec`, and not
    held by a live peer. A candidate whose artifact `runs_of_artifacts` maps to a different run is
    dropped: a stopped run never carries a sibling's plan, nor a `docs/plans/` path no run owns. Unknown contest withholds every dirty
    candidate as `contest-unknown`. An unreadable index propagates (`IndexParseError`)."""
    runs = stopped_own_runs(repo_root, session_id, now=now)
    if not runs:
        return StoppedResidue({}, {})
    from coordinator_core.ops.ceremony import commit_companion
    from coordinator_core.session import scope

    root = Path(repo_root)
    spec = [_norm(p) for p in pathspec]
    cands: dict[str, set[str]] = {}
    for run_id in runs:
        paths = {_norm(p) for p in run_artifacts.candidate_paths(root, run_id).paths()}
        cands[run_id] = paths - set(spec)
    owner = run_artifacts.runs_of_artifacts(root, sorted(set().union(*cands.values())))
    for run_id, paths in cands.items():
        paths -= {
            p for p in paths
            if owner.get(p, None if p.startswith("docs/plans/") else run_id) != run_id
        }
    every = sorted(set().union(*cands.values()))
    dirty = set(commit_companion.own_left_behind(root, {session_id: every}, session_id, spec))
    if not dirty:
        return StoppedResidue({}, {})
    contested = scope.contested_by_live_peers(sorted(dirty), session_id, cwd=str(root))
    withheld: dict[str, str] = {}
    if contested is None:
        withheld = {p: "contest-unknown" for p in sorted(dirty)}
        dirty = set()
    else:
        for path in sorted(dirty):
            if contested.get(path):
                withheld[path] = "peer-held"
        dirty -= set(withheld)
    carried = {rid: tuple(sorted(paths & dirty)) for rid, paths in cands.items() if paths & dirty}
    return StoppedResidue(carried, withheld)
