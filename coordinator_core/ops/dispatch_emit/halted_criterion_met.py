"""halted_criterion_met names which halted runs have already met their exit criterion at HEAD.

Two definitions. A halted run's objective commit is a commit reachable from HEAD that is a run
landing (``checkpoint_stamp.is_landing_message``) and names the run by structured naming: its
``Inline-Review:`` line holds the run id, its subject ends ``(run <run id>)``, or it carries
``Deliverable-Id: <the run sizing's deliverable_id>``. Prose mentions never count. A run's own hunks
are the paths its halt marker records as withheld; they are clean when none is in the dirty set.

Invariants: at most one ``run_git`` call per invocation, zero with no holders; ``dirty`` is called
at most once, and only when a holder has an objective commit and a non-empty withheld set; it never
raises, and any read error holds (the run is absent from the result).

Stdlib only at module top: land_run and terminal_commit have cold-import pins, so every reader is
imported lazily.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

_FIELD = "\x1f"
_RECORD = "\x1e"


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def _holders(root: Path, run_ids: Iterable[str]) -> dict:
    """``{run_id: (needles, withheld, since)}`` for every holder with a stamp; others hold."""
    from coordinator_core.ops import warp_run_deliverable as wrd
    from coordinator_core.ops.dispatch_emit import stopped_runs

    out = {}
    for run_id in run_ids:
        since = stopped_runs._stamp_epoch(run_id)
        if since is None:
            continue
        deliverable = wrd.sizing_scalar(root, wrd.record_sizing_rel(root, run_id), "deliverable_id")
        withheld = {_norm(p) for p in wrd.halted_withheld_paths(root, run_id)}
        out[run_id] = (deliverable, withheld, since)
    return out


def _names(body: str, run_id: str, deliverable: str) -> bool:
    lines = body.splitlines()
    subject = lines[0].strip() if lines else ""
    if subject.endswith(f"(run {run_id})"):
        return True
    for line in lines:
        if line.startswith("Inline-Review:") and run_id in line:
            return True
        if deliverable and line.startswith("Deliverable-Id:") and line.split(":", 1)[1].strip() == deliverable:
            return True
    return False


def met_at_head(root: Path, run_ids: Iterable[str], dirty: Callable[[], set]) -> dict:
    """``{run_id: landing sha}`` for each halted run whose landing commit is reachable from HEAD and
    whose withheld paths are clean; every other run is absent. Never raises."""
    try:
        return _met_at_head(Path(root), list(run_ids), dirty)
    except Exception:  # noqa: BLE001 -- any read error holds
        return {}


def _met_at_head(root: Path, run_ids: list, dirty: Callable[[], set]) -> dict:
    holders = _holders(root, run_ids)
    if not holders:
        return {}
    from coordinator_core.git.run import run_git
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import is_landing_message

    needles = []
    for run_id, (deliverable, _w, _s) in holders.items():
        needles.extend(n for n in (run_id, deliverable) if n and n not in needles)
    since = min(s for _d, _w, s in holders.values())
    result = run_git([
        "-C", str(root), "log", f"--since=@{int(since)}", "-F",
        *[f"--grep={n}" for n in needles], f"--format=%H{_FIELD}%B{_RECORD}", "HEAD",
    ])
    if result.returncode != 0:
        return {}
    landings = []
    for record in result.stdout.split(_RECORD):
        sha, sep, body = record.strip("\n").partition(_FIELD)
        if sep and is_landing_message(body):
            landings.append((sha.strip(), body))
    shas = {}
    for run_id, (deliverable, _w, _s) in holders.items():
        sha = next((s for s, body in landings if _names(body, run_id, deliverable)), "")
        if sha:
            shas[run_id] = sha
    needs_dirty = [r for r in shas if holders[r][1]]
    if needs_dirty:
        dirty_set = {_norm(p) for p in dirty()}
        shas = {r: s for r, s in shas.items() if not (holders[r][1] & dirty_set)}
    return shas
