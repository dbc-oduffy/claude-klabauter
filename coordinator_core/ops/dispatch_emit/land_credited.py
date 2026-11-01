"""``dispatch.land_credited``: land a run's credited paths in one CAS commit.

The credited rows' declared writes come from the run script's commit marker (one hop through the ask-run
manifest). One in-process ``scoped_changed`` over those paths, the run's own record dirs and its superseded
predecessors' record dirs names what differs from HEAD; ``land_credited_holds.holds`` names the paths to
leave out (provenance, never a refusal); ``git.commit.commit_paths`` commits the rest with zero spawns,
pinned to the blobs read here, and a lost ref race refuses with nothing written.

Checks the old ``dispatch.land_run`` ran that this op does not: the reach gate, the since-base overlap walk,
the hunk gate, the import closure and the landing test gate (see the research doc of the rebuild plan).
Does not import ``land_run``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Optional

from coordinator_core.git.commit import _EPHEMERAL_EMISSION_RE
from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import land_credited_contract as contract


class _BadRequest(ValueError):
    pass


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _safe_rel(path: str) -> str:
    from coordinator_core.ops.dispatch_emit.land_credited_holds import safe_rel

    try:
        return safe_rel(path)
    except ValueError as exc:
        raise _BadRequest(str(exc)) from exc


def _credited(root: Path, request: contract.LandCreditedRequest) -> list:
    """Declared paths of the marker the request's script carries."""
    from coordinator_core.ops.dispatch_emit.commit_request import MalformedCommitRequestError
    from coordinator_core.ops.dispatch_emit.land_credited_holds import marker_paths

    try:
        paths = marker_paths(root, request.script_path, _safe_rel)
    except (OSError, ValueError, KeyError, TypeError, MalformedCommitRequestError) as exc:
        if isinstance(exc, _BadRequest):
            raise
        raise _BadRequest(f"script {request.script_path!r} yields no readable commit marker: {exc}") from exc
    if paths is None:
        raise _BadRequest(f"script {request.script_path!r} carries no commit marker")
    return paths


def _stamp_floor(sizing_name: str) -> str:
    """Lowest run stamp that can bind ``sizing_name`` (its date prefix, a day early); ``""`` without one."""
    from datetime import date, timedelta

    m = re.match(r"(\d{4})-(\d{2})-(\d{2})-", sizing_name)
    if m is None:
        return ""
    try:
        day = date(int(m.group(1)), int(m.group(2)), int(m.group(3))) - timedelta(days=1)
    except (ValueError, OverflowError):
        return ""
    return day.strftime("%Y%m%d") + "T000000-000000"


def _superseded_roots(root: Path, run_id: str) -> list:
    """Record-dir roots of earlier runs bound to this run's sizing (requirement c): one directory listing and
    one ``run.json`` read per earlier same-window dir."""
    from coordinator_core.ops.dispatch_emit import run_artifacts
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, record_dir
    from coordinator_core.ops.dispatch_emit.run_write_set import _sizing_rel

    mine = run_artifacts._STAMP_TAIL.search(run_id)
    sizing_rel = _sizing_rel(record_dir(root, run_id))
    if mine is None or not sizing_rel:
        return []
    try:
        listing = [e.name for e in os.scandir(root / RECORD_DIR_ROOT) if e.is_dir()]
    except OSError:
        return []
    sizing_name = _norm(sizing_rel).rsplit("/", 1)[-1]
    needle = sizing_name.encode("utf-8")
    floor = _stamp_floor(sizing_name)
    candidates = []
    for name in listing:
        tail = run_artifacts._STAMP_TAIL.search(name)
        if tail is None or tail.group(0) >= mine.group(0) or tail.group(0) < floor:
            continue
        try:
            raw = (root / RECORD_DIR_ROOT / name / "run.json").read_bytes()
        except OSError:
            continue
        if needle in raw or not needle.isascii():
            candidates.append(f"{RECORD_DIR_ROOT}/{name}/run.json")
    if not candidates:
        return []
    from coordinator_core.ops.dispatch_emit import run_token

    live = {str(r["run_id"]) for r in run_token.held_records(root) if isinstance(r.get("run_id"), str)}
    names = run_artifacts.superseded_runs(root, run_id, sizing_rel, candidates, live_run_ids=live)
    return [f"{RECORD_DIR_ROOT}/{n}" for n in names]


def _differing(root: Path, run_id: str, declared: list) -> list:
    """Declared paths plus the run's own and superseded record dirs' files that differ from HEAD (a, b, c)."""
    from coordinator_core.git.scoped_changed import scoped_changed
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    roots = [r for r in (f"{RECORD_DIR_ROOT}/{run_id}", f"scratch/warp/{run_id}") if (root / r).is_dir()]
    roots += _superseded_roots(root, run_id)
    found = scoped_changed(root, declared, roots, [])
    return [p for p in _differing_unfiltered(root, declared, roots, found) if not _EPHEMERAL_EMISSION_RE.search(p)]


