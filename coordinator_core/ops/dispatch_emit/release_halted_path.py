"""``dispatch.release_halted_path``: the sanctioned per-path release of one halted run's hold.

Params: ``{"run_id": str, "path": str, "reason": str, "session_id"?: str}``; ``reason`` is required
and non-blank. Reply on success: ``{ok, run_id, path, released, reason}``; a repeat release is
``released: false`` and echoes the FIRST recorded reason. The path moves out of the marker's
``run_artifacts.withheld`` into ``released_paths[path] = {reason, session, at}``; ``status`` stays
``halted`` and every other withheld path keeps holding. A path is accepted when the run withholds it or
its ``manifest.json`` declares it in a row's ``writes``. Reads one run's marker and manifest, writes the
marker through the halted_run writer; no spawn, no git call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

OP_NAME = "dispatch.release_halted_path"


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def release_path(root: Path, run_id: str, path: str, reason: str, session_id: str = "") -> dict:
    """Release `path` from halted `run_id`'s holds, recording `reason`; the op reply dict. Never raises."""
    if not isinstance(reason, str) or not reason.strip():
        return {"ok": False, "error": "reason must be a non-empty string; say why this path stops being held"}
    rel = _norm(path) if isinstance(path, str) else ""
    if not rel:
        return {"ok": False, "error": "path must be a non-empty string; pass {\"path\": \"<repo-relative path>\"}"}
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
        from coordinator_core.ops.dispatch_emit.halted_hold_release import RELEASED_PATHS_FIELD
        from coordinator_core.ops.dispatch_emit.halted_withheld_index import _manifest_declared

        rdir = record_dir(Path(root), run_id)
        marker = halted_run.read_strict(rdir)
        if not marker or marker.get("status") != halted_run.STATUS_HALTED:
            return {"ok": False, "error": f"run {run_id} is not halted; only a halted run's hold can be released"}
        released = marker.get(RELEASED_PATHS_FIELD)
        released = dict(released) if isinstance(released, dict) else {}
        first = released.get(rel)
        if isinstance(first, dict):
            return {"ok": True, "run_id": run_id, "path": rel, "released": False, "reason": first.get("reason")}
        artifacts = marker.get("run_artifacts") if isinstance(marker.get("run_artifacts"), dict) else {}
        withheld = artifacts.get("withheld") if isinstance(artifacts.get("withheld"), dict) else {}
        keys = [k for k in withheld if _norm(k) == rel]
        if not keys and rel not in {_norm(p) for p in _manifest_declared(rdir)}:
            return {"ok": False, "error": f"run {run_id} neither withholds nor declares {rel}"}
        released[rel] = {
            "reason": reason.strip(),
            "session": session_id,
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        marker["run_artifacts"] = {**artifacts, "withheld": {k: v for k, v in withheld.items() if k not in keys}}
        marker[RELEASED_PATHS_FIELD] = released
        halted_run._write(rdir, marker)
        return {"ok": True, "run_id": run_id, "path": rel, "released": True, "reason": reason.strip()}
    except Exception as exc:  # noqa: BLE001 -- unreadable record or bad run id: the hold stands
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.release_halted_path": release one path of one halted run, with a reason."""
    if repo_root is None:
        return {"ok": False, "error": f"{OP_NAME} requires repo_root"}
    from coordinator_core.lifecycle import main_worktree_root

    p = params if isinstance(params, dict) else {}
    run_id = p.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return {"ok": False, "error": "run_id must be a non-empty string; pass {\"run_id\": \"<id>\"}"}
    session = p.get("session_id")
    return release_path(
        main_worktree_root(Path(repo_root)), run_id, p.get("path"), p.get("reason"),
        session if isinstance(session, str) else "",
    )
