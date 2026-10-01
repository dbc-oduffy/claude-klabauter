"""
coordinator_core.ops.fanout.ops — ops "fanout.compose", "fanout.census", "fanout.reconcile".

Purpose: expose the pure fan-out functions (compose, census, reconcile) over JSON-RPC.
Each handler maps params to one pure function; `repo_root` is ignored. Failures return
{"error": <str>}.

Negative spec: no file, process or network access beyond the cached schema load in `contract`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fanout import census as census_mod
from coordinator_core.ops.fanout import compose as compose_mod
from coordinator_core.ops.fanout import contract
from coordinator_core.ops.fanout import reconcile as reconcile_mod


def _guarded(op: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (ValueError, KeyError, TypeError) as exc:
        return {"error": f"{op}: {exc}"}


@register_op("fanout.compose")
def _compose(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: manifest (object or YAML string). Returns {job_id, actions[]}."""
    return _guarded("fanout.compose", lambda: compose_mod.compose(params.get("manifest")))


@register_op("fanout.census")
def _census(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: manifest, sessions, session_details (optional), checkin_comments (optional)."""

    def run() -> Any:
        manifest = contract.validate_manifest(params.get("manifest"))
        return census_mod.census(
            manifest,
            params.get("sessions"),
            params.get("session_details"),
            params.get("checkin_comments"),
        )

    return _guarded("fanout.census", run)


@register_op("fanout.reconcile")
def _reconcile(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: manifest, census (a fanout.census result), pause (optional {message})."""

    def run() -> Any:
        manifest = contract.validate_manifest(params.get("manifest"))
        census = params.get("census")
        if not isinstance(census, dict):
            raise ValueError("census must be an object")
        return reconcile_mod.reconcile(manifest, census, params.get("pause"))

    return _guarded("fanout.reconcile", run)