def _differing_unfiltered(root: Path, declared: list, roots: list, found) -> list:
    """Unfiltered differing set; without a scoped status, declared deletions tracked at HEAD ride too."""
    if found is None:
        # A gap in the scoped status: take every present file and let commit_paths settle the unchanged ones.
        walked = [
            _norm(os.path.relpath(os.path.join(dirpath, name), root))
            for r in roots
            for dirpath, _dirs, names in os.walk(root / r)
            for name in names
        ]
        present = {p for p in declared if (root / p).exists()}
        gone = [p for p in declared if p not in present]
        if gone:
            from coordinator_core.git.commit import partition_declared_deletions

            split = partition_declared_deletions(root, gone)
            present |= set(split[0]) if split else set()
        return sorted(present | set(walked))
    changed, candidates = found
    return sorted(changed | {p for p in candidates if not p.endswith("/")})


def _holds(root: Path, request: contract.LandCreditedRequest) -> dict:
    """``{path: reason}`` from the holds hook (requirements e and f)."""
    from coordinator_core.ops.dispatch_emit import land_credited_holds

    return dict(land_credited_holds.holds(root, request))


def _message(run_id: str, deleted: list) -> str:
    lines = [f"warp: land credited paths of {run_id}", "", f"Run-Id: {run_id}"]
    if deleted:
        lines.append("Removes: " + ", ".join(sorted(deleted)))
    return "\n".join(lines)


#: ``refused`` value of a pre-commit leg that ran over its own budget.
REFUSED_LAND_DEADLINE = contract.REFUSED_LAND_DEADLINE

#: Pre-commit legs in run order, each with its own process-time budget in seconds. Budgets are separate
#: allowances, never one shared pool: a slow earlier leg cannot shorten ``commit``.
PRECOMMIT_LEG_BUDGET_SECS = {"credited": 0.1, "residue": 0.15, "holds": 0.1}

#: The commit leg has no entry above: it is measured on its own clock, started after the pre-commit legs, so
#: none of them can debit it, and it never halts (a landed commit cannot be taken back).
#: ``commit_paths`` takes the index lock itself, so the lock wait is inside this leg.
COMMIT_LEG = "commit"


class _LegOver(Exception):
    def __init__(self, leg: str) -> None:
        super().__init__(leg)
        self.leg = leg


class _Legs:
    """Per-leg process time (``time.thread_time`` deltas, never wall clock; thread-scoped so a receipt thread of an earlier landing cannot debit a leg) of one landing."""

    def __init__(self) -> None:
        self.process_ms: dict = {}

    def run(self, leg: str, fn, *args):
        """Call ``fn(*args)``; record its process ms under ``leg``. A pre-commit leg over its budget raises
        ``_LegOver`` after it returns (the commit leg never does: its work cannot be taken back)."""
        import time

        start = time.thread_time()
        try:
            result = fn(*args)
        finally:
            self.process_ms[leg] = round((time.thread_time() - start) * 1000.0, 3)
        budget = PRECOMMIT_LEG_BUDGET_SECS.get(leg)
        if budget is not None and self.process_ms[leg] > budget * 1000.0:
            raise _LegOver(leg)
        return result


def _halt(root: Path, request: contract.LandCreditedRequest, legs: _Legs, over: str) -> dict:
    """The land-deadline halt: nothing committed, per-leg process ms in the reply and the run's ``land-legs.json``."""
    budget_ms = PRECOMMIT_LEG_BUDGET_SECS[over] * 1000.0
    reply = contract.refused_reply(
        REFUSED_LAND_DEADLINE,
        f"land-deadline: leg {over} used {legs.process_ms[over]}ms process time, over its {budget_ms:g}ms budget; "
        "nothing committed. Re-fire the landing.",
    )
    reply["leg"] = over
    reply["leg_timings"] = dict(legs.process_ms)
    from coordinator_core.ops.dispatch_emit import land_leg_record

    rel = land_leg_record.write(
        root, request.run_id, outcome=land_leg_record.OUTCOME_HALT, leg=over, bound_by="budget",
        legs=[{"leg": name, "process_ms": ms} for name, ms in legs.process_ms.items()],
    )
    if rel:
        reply["leg_record"] = rel
    return reply


