"""
coordinator_core.ops.group_em_standing -- JSON-RPC "groupem.standing".

Purpose: read-only "who holds this repo's Group-EM nomination, is that holder
live, and is the watch ticking" in one call. The nomination leg is
`nomination.who` (or `nomination.standing` when `peer` is given), which reads
liveness through `nomination.is_live` -- the same join `claim()` uses, so the
read and the claim cannot disagree. The watch leg is
`watch_heartbeat.read_liveness`.

Registered in `coordinator_core/ops/__init__.py`'s registration list,
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`,
`coordinator_core/op_scopes.py` (scope "none") and
`coordinator_core/authz/classification.py` (COMPUTE_ONLY). Enrolled in
`_BUDGETED_ENTRYPOINTS` of the spawn audit with an empty reachable spawn set.

Negative-spec:
    - Never claims, stamps, locks or arms anything; never imports
      `watch_heartbeat.stamp`, `nomination.claim`, or `atomic_record.holder_lock`.
    - Never computes liveness itself: no `session_registry.liveness_annotation`.
    - Never raises: a failing leg is `None` plus a `<leg>_error` string.

Spec: docs/plans/2026-10-01-groupem-standing-op.md (C1).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from coordinator_core.group_em import nomination, watch_heartbeat
from coordinator_core.ipc import register_op


@register_op("groupem.standing")
def _groupem_standing(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.standing" handler.

    Params:
        repo_root (str, optional) -- defaults to the caller's `os.getcwd()`,
            matching `groupem.enter`.
        peer (str, optional) -- session id or unique registry name; when a
            non-empty str the nomination leg is `nomination.standing`.

    Returns:
        {"nomination": dict | None, "watch_liveness": dict | None}
        plus `nomination_error` / `watch_liveness_error` when a leg raised.
        `nomination` is None when no record is on file.

    Scope "none": the engine-injected `repo_root` kwarg is unused; only the
    wire param narrows the target.
    """
    p = params if isinstance(params, dict) else {}
    override = p.get("repo_root")
    target_root = override if isinstance(override, str) and override else str(Path.cwd())
    peer = p.get("peer")

    result: dict = {}
    try:
        if isinstance(peer, str) and peer:
            result["nomination"] = nomination.standing(target_root, peer)
        else:
            result["nomination"] = nomination.who(target_root)
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        result["nomination"] = None
        result["nomination_error"] = f"{type(exc).__name__}: {exc}"
    try:
        result["watch_liveness"] = watch_heartbeat.read_liveness(target_root, time.time())
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise
        result["watch_liveness"] = None
        result["watch_liveness_error"] = f"{type(exc).__name__}: {exc}"
    return result
