"""
coordinator_core.install.migrations — one-time, idempotent, machine-local
repoint legs for registry pins that `ensure_venv` deliberately will not
self-heal (its `_set_pin` / `_should_write_general_pin` preserve a healthy
operator pin across rebuilds by design).

A migration leg here does exactly one thing: detect an old value at a
machine-local registry key and repoint it to the correct one, verifying the
target before writing. Each leg is independently importable and callable;
this package holds no shared runtime state.

Spec backlink: docs/plans/2026-08-18-retire-coordinator-venv.md (C1).
"""

from __future__ import annotations