def land(root: Path, request: contract.LandCreditedRequest) -> dict:
    """Run the landing against worktree ``root``; the reply carries ``contract.REPLY_KEYS`` plus ``leg_timings``
    (``{leg: process_ms}``, every leg that ran). A pre-commit leg over its budget halts with ``land-deadline``."""
    from coordinator_core.git.commit import CommitRefused, NothingToCommit, checkin_blob_sha, commit_paths
    from coordinator_core.ops._path_guard import safe_id

    if not safe_id(request.run_id):
        return contract.refused_reply(contract.REFUSED_BAD_REQUEST, f"run_id {request.run_id!r} is not a single safe path segment")
    legs = _Legs()
    try:
        declared = legs.run("credited", _credited, root, request)
        differing = legs.run("residue", _differing, root, request.run_id, declared)
        held = legs.run("holds", _holds, root, request)
    except _BadRequest as exc:
        return contract.refused_reply(contract.REFUSED_BAD_REQUEST, str(exc))
    except _LegOver as over:
        return _halt(root, request, legs, over.leg)

    differing_set = set(differing)
    withheld = {_norm(p): r for p, r in held.items() if _norm(p) in differing_set}
    to_land = [p for p in differing if p not in withheld]
    present = [p for p in to_land if (root / p).is_file()]
    present_set = set(present)
    deleted = [p for p in to_land if p not in present_set]
    if not to_land:
        reply = contract.refused_reply(contract.REFUSED_NOTHING_TO_LAND, "no credited path differs from HEAD outside the holds", withheld)
        reply["leg_timings"] = dict(legs.process_ms)
        return reply

    def _commit():
        expected = {p: sha for p in present if (sha := checkin_blob_sha(root, p))}
        return commit_paths(root, present, _message(request.run_id, deleted), deleted_paths=deleted, expected_blobs=expected)

    try:
        outcome = legs.run(COMMIT_LEG, _commit)
    except NothingToCommit as exc:
        reply = contract.refused_reply(contract.REFUSED_NOTHING_TO_LAND, str(exc), withheld)
    except CommitRefused as exc:
        reply = contract.refused_reply(contract.REFUSED_COMMIT, str(exc), withheld)
    else:
        landed = [p for p in to_land if p not in set(outcome.no_delta)]
        _defer_receipts(root, request, outcome.sha)
        reply = contract.landed_reply(outcome.sha, landed, withheld)
    reply["leg_timings"] = dict(legs.process_ms)
    return reply


_LOG = logging.getLogger(__name__)

#: Receipt-writer threads started by ``land``; a caller that must observe the write joins them.
DEFERRED_RECEIPT_THREADS: list = []
_THREADS_LOCK = threading.Lock()


def _write_receipts(root: Path, request: contract.LandCreditedRequest, sha: str) -> None:
    """Build and write the run's local completion receipts; any failure is logged, never raised."""
    from datetime import datetime, timezone

    from coordinator_core.completion_receipts import store
    from coordinator_core.ops.dispatch_emit.completion_receipt import build_run_receipts

    try:
        built = build_run_receipts(
            root, request, done_ids=[], incomplete_ids=[], record=None, script_path=request.script_path,
            session_id=None, base_sha=sha, branch=None,
            now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), local=True,
        )
        for fm, prose in built:
            store.write_receipt(root, fm, prose)
    except Exception as exc:  # noqa: BLE001 -- the commit is already in; a receipt failure never undoes it
        _LOG.warning("land_credited: receipt write for %s failed after commit %s: %s", request.run_id, sha, exc)


def _defer_receipts(root: Path, request: contract.LandCreditedRequest, sha: str) -> None:
    """Start the receipt write on a daemon thread; the reply never waits on it (a stalled disk hangs only the thread).

    The receipt carries no done/incomplete ids, record, session or branch: ``land`` has none. A thread lost
    with its process leaves no receipt; ``terminal_commit`` mints its own full receipt under a fresh id and
    never replaces or removes this one."""
    thread = threading.Thread(
        target=_write_receipts, args=(root, request, sha), name=f"land-credited-receipt-{request.run_id}", daemon=True,
    )
    with _THREADS_LOCK:
        DEFERRED_RECEIPT_THREADS[:] = [t for t in DEFERRED_RECEIPT_THREADS if t.is_alive()]
        DEFERRED_RECEIPT_THREADS.append(thread)
    thread.start()


@register_op(contract.OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Land a run's credited paths: params ``run_id``, ``plan_path`` and ``script_path`` (all required);
    reply keys are ``land_credited_contract.REPLY_KEYS``."""
    request = contract.parse_request(params)
    if isinstance(request, str):
        return contract.refused_reply(contract.REFUSED_BAD_REQUEST, request)
    if repo_root is None:
        return contract.refused_reply(contract.REFUSED_BAD_REQUEST, f"{contract.OP_NAME} requires a common_dir-keyed dispatch; repo_root was not supplied")
    from coordinator_core.git.call_io_scope import call_io_scope
    from coordinator_core.lifecycle import main_worktree_root

    with call_io_scope():
        return land(main_worktree_root(repo_root), request)
