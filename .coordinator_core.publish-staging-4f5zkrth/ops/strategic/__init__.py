"""
coordinator_core.ops.strategic — helper package for the "strategic.generate" op.

Purpose: houses the three generator helpers dispatched by
coordinator_core.ops.strategic_generate (Track A version_highlights derivation,
Track B competitor_deltas derivation, and the draft.yaml writer) so the top-level
op module stays a thin RPC wrapper — same split as coordinator_core.cartography/
(primitives) vs coordinator_core.ops.cartography_*.py (RPC wrappers).

Spec backlink: pln-makima-generation-leg-machine--127c81 § C1
"""

from __future__ import annotations
