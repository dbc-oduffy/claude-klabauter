"""
coordinator_core.ops.warm_request_status — JSON-RPC "warm.request_status" operation.

Purpose: the poll op a caller names after a MUTATING dispatch comes back
WARM_DISPATCH_INDETERMINATE (-32004). Registered on every table `ping` is on
(D5, `docs/plans/2026-09-23-warm-dispatch-reconcile.md` C4;
`coordinator_core/contract/dispatch-ack-reconcile-contract.md` § 9).

This module's handler is reached ONLY on the cold or pool path, where no
`dispatch_ack.AckStore` is visible — the accept process's `_serve_line`
intercepts `warm.request_status` and answers it directly from the store
(contract § 9), never submitting it to the pool or the cold entrypoint. A
pool worker cannot see the accept process's memory, so this registered
handler always answers `unknowable(no-resident-engine)` and never
`not_received` — the only claim this handler is in a position to make.

Self-registration: importing this module calls register_op(...) as a
side-effect, mirroring `coordinator_core.ops.ping`.

Spec backlink: docs/plans/2026-09-23-warm-dispatch-reconcile.md § C4
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ipc import register_op


@register_op("warm.request_status")
def _warm_request_status(params: dict, repo_root: Path | None = None) -> dict:
    """JSON-RPC "warm.request_status" handler (cold/pool path only).

    Contract (dispatch-ack-reconcile-contract.md § 9): every answer carries
    `state`, `reason` (when unknowable), `engine_boot_ns` and `engine_pid`.
    This leg never sees a resident `AckStore`, so it always answers
    `unknowable(no-resident-engine)` with no engine boot/pid to report.
    """
    key = params.get("key")
    if not key or not isinstance(key, str):
        raise ValueError("warm.request_status requires 'key' (non-empty string)")
    return {
        "state": "unknowable",
        "reason": "no-resident-engine",
        "engine_boot_ns": None,
        "engine_pid": None,
    }
