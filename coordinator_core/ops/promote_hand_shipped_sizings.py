"""Sweep leg of `promote_shipped_in_flight_stubs`: ships a sizing whose warp run was finished by
hand-commit.

A run that never reached its terminal commit leaves its sizing `routed` and its run dir without
`reconciled.json`. When the commits since the run's submission cover every path the run's
`commit-request.txt` declared, the work landed and this leg closes the record: `sizing.ship` flips
the sizing, then `warp_run_deliverable.write_reconciled` leaves the run-local marker.

A `routed` sizing no run references is shipped when a commit since the sizing's mtime carries a
`Deliverable-Id` equal to its own; it leaves no `reconciled.json` because it has no run dir.

Invariants: one `git log` spawn for the whole sweep, none when no run is a candidate; a commit
counts for a run only at or after its `submitted_at`, by `Deliverable-Id` equal to the sizing's or
by the submitting `Session-Id`; a run declaring no writes is never promoted. The sizing is never
written directly, and a `sizing.ship` refusal is counted, never forced.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List

from coordinator_core.git.run import run_git
from coordinator_core.ops.warp_run_deliverable import (
    RECONCILED_FILE,
    RunRecord,
    _norm,
    run_records,
    sizing_scalar,
    write_reconciled,
)
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.warm.entry_seam import reentrant_dispatch

import coordinator_core.ops.sizing_ship  # noqa: F401 -- registers "sizing.ship"
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

_PROG = "promote-hand-shipped-sizings"
_SHIPPABLE = frozenset({"routed", "sized"})
_REC = "\x1e"
_FLD = "\x1f"
_FORMAT = f"--format={_REC}%H{_FLD}%ct{_FLD}%(trailers:key=Session-Id,valueonly,separator=%x2c){_FLD}" \
    f"%(trailers:key=Deliverable-Id,valueonly,separator=%x2c){_FLD}"


def _commits_since(repo_root: Path, since: float) -> List[dict]:
    """Commits on HEAD at or after `since` as `{sha, ct, sessions, dids, paths}`; `[]` when git fails."""
    result = run_git(
        ["-C", str(repo_root), "log", f"--since={int(since)}", _FORMAT, "--name-only"]
    )
    if not result.ok:
        return []
    commits: List[dict] = []
    for record in result.stdout.replace("\r", "").split(_REC):
        parts = record.split(_FLD, 4)
        if len(parts) < 5:
            continue
        try:
            ct = int(parts[1])
        except ValueError:
            continue
        commits.append(
            {
                "sha": parts[0].strip(),
                "ct": ct,
                "sessions": {s.strip() for s in parts[2].split(",") if s.strip()},
                "dids": {d.strip() for d in parts[3].split(",") if d.strip()},
                "paths": {_norm(p.strip()) for p in parts[4].splitlines() if p.strip()},
            }
        )
    return commits


def _ship(repo_root: Path, sizing_rel: str) -> str:
    """Empty string when `sizing.ship` accepted, else the refusal text."""
    try:
        reply = reentrant_dispatch("sizing.ship", {"sizing_path": sizing_rel}, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 -- counted, never forced
        return repr(exc)
    if not isinstance(reply, dict) or reply.get("exit_code") != 0:
        return str(reply.get("error") if isinstance(reply, dict) else reply)
    return ""


def run(repo_root: Path) -> Dict[str, object]:
    """Reconcile every hand-shipped run under `repo_root`; returns counters
    `{candidates, promoted, refused, no_declared_writes, lines}` where `lines` are advisory text."""
    root = Path(repo_root)
    counters: Dict[str, object] = {
        "candidates": 0, "promoted": 0, "refused": 0, "no_declared_writes": 0, "lines": [],
    }
    lines: List[str] = counters["lines"]  # type: ignore[assignment]
    candidates: List[tuple] = []
    run_sizings = set()
    for record in run_records(root):
        if record.sizing_rel:
            run_sizings.add(_norm(record.sizing_rel))
        if record.reconciled or not record.sizing_rel or record.submitted_at <= 0:
            continue
        if sizing_scalar(root, record.sizing_rel, "status") not in _SHIPPABLE:
            continue
        deliverable_id = sizing_scalar(root, record.sizing_rel, "deliverable_id")
        if not deliverable_id:
            continue
        if not record.writes:
            counters["no_declared_writes"] += 1
            lines.append(f"{_PROG}: run {record.run_id} declares no writes; not promoted")
            continue
        candidates.append((record, deliverable_id))
    runless = _runless_sizings(root, run_sizings)
    counters["candidates"] = len(candidates) + len(runless)
    if not candidates and not runless:
        return counters

    since = [r.submitted_at for r, _ in candidates] + [m for _, _, m in runless]
    commits = _commits_since(root, min(since))
    for sizing_rel, deliverable_id, mtime in runless:
        shas = [c["sha"] for c in commits if c["ct"] >= mtime and deliverable_id in c["dids"]]
        if not shas:
            continue
        refusal = _ship(root, sizing_rel)
        if refusal:
            counters["refused"] += 1
            lines.append(f"{_PROG}: sizing.ship refused {sizing_rel}: {refusal}")
            continue
        declare_write(str((root / sizing_rel).resolve()))
        counters["promoted"] += 1
        lines.append(
            f"{_PROG}: shipped {sizing_rel} (no run record; hand-commit "
            f"{', '.join(s[:8] for s in shas)} carries its Deliverable-Id)"
        )
    for record, deliverable_id in candidates:
        matched = _qualifying(record, deliverable_id, commits)
        covered = set().union(*(c["paths"] for c in matched)) if matched else set()
        if not matched or not set(record.writes) <= covered:
            continue
        refusal = _ship(root, record.sizing_rel)
        if refusal:
            counters["refused"] += 1
            lines.append(f"{_PROG}: sizing.ship refused {record.sizing_rel}: {refusal}")
            continue
        declare_write(str((root / record.sizing_rel).resolve()))
        shas = [c["sha"] for c in matched]
        write_reconciled(root / RUN_DIR_ROOT / record.run_id, record.sizing_rel, deliverable_id, shas)
        counters["promoted"] += 1
        lines.append(
            f"{_PROG}: shipped {record.sizing_rel} (run {record.run_id} finished by hand-commit "
            f"{', '.join(s[:8] for s in shas)}; {RECONCILED_FILE} written)"
        )
    return counters


def _runless_sizings(root: Path, run_sizings: set) -> List[tuple]:
    """`(sizing_rel, deliverable_id, mtime)` for each shippable sizing no warp run references."""
    found: List[tuple] = []
    for path in sorted((root / "state" / "sizings").glob("*.yaml")):
        rel = _norm(path.relative_to(root).as_posix())
        if rel in run_sizings or sizing_scalar(root, rel, "status") not in _SHIPPABLE:
            continue
        deliverable_id = sizing_scalar(root, rel, "deliverable_id")
        if deliverable_id:
            found.append((rel, deliverable_id, path.stat().st_mtime))
    return found


def _qualifying(record: RunRecord, deliverable_id: str, commits: List[dict]) -> List[dict]:
    return [
        c for c in commits
        if c["ct"] >= record.submitted_at
        and (deliverable_id in c["dids"] or (record.session_id and record.session_id in c["sessions"]))
    ]


def report(counters: Dict[str, object]) -> None:
    """Print the advisory lines `run` collected, to stdout."""
    for line in counters["lines"]:  # type: ignore[union-attr]
        print(line, file=sys.stdout)
