"""
coordinator_core.housekeeping — the handoff-housekeeping v2 package.

Purpose: home for the replacement handoff-housekeeping cycle built to the
per-leg brightline in docs/research/2026-08-29-housekeeping-v2-target-shape.md
and docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md. This
package is assembled chunk by chunk (C1-C7 of that plan); this __init__ is
intentionally empty at C1 — the fixture instrument — and gains re-exports as
later chunks land head_scan, corpus, archive_index, resolve, gate_clear,
terminal and cycle.

Spec backlink: docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md
"""

from __future__ import annotations
