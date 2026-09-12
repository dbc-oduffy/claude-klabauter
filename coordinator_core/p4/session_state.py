"""
coordinator_core/p4/session_state.py — ``p4.session_state`` op (C7, D9 S3).

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
§ C7, D9.

Classified COMPUTE_ONLY. Returns ``{registered, client, port, p4_change,
p4_base_sha, p4_shelved_at, p4_shelved_sha}`` built entirely from C1's
readers (``coordinator_core.p4.workspace.identity`` and ``.session_change``)
— zero p4 spawns, zero writes. This is the one seam DoE's H5 skill step and
Example-game-repo's C12 both call; neither reimplements the read.

Negative-spec:
  - Never spawns ``p4`` (that's ``register.py``'s ``_confirm_client`` and
    the later push/commit legs, not this op).
  - Never raises: an unregistered workspace or an absent session simply
    reads as ``registered: False`` / all-``None`` fields, matching
    ``workspace.session_change``'s own never-raises contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.p4 import workspace


@register_op("p4.session_state")
def _session_state(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``p4.session_state`` handler.

    Params: ``repo_key`` (optional — absent or unregistered reads as
    ``registered: False``, ``client``/``port`` both ``None``), ``sdir``
    (optional session directory — absent reads all four ``p4_*`` fields as
    ``None``, matching ``workspace.session_change``'s absent-file contract).
    """
    repo_key = params.get("repo_key")
    sdir = params.get("sdir")

    registered = False
    client = None
    port = None
    if repo_key:
        try:
            ident = workspace.identity(repo_key)
            registered = True
            client = ident.client
            port = ident.port
        except workspace.P4WorkspaceUnregistered:
            registered = False

    if sdir:
        change = workspace.session_change(str(sdir))
    else:
        change = {
            "p4_change": None,
            "p4_base_sha": None,
            "p4_shelved_at": None,
            "p4_shelved_sha": None,
        }

    return {
        "registered": registered,
        "client": client,
        "port": port,
        **change,
    }
