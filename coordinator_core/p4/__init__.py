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

# Review: overengineering-reviewer F1 (integrator-applied) — no in-repo
# consumer ever imported this package's re-export facade; every caller
# reaches for submodules directly. The facade also shadowed the
# `session_change` submodule for any `from coordinator_core.p4 import
# session_change`, and its eager `runner` import was the reason
# `push_outstanding.py::_is_p4_repo` had to duplicate `workspace.is_p4_repo`
# rather than call it. Left empty; import the submodules you need directly.

Negative-spec (do NOT add here, see plan § Anti-scope):
  - No fragment loader, no provider-slot machinery (D9 — the read contract
    is DoE's committed ``p4-provider-fragment.md``, read by nothing at
    runtime).
  - No ``.p4config`` walk-up, no ``p4 set``, no probing (D1).
  - No re-export facade — see review note above; import submodules directly.
"""

from __future__ import annotations
