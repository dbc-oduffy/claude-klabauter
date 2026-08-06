"""
coordinator_core.resolution — Tier-A canonical resolution facade package.

Purpose: the single entry point every caller in the canonical-resolution-engine
migration binds to for the two structurally-distinct resolution shapes the
coordinator engine performs:

    - operator-authored config (settings home, claude-klabauter root/bin, example-doctrine-repo root) —
      corruption-checked only, never trust-checked. See
      ``coordinator_core.resolution.facade.resolve_operator_config``.
    - harness-supplied plugin roots (``CLAUDE_PLUGIN_ROOT``) — trust-checked
      via the shared ``coordinator_core.trusted_root_guard`` trust-core. See
      ``coordinator_core.resolution.facade.guard_plugin_root``.

Spec backlink: docs/plans/2026-07-21-canonical-resolution-engine.md § W1-A1

Negative-spec: this package does NOT introduce a third path-resolution/
provenance concept — it is a thin facade over the two ALREADY-shipped
resolvers (``coordinator_core.machine_resolver`` and
``coordinator_core.trusted_root_guard``), composed here rather than
re-derived.
"""

from __future__ import annotations

from coordinator_core.resolution.facade import (
    OperatorConfigError,
    guard_plugin_root,
    resolve_operator_config,
)

__all__ = [
    "OperatorConfigError",
    "guard_plugin_root",
    "resolve_operator_config",
]
