"""``dispatch.run_terminality``: read-only answer to "is this ``state/warp`` run dir done, or still owed work?".

Params: ``run_dir`` (string, required: repo-relative, either slash; its parent must be the warp record root
``ask_contract.RECORD_DIR_ROOT`` and it must be an existing directory).
Reply: ``{ok, run_id, terminal, resumable, reason}`` or ``{ok: false, error}``. Writes nothing, spawns nothing; at
most two file reads (``halted.json``, then ``run-outcome.json`` when no marker exists).
Classification: a ``halted.json`` status of ``landed`` / ``superseded`` / ``closed`` is terminal; ``halted`` is
resumable (released or not); ``resumed`` and an unreadable marker are neither. With no marker, a recorded
``objective_loop.outcome`` in ``land_run._RECORDED_EXIT_OUTCOMES`` is terminal. Anything else, unknown statuses
included, is neither terminal nor resumable: the caller keeps the dir.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.ipc import register_op

OP_NAME = "dispatch.run_terminality"


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _reply(run_id: str, terminal: bool, resumable: bool, reason: str) -> dict:
    return {"ok": True, "run_id": run_id, "terminal": terminal, "resumable": resumable, "reason": reason}


def _resolve_run_dir(root: Path, run_dir: str) -> tuple:
    """``(path, None)`` for an existing directory directly under the warp record root, else ``(None, reason)``."""
    from coordinator_core.ops.dispatch_emit import ask_contract

    rel = PurePosixPath(run_dir.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts:
        return None, f"run_dir {run_dir!r} must be a repo-relative path under {ask_contract.RECORD_DIR_ROOT}"
    target = (root / rel).resolve()
    base = (root / ask_contract.RECORD_DIR_ROOT).resolve()
    if target.parent != base or target == base:
        return None, f"run_dir {run_dir!r} is not a direct child of {ask_contract.RECORD_DIR_ROOT}"
    if not target.is_dir():
        return None, f"run_dir {run_dir!r} is not a directory"
    return target, None


def _classify_marker(run_id: str, marker: dict) -> dict:
    from coordinator_core.ops.dispatch_emit import halted_run

    status = marker.get("status")
    if status in (halted_run.STATUS_LANDED, halted_run.STATUS_SUPERSEDED, halted_run.STATUS_CLOSED):
        return _reply(run_id, True, False, f"halt marker {status}")
    if status == halted_run.STATUS_HALTED:
        if marker.get(halted_run.HALT_RELEASED_FIELD):
            return _reply(run_id, False, True, "halt released; still resumable")
        return _reply(run_id, False, True, "live halt")
    if status == halted_run.STATUS_RESUMED:
        return _reply(run_id, False, False, "resumed by a successor run")
    return _reply(run_id, False, False, "unrecognised halt marker status")


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.run_terminality": classify one warp run dir as terminal, resumable or neither."""
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
    from coordinator_core.ops.dispatch_emit.land_run import _RECORDED_EXIT_OUTCOMES

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    run_dir = params.get("run_dir")
    if not isinstance(run_dir, str) or not run_dir.strip():
        return _refuse(f"{OP_NAME} requires a string run_dir")
    target, why = _resolve_run_dir(Path(repo_root), run_dir.strip())
    if target is None:
        return _refuse(why)
    run_id = target.name
    try:
        marker = halted_run.read_strict(target)
    except ValueError:
        return _reply(run_id, False, False, "unreadable halt marker")
    if marker is not None:
        return _classify_marker(run_id, marker)
    recorded = (ask_contract.read_run_outcome(Path(repo_root), run_id) or {}).get("objective_loop")
    outcome = recorded.get("outcome") if isinstance(recorded, dict) else None
    if outcome in _RECORDED_EXIT_OUTCOMES:
        return _reply(run_id, True, False, f"run outcome {outcome}")
    return _reply(run_id, False, False, "no halt marker or terminal outcome")
