"""
coordinator_core/p4/ — the Perforce-as-second-class-VCS floor.

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
(D1, D2, D9 S3). This package is imported lazily, only behind
``coordinator_core.p4.workspace.is_p4_repo``'s marker check — a git-only
process never loads it (D1, § Anti-scope).

This module (C1) ships the read-only floor: the one p4 spawn helper
(``runner``) and the marker/identity/session-state readers (``workspace``).
It does NOT ship the session-changelist minter (C2), the push leg (C3), the
checkout guard (D5), or the verb fence (D6) — those are later plan rows.

Negative-spec (do NOT add here, see plan § Anti-scope):
  - No fragment loader, no provider-slot machinery (D9 — the read contract
    is DoE's committed ``p4-provider-fragment.md``, read by nothing at
    runtime).
  - No ``.p4config`` walk-up, no ``p4 set``, no probing (D1).
"""

from __future__ import annotations

from coordinator_core.p4.runner import P4Error, P4Result, classify_error, run
from coordinator_core.p4.workspace import (
    P4Identity,
    P4WorkspaceUnregistered,
    identity,
    is_p4_repo,
    session_change,
)

__all__ = [
    "P4Error",
    "P4Result",
    "classify_error",
    "run",
    "P4Identity",
    "P4WorkspaceUnregistered",
    "identity",
    "is_p4_repo",
    "session_change",
]
