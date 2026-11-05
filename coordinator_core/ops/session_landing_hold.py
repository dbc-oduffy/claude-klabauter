"""
coordinator_core.ops.session_landing_hold -- JSON-RPC "session.landing_hold".

Purpose: set, clear or inspect a session's landing hold (`session.landing_hold`). While held, the
halted-record commit and both row-mark commits defer instead of landing. `clear` turns the hold
off, then replays each deferred landing in deferral order through the entry point that deferred it.

Registered in `coordinator_core/ops/__init__.py`'s registration list, `_registry_map.py`'s
`OP_MODULE_MAP`, `op_scopes.py` (scope "common_dir", like the commits it replays) and
`authz/classification.py` (MUTATING).

Negative-spec:
    - Never raises: an invalid param is `{"error": ...}`.
    - Never drops a landing: a replay that fails stays in `<sid>.replay-failed.json` beside the hold
      record (the hold itself is off) and the next `clear` retries it first.
    - Denies nothing: the hold is advisory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ipc import register_op
from coordinator_core.session import landing_hold
from coordinator_core.session.machinery_paths import machinery_root, safe_session_id

_FAILED_SUFFIX = ".replay-failed.json"


def _failed_path(root: str, sid: str) -> str:
    return os.path.join(machinery_root(root), "landing-hold", sid + _FAILED_SUFFIX)


def _read_failed(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _store_failed(path: str, entries: list) -> None:
    if entries:
        atomic_write_bytes(path, json.dumps(entries, indent=2).encode("utf-8"))
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _replay_one(entry: dict, repo_root: Path, worktree_root: Path) -> Optional[str]:
    """Land one deferred entry. Returns its sha (or "" when nothing committed); raises on failure."""
    kind = entry.get("kind")
    params = entry.get("params")
    if not isinstance(params, dict):
        raise ValueError(f"deferred {kind!r} entry carries no params")
    if kind == "checkpoint_stamp":
        from coordinator_core.ops.dispatch_emit.checkpoint_stamp import _land_stamp

        return _land_stamp(worktree_root, list(params["paths"]), params["message"], params.get("session_id")) or ""
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    if kind == "halted_record":
        reply = reentrant_dispatch("dispatch.terminal_commit", params, repo_root=repo_root)
    elif kind == "coded_stamp":
        reply = reentrant_dispatch("ceremony.commit_v2", params, repo_root=repo_root)
    else:
        raise ValueError(f"unknown deferred kind {kind!r}")
    if not isinstance(reply, dict) or reply.get("error") or reply.get("committed") is False:
        detail = reply.get("error") if isinstance(reply, dict) else reply
        raise RuntimeError(f"{kind} replay did not land: {detail!r}")
    return reply.get("sha") or ""


@register_op("session.landing_hold")
def _session_landing_hold(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "session.landing_hold" handler.

    Params:
        action (str) -- "set", "clear" or "status".
        session_id (str) -- required, path-safe.

    Returns:
        set:    {"action": "set", "held": True}
        status: {"action": "status", "held": bool, "deferred_count": int}
        clear:  {"action": "clear", "cleared": bool, "replayed": [{"kind", "sha"}],
                 "failed": [{"kind", "error"}]}
        or {"error": str} on an invalid param.
    """
    p = params if isinstance(params, dict) else {}
    action = p.get("action")
    sid = p.get("session_id")
    if action not in ("set", "clear", "status"):
        return {"error": "action must be 'set', 'clear' or 'status'"}
    if not isinstance(sid, str) or not safe_session_id(sid):
        return {"error": "session_id (non-empty, path-safe str) is required"}
    try:
        from coordinator_core.lifecycle import main_worktree_root

        common = Path(repo_root) if repo_root is not None else Path.cwd()
        worktree_root = main_worktree_root(common)
        root = str(worktree_root)
        failed_path = _failed_path(root, sid)
        if action == "set":
            landing_hold.set_hold(root, sid)
            return {"action": "set", "held": True}
        if action == "status":
            return {
                "action": "status", "held": landing_hold.is_held(root, sid),
                "deferred_count": len(landing_hold.pending(root, sid)) + len(_read_failed(failed_path)),
            }
        was_held = landing_hold.is_held(root, sid)
        entries = _read_failed(failed_path) + landing_hold.clear_hold(root, sid)
        _store_failed(failed_path, entries)
        replayed: list = []
        failed: list = []
        kept: list = []
        for entry in entries:
            try:
                sha = _replay_one(entry, common, worktree_root)
                replayed.append({"kind": entry.get("kind"), "sha": sha})
            except Exception as exc:  # noqa: BLE001 -- a failed replay is kept, never dropped
                kept.append(entry)
                failed.append({"kind": entry.get("kind"), "error": repr(exc)})
        _store_failed(failed_path, kept)
        return {"action": "clear", "cleared": was_held, "replayed": replayed, "failed": failed}
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        return {"error": f"{type(exc).__name__}: {exc}"}
