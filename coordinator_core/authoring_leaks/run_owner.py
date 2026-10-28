"""run_owner: which OTHER live run holds an uncommitted path.

Joins `claim_index.lookup` (path -> claimant sessions) with `warp_live.live_run_for_session`
(session -> live run id). Read-only on disk, zero spawns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Optional

from coordinator_core import warp_live
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core


def owning_runs(
    repo_root, paths: Iterable[str], *, self_session: Optional[str] = None
) -> Dict[str, str]:
    """`{path: run_id}` for each of `paths` claimed by a session, other than `self_session`, that
    owns a live run. Unclaimed, self-claimed, dead-run and UNANSWERABLE paths are absent. Keys are
    the caller's own strings. `self_session` None resolves the repo's current session; a resolve
    failure excludes nobody. Never raises: any failure returns {}.
    """
    try:
        paths = list(paths)
        if not paths:
            return {}
        root = Path(repo_root)
        if self_session is None:
            try:
                self_session = session_core.resolve_session_id(str(root))
            except Exception:  # noqa: BLE001 -- no self-exclusion, never a raise
                self_session = None
        claims = claim_index.lookup(paths, cwd=str(root))
        runs: Dict[str, Optional[str]] = {}
        out: Dict[str, str] = {}
        for path in paths:
            claimants = claims.get(path) or []
            if claim_index.UNANSWERABLE in claimants:
                continue
            for sid in claimants:
                if not sid or sid == self_session:
                    continue
                if sid not in runs:
                    runs[sid] = warp_live.live_run_for_session(root, sid)
                if runs[sid]:
                    out[path] = runs[sid]
                    break
        return out
    except Exception:  # noqa: BLE001 -- an owner lookup never breaks its caller
        return {}
