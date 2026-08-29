"""
coordinator_core.ops.check_auto_reconcile -- in-process dispatch step for DoE's
check-auto-reconcile.sh trampoline (fleet /workday-start Morning Briefing probe).

Purpose: resolve the INVOKING repo's root (an explicit `repo_root` passed to
get_response(), falling back to process cwd via _resolve_own_repo_root()
when omitted -- DR-382) and run the already-registered
"handoff.reconcile_open" op (coordinator_core/ops/handoff_reconcile.py) in-process
via coordinator_core.invoke.dispatch.dispatch_message -- the same in-process
JSON-RPC path `python -m coordinator_core.invoke handoff.reconcile_open` used as
a subprocess before this port, minus the extra process hop. No dry_run flag is
ever passed here -- the op's own dry_run=true default governs (observation-only
daily probe; forcing a live reconcile is DoE's call, not this module's).

Deliberately DOES NOT parse the response envelope or render output -- envelope
parsing and rendering stay entirely on the DoE side (coordinator/bin/
check-auto-reconcile.sh) so that trampoline's COORDINATOR_AUTO_RECONCILE_JSON
test seam never needs a claude-klabauter checkout to exercise the rendering contract; this
module would be irrelevant to that seam.

Not a JSON-RPC op itself -- a plain module, NOT @register_op'd, called by
direct import (mirrors coordinator-auto-push / handoff-gate-aging's
direct-import trampoline shape, template-variant #1). The op it dispatches
(handoff.reconcile_open) is already registered elsewhere; this module adds no
new registry entries.

Spec backlink: DoE-claude:pln-doe-side-adoption-of-claude-klabauter-au-284ced (C1) +
cross-repo/inbox/2026-07-13-claude-klabauter-em-claude-klabauter-auto-reconcile-wire-surfaces.md

Negative-spec:
    - Does NOT pass a dry_run flag, forced or otherwise.
    - Does NOT parse result.surfaced[] or render any output -- see DoE-side
      check-auto-reconcile.sh for that slice.
    - Does NOT register a new op or touch the four shared registry files --
      handoff.reconcile_open is already fully registered.
    - Does NOT raise on infrastructure failure (not-a-git-repo, dispatch
      exception) -- get_response() returns None, mirroring the DoE
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
from typing import Any, Dict, List, Optional, Union


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
    it is NOT a fallback, it is the fleet convention: the DoE/sibling-repo
    trampoline (coordinator/bin/check-auto-reconcile.py) and the in-process
    orient-assemble reader (orient_assemble/readers_branch_reconcile.py)
    both run with cwd already set to the repo under reconciliation.

    Fixed 2026-08-03 -- the prior `find_repo_root(cwd=Path(__file__)...)`
    always resolved to claude-klabauter's OWN checkout (this module's on-disk
    location), regardless of which repo actually invoked it, so every
    caller reconciled (and, once dry_run is armed, would have WRITTEN to)
    claude-klabauter's own state/handoffs/ corpus instead of its own. See
    DoE-claude docs/plans/2026-07-26-gate-resolution-widen-and-migrate.md
    (AC26c).
    """
    from coordinator_core.lifecycle import find_repo_root

    try:
        return find_repo_root()
    except RuntimeError:
        print(f"skip: _resolve_own_repo_root: return find_repo_root() failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


def get_response(repo_root: Optional[Union[Path, str]] = None) -> Optional[Dict[str, Any]]:
    """Dispatch handoff.reconcile_open in-process and return the raw JSON-RPC
    response dict (envelope intact -- 'result' or 'error' key), or None on any
    infrastructure failure.

    `repo_root`, when given, is used as-is (DR-382: an explicit root passed
    in by an argv-edge caller or an in-process reader threading its own
    scan scope). A `str` is accepted alongside a `Path`: the value is
    stringified into `_origin_worktree` either way, and the orient_assemble
    readers that thread a scan scope carry it as `str` end-to-end
    (`brief(cadence, *, repo_root: str | None)`), so narrowing here would
    force a conversion at every threading call site for no gain at the wire.
    `None` -- the default, and every caller's behaviour before
    this parameter existed -- falls back to `_resolve_own_repo_root()`,
    which stays the entry-point default (this function is still an argv-edge
    entry point per its own module docstring; cwd resolution is correct
    there when no root is supplied)."""
    if repo_root is None:
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
        return loop.run_until_complete(
            dispatch_message(msg, caller="coordinator_core.ops.check_auto_reconcile")
        )
    except Exception:
        print(f"skip: get_response: return loop.run_until_complete(dispatch_message(msg)) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    finally:
        loop.close()


_USAGE = (
    "usage: python -m coordinator_core.ops.check_auto_reconcile\n\n"
    "Standalone CLI entrypoint -- prints the raw JSON-RPC response envelope for\n"
    "manual debugging by dispatching handoff.reconcile_open in-process. Takes no\n"
    "arguments; an unrecognized argument prints this usage and exits 2 rather\n"
    "than silently dispatching the sweep."
)


def main(argv: List[str]) -> int:
    """Standalone CLI entrypoint (`python -m coordinator_core.ops.check_auto_reconcile`)
    -- prints the raw JSON-RPC response envelope for manual debugging. The DoE
    trampoline does NOT invoke this CLI form; it calls get_response() directly.

    `-h`/`--help` prints usage and exits 0 WITHOUT calling get_response() --
    the same defect class the sender's own memo caught in
    `start_example_retrieval_repo.py --help` (a flag reaching the working path before
    anything decides what it means). Any other unrecognized argument prints
    usage and exits 2 -- this CLI takes none, and silently ignoring one would
    dispatch the full sweep on a typo'd flag."""
    if argv:
        if argv[0] in ("-h", "--help"):
            print(_USAGE)
            return 0
        print(_USAGE)
        return 2

    response = get_response()
    if response is None:
        return 1
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
