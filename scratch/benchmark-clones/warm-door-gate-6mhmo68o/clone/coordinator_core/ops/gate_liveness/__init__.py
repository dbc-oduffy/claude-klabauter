"""
coordinator_core.ops.gate_liveness — the gate-closure-signal package.

Purpose: houses the two halves of the `external_gate.closure_key` contract
(`plan-tasks.schema.json` 1.10.0 / `cross-repo-memo.schema.json` 1.7.0,
SSOT `coordinator_core.contract.emit_memo_schema`):

  - `emit_discharge` — the PRODUCER half. Composes the `discharges:` block a
    discharging repo stamps on an outbound cross-repo memo. Not a registered
    op (plain composer/validator) — see that module's docstring.
  - `resolve` — the READER half. Registered as the `gate_liveness.resolve`
    op (C1): joins each plan's `external_gate[].closure_key` entries against
    inbound `discharges:` blocks found in this repo's own
    `cross-repo/inbox/`/`cross-repo/archive/` trees.

Spec backlink: docs/plans/2026-08-21-a-discharged-gate-tells-the-row-waiting.md
"""

from __future__ import annotations
