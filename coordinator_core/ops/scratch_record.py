"""coordinator_core.ops.scratch_record -- ops "scratch.record_run_path" and "scratch.keep": thin
wrappers over ``dispatch_emit.run_scratch`` for scratch creators outside the engine.

``record_run_path`` lists scratch paths in the run's ledger so the run-end reaper may remove them;
``keep`` holds one path past run end under a named purpose and a TTL. Neither lists a directory or
spawns a process; a refused path or keep returns ``{"error": ...}`` and writes nothing.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.ops.scratch_delete import _resolve_root

RECORD_OP_KEY = "scratch.record_run_path"
KEEP_OP_KEY = "scratch.keep"


def _run_id_refusal(op: str, run_id) -> str:
    if not isinstance(run_id, str) or not run_id:
        return f"{op}: run_id must be a non-empty string"
    return ""


@register_op(RECORD_OP_KEY)
def _record_handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "scratch.record_run_path": params `run_id`, `paths` (list of repo-relative scratch/ paths)."""
    from coordinator_core.ops.dispatch_emit import run_scratch

    run_id = params.get("run_id")
    paths = params.get("paths")
    refusal = _run_id_refusal(RECORD_OP_KEY, run_id)
    if refusal:
        return {"error": refusal}
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        return {"error": f"{RECORD_OP_KEY}: paths must be a non-empty list of strings"}
    root = _resolve_root(params.get("repo_root"), repo_root, run_id)
    if not root:
        return {"error": f"{RECORD_OP_KEY}: no repo root"}
    try:
        recorded = run_scratch.record_paths(Path(root), run_id, paths)
    except (ValueError, OSError) as exc:
        return {"error": f"{RECORD_OP_KEY}: {exc}"}
    return {"recorded": recorded}


@register_op(KEEP_OP_KEY)
def _keep_handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC "scratch.keep": params `run_id`, `path`, `purpose` (non-empty), `ttl_hours` (> 0)."""
    from coordinator_core.ops.dispatch_emit import run_scratch

    run_id = params.get("run_id")
    refusal = _run_id_refusal(KEEP_OP_KEY, run_id)
    if refusal:
        return {"error": refusal}
    root = _resolve_root(params.get("repo_root"), repo_root, run_id)
    if not root:
        return {"error": f"{KEEP_OP_KEY}: no repo root"}
    try:
        kept = run_scratch.declare_keep(
            Path(root), run_id, params.get("path"), params.get("purpose"), params.get("ttl_hours")
        )
    except (ValueError, OSError) as exc:
        return {"error": f"{KEEP_OP_KEY}: {exc}"}
    return {"kept": kept}
