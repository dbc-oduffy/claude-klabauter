"""``dispatch.close_halted_run_rejected``: closes a halted run whose withheld edits the EM deliberately rejects.

Params: ``run_id`` (string, required), ``rejected_paths`` (non-empty list of withheld paths the EM rejects,
required unless the run withholds nothing: then it may be omitted and the run closes with empty
dispositions and no git spawn), ``reason`` (non-empty string, required, never defaulted). Fail-closed: the
marker is ``halted`` or ``resumed``, no live successor holds the run, and every withheld path classifies: a rejected path is ``rejected`` when HEAD's blob
equals its blob at the halt anchor (``landed`` when HEAD already carries the kept blob instead); a path not named
rejected must already be ``landed``/``lost``/``scratch-abandoned`` as ``close_halted_run_superseded`` classifies
them. Any other path refuses naming it; marker and index stay unchanged. On success the marker is rewritten
``status: closed`` with ``closed: {reason, at, disposition: rejected}`` and ``path_dispositions`` (every path), the
run leaves the halt-withheld index and its parent ask ends as the superseded close ends it.
Two git spawns: ``rev-list`` (anchor), ``cat-file --batch-check`` (HEAD, anchor and kept blobs).
A successful close then lands one scoped closure commit of the run's record files with ``Checkpoint-Run-Closed:``
trailers (``halted_run_close_commit.commit_close_records``): ``add`` and ``commit`` spawns beyond the two above, none on a
refusal; a refused commit never fails the close.
Reply: ``{ok, run_id, dispositions, reason, recorded, records_commit?, records_commit_error?}`` or ``{ok: false, error}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit.halted_run_superseded import (
    _blob_ids, _classify_paths, _end_parent_ask, _halt_anchor, _land_closure, _newest_touch, _prints_plan, _refuse,
)

OP_NAME = "dispatch.close_halted_run_rejected"
DISPOSITION_REJECTED = "rejected"
# A resumed run whose successor also halted is as closable as a halted one; a live successor still refuses.
_CLOSABLE = ("halted", "resumed")


def _params(params: object) -> tuple:
    """``(run_id, rejected, reason, None)`` or ``(None, None, None, refusal)``."""
    from coordinator_core.ops._path_guard import safe_id

    params = params if isinstance(params, dict) else {}
    run_id, raw, reason = params.get("run_id"), params.get("rejected_paths"), params.get("reason")
    if not isinstance(run_id, str) or not run_id or not safe_id(run_id):
        return None, None, None, _refuse(f"{OP_NAME} requires a safe string run_id")
    if isinstance(raw, str):
        raw = [raw]
    if raw is None:
        raw = []
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        return None, None, None, _refuse(f"{OP_NAME} requires rejected_paths: a list of withheld paths")
    if not isinstance(reason, str) or not reason.strip():
        return None, None, None, _refuse(f"{OP_NAME} requires a non-empty reason")
    return run_id, sorted(set(raw)), reason.strip(), None


def _mark_rejected(record: Path, expected: dict, reason: str, dispositions: dict) -> bool:
    """Rewrite the marker as ``closed`` (rejected) only if it still equals ``expected`` and is ``halted`` or ``resumed``."""
    from coordinator_core.ops.dispatch_emit import halted_run

    marker = halted_run._read(record)
    if marker is None or marker != expected or marker.get("status") not in _CLOSABLE:
        return False
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    halted_run._write(record, {
        **marker, "status": halted_run.STATUS_CLOSED,
        "closed": {"reason": reason, "at": at, "disposition": DISPOSITION_REJECTED},
        halted_run.PATH_DISPOSITIONS_FIELD: dispositions,
    })
    halted_run._live_index_drop(record)
    halted_run._record_judge(record)
    return True


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.close_halted_run_rejected": verify HEAD still equals the halt anchor, then close the run."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_hold_release, halted_run, halted_withheld_index
    from coordinator_core.ops.dispatch_emit.halted_run_landed import _live_successor

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    run_id, rejected, reason, bad_params = _params(params)
    if bad_params:
        return bad_params
    root = main_worktree_root(Path(repo_root))
    try:
        record = ask_contract.record_dir(root, run_id)
        marker = halted_run.read_strict_run(record)
        withheld = halted_run.halted_withheld_strict(record)
    except halted_run.UnreadableHaltRecord as exc:
        return _refuse(f"{exc.reason}: halt marker unreadable; {run_id} is not closed rejected")
    except ValueError as exc:
        return _refuse(str(exc))
    if marker is None:
        return _refuse(f"{run_id} has no halt record; nothing to reject")
    if marker.get("status") not in _CLOSABLE:
        return _refuse(f"{run_id} is {marker.get('status')}; only a halted or resumed run closes rejected")
    successor = _live_successor(root, run_id)
    if successor:
        return _refuse(f"{run_id} has a live successor {successor}; wait for it or stop it")
    if withheld and not rejected:
        return _refuse(f"{OP_NAME} requires rejected_paths: a non-empty list of withheld paths")
    stray = sorted(p for p in rejected if p not in withheld)
    if stray:
        return _refuse(f"{stray[0]} is not a withheld path of {run_id}; it cannot be rejected")
    if not withheld:
        if not _mark_rejected(record, marker, reason, {}):
            return _refuse(f"{run_id} halt marker changed under the write; retry")
        halted_withheld_index.remove_run(root, run_id)
        _end_parent_ask(root, run_id, marker.get("sizing_rel"))
        return {
            "ok": True, "run_id": run_id, "dispositions": {}, "reason": reason,
            "recorded": (record / halted_run.HALTED_FILE).relative_to(root).as_posix(),
        }
    plan = halted_hold_release._kept_plan(record) or _prints_plan(record, withheld)
    kept = plan[1] if plan else {}
    paths = sorted(withheld)
    anchor, why = _halt_anchor(root, run_id)
    if anchor is None:
        return _refuse(why)
    kept_paths = [p for p in paths if p in kept]
    ids = _blob_ids(
        root, [f"HEAD:{p}" for p in paths] + [f"{anchor}:{p}" for p in paths] + [kept[p] for p in kept_paths],
    )
    if ids is None:
        return _refuse("git cat-file failed; retry")
    n = len(paths)
    head, base = dict(zip(paths, ids[:n])), dict(zip(paths, ids[n:2 * n]))
    keep = dict(zip(kept_paths, ids[2 * n:]))
    dispositions, bad = {}, []
    others = [p for p in paths if p not in rejected]
    if others:
        missing = [p for p in others if p not in kept]
        if missing:
            bad.append(f"{missing[0]} (no kept blob and not named rejected)")
        # A hand-landed edit whose blob differs from the kept one (re-edited, eol-normalised) is superseded, not
        # unclassifiable: the post-halt log is read only when some unrejected path is not byte-landed.
        pending = [p for p in others if p in kept and head[p] != keep.get(p)]
        touched = _newest_touch(root, anchor, pending) if pending else {}
        if touched is None:
            return _refuse("git log failed; retry")
        done, unclassified = _classify_paths([p for p in others if p in kept], touched, head, keep, base, anchor)
        dispositions.update(done)
        bad += unclassified
    for p in rejected:
        if head[p] == base[p]:
            dispositions[p] = {"disposition": DISPOSITION_REJECTED, "blob": head[p]}
        elif head[p] is not None and head[p] == keep.get(p):
            dispositions[p] = {"disposition": halted_run.DISPOSITION_LANDED, "blob": head[p]}
        else:
            bad.append(f"{p} (HEAD differs from the halt anchor {anchor[:12]} blob; it was changed after the halt)")
    if bad:
        return _refuse(f"cannot close {run_id} rejected, {len(bad)} withheld path(s) unresolved: " + "; ".join(sorted(bad)))
    if not _mark_rejected(record, marker, reason, dispositions):
        return _refuse(f"{run_id} halt marker changed under the write; retry")
    halted_withheld_index.remove_run(root, run_id)
    _end_parent_ask(root, run_id, marker.get("sizing_rel"))
    reply = {
        "ok": True, "run_id": run_id, "dispositions": dispositions, "reason": reason,
        "recorded": (record / halted_run.HALTED_FILE).relative_to(root).as_posix(),
    }
    return _land_closure(root, record, run_id, reply, OP_NAME, "rejected")
