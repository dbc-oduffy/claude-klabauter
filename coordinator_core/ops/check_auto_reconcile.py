"""
coordinator_core.ops.check_auto_reconcile -- in-process dispatch step for example-doctrine-repo's
check-auto-reconcile.sh trampoline (fleet /workday-start Morning Briefing probe).

Purpose: resolve the INVOKING repo's root (via process cwd -- see
_resolve_own_repo_root()) and run the already-registered
"handoff.reconcile_open" op (coordinator_core/ops/handoff_reconcile.py) in-process
via coordinator_core.invoke.dispatch.dispatch_message -- the same in-process
JSON-RPC path `python -m coordinator_core.invoke handoff.reconcile_open` used as
a subprocess before this port, minus the extra process hop. No dry_run flag is
ever passed here -- the op's own dry_run=true default governs (observation-only
daily probe; forcing a live reconcile is example-doctrine-repo's call, not this module's).

Deliberately DOES NOT parse the response envelope or render output -- envelope
parsing and rendering stay entirely on the example-doctrine-repo side (coordinator/bin/
check-auto-reconcile.sh) so that trampoline's COORDINATOR_AUTO_RECONCILE_JSON
test seam never needs a claude-klabauter checkout to exercise the rendering contract; this
module would be irrelevant to that seam.

Not a JSON-RPC op itself -- a plain module, NOT @register_op'd, called by
direct import (mirrors coordinator-auto-push / handoff-gate-aging's
direct-import trampoline shape, template-variant #1). The op it dispatches
(handoff.reconcile_open) is already registered elsewhere; this module adds no
new registry entries.

Spec backlink: docs/plans/2026-07-13-doe-auto-reconcile-adopt.md (C1) +
cross-repo/inbox/2026-07-13-claude-klabauter-em-claude-klabauter-auto-reconcile-wire-surfaces.md

Negative-spec:
    - Does NOT pass a dry_run flag, forced or otherwise.
    - Does NOT parse result.surfaced[] or render any output -- see example-doctrine-repo-side
      check-auto-reconcile.sh for that slice.
    - Does NOT register a new op or touch the four shared registry files --
      handoff.reconcile_open is already fully registered.
    - Does NOT raise on infrastructure failure (not-a-git-repo, dispatch
      exception) -- get_response() returns None, mirroring the example-doctrine-repo
      consumer's silent-skip contract.
    - Does NOT resolve the repo root from this module's own on-disk
      location -- that always resolved to claude-klabauter's checkout regardless of
      the caller, mistargeting every invoker's reconcile (and, once
      dry_run is armed, write) at claude-klabauter's own state/handoffs/ corpus. See
      _resolve_own_repo_root()'s docstring for the fix and its spec
      backlink.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def _resolve_own_repo_root() -> Optional[Path]:
    """Resolve the INVOKING repo's root -- the repo whose handoffs are being
    reconciled -- via the process's own current working directory, never
    this module's on-disk location.

    `handoff.reconcile_open` is "common_dir"-scoped (op_scopes.py):
    coordinator_core.ipc resolves the per-request partition strictly from
    the `_origin_worktree` JSON-RPC envelope field this function feeds
    (get_response(), below) -- there is no separate params-level repo_root.
    Every other direct-import trampoline in this package (auto_push.py,
    handoff_gate_aging.py) takes repo_root as an explicit argument sourced
    from the invoking process's cwd; this module has no such argument
    (get_response() takes none), so cwd is the only correct source here --
    it is NOT a fallback, it is the fleet convention: the example-doctrine-repo/sibling-repo
    trampoline (coordinator/bin/check-auto-reconcile.py) and the in-process
    orient-assemble reader (orient_assemble/readers_branch_reconcile.py)
    both run with cwd already set to the repo under reconciliation.

    Fixed 2026-08-03 -- the prior `find_repo_root(cwd=Path(__file__)...)`
    always resolved to claude-klabauter's OWN checkout (this module's on-disk
    location), regardless of which repo actually invoked it, so every
    caller reconciled (and, once dry_run is armed, would have WRITTEN to)
    claude-klabauter's own state/handoffs/ corpus instead of its own. See
    example-doctrine-repo docs/plans/2026-07-26-gate-resolution-widen-and-migrate.md
    (AC26c).
    """
    from coordinator_core.lifecycle import find_repo_root

    try:
        return find_repo_root()
    except RuntimeError:
        print(f"skip: _resolve_own_repo_root: return find_repo_root() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


def get_response() -> Optional[Dict[str, Any]]:
    """Dispatch handoff.reconcile_open in-process and return the raw JSON-RPC
    response dict (envelope intact -- 'result' or 'error' key), or None on any
    infrastructure failure."""
    repo_root = _resolve_own_repo_root()
    if repo_root is None:
        return None

    try:
        from coordinator_core.invoke.dispatch import dispatch_message
    except ImportError:
        print(f"skip: get_response: from coordinator_core.invoke.dispatch import dispatch_message failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    msg: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "handoff.reconcile_open",
        "params": {},
        "_origin_worktree": str(repo_root),
    }

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(dispatch_message(msg))
    except Exception:
        print(f"skip: get_response: return loop.run_until_complete(dispatch_message(msg)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    finally:
        loop.close()


def main(argv: List[str]) -> int:
    """Standalone CLI entrypoint (`python -m coordinator_core.ops.check_auto_reconcile`)
    -- prints the raw JSON-RPC response envelope for manual debugging. The example-doctrine-repo
    trampoline does NOT invoke this CLI form; it calls get_response() directly."""
    response = get_response()
    if response is None:
        return 1
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
