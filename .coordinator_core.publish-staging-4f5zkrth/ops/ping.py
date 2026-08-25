"""
coordinator_core.ops.ping — JSON-RPC "ping" operation.

Purpose: The cheapest registered COMPUTE_ONLY op — a trivial dispatch that returns
the current monotonic clock. Post-DR-215 (command-type engine) it is reached
spawn-per-call through the `coordinator_core.invoke` command entrypoint; it proves
the entrypoint can resolve, populate the registry, and dispatch — NOT that any
resident process is "live" (there is none). Used as the doctor's invoke-dispatch smoke.

Self-registration: importing this module calls register_op("ping", _ping) as a
side-effect. coordinator_core.ops.__init__ imports this module, so the registration
fires when `import coordinator_core.ops` executes (at dispatch time).

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C1b
"""

from __future__ import annotations

import time

from pathlib import Path

from coordinator_core.ipc import register_op


@register_op("ping")
def _ping(params: dict, repo_root: Path | None = None) -> dict:
    """JSON-RPC "ping" handler.

    Returns:
        {"ok": true, "ts": <float>}  where ts is time.monotonic() at dispatch time.

    The ts value is monotonic (not wall-clock). Post-DR-215 this op is reached
    spawn-per-call via the command entrypoint; it carries no resident-liveness or
    warm-latency contract — it is simply the cheapest dispatch to smoke-test.
    """
    return {"ok": True, "ts": time.monotonic()}
