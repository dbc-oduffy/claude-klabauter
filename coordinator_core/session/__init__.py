"""
coordinator_core.session — Python engine port of coordinator's session substrate
(Port of: coordinator-session.sh, coordinator-claude e34f2484, 2026-07-22, + its
``coordinator/lib/session/*.sh`` sibling modules).

Purpose: package namespace for the session-tracking primitives (identity
resolution, liveness, scope, shape, claims) that formerly lived in the bash
hub. Modules are ported one T0-bash-module at a time (``core``, ``identity``
first — this hub); sibling modules (``liveness``, ``scope``, ``shape``,
``claims``) land from other build agents in the same migration wave and
extend this package.

Negative-spec: keep this file minimal — a thin namespace marker only. Sibling
agents add their own modules under this package; they do not need to touch
this ``__init__.py``.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4a-g1
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t4a-coordinator-session-hub.md
"""
