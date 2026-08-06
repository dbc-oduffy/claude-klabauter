"""coordinator_core.clustering — record-clustering primitives.

Non-op logic (unattached-record graduation-gate clustering) that lives
outside `coordinator_core/ops/` deliberately: it is not a registered op,
just an importable heuristic consumed by CLI callers and future
`queue.*` ops alike.

Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C2
"""
from __future__ import annotations
