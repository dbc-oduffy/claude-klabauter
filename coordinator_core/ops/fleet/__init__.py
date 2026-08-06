"""
coordinator_core.ops.fleet — fleet.* MUTATING archival ops sub-package.

Purpose: three producer-side ops that git-mv terminal artifacts into archive/
under a confirm→act (dry_run:true / dry_run:false) wire contract.

Ops registered by handler modules (imported by coordinator_core/ops/__init__.py):
    fleet.archive_completed_plans    — archive_plans.py
    fleet.archive_completed_handoffs — archive_handoffs.py
    fleet.prune_closed_bugs          — prune_bugs.py

Shared substrate: _common.py (param validation, envelope builders, D3 check,
main_worktree_root helper, async archive_and_commit git helper).

All three ops are MUTATING, UDS-only per DR-208 + DR-211 five-bound (v).
The DR-211 breadcrumb (authz/classification.py:50-73) reserves their
OP_CLASSIFICATION entries; those land in chunk C5 (ops/__init__.py +
authz/classification.py + ipc.py _OP_KEY_SCOPE).

Spec backlinks:
  - Plan:     docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md
  - Contract: coordinator_core/contract/cockpit-invoke-producer-contract.md
  - DR-208:   docs/decisions/DR-208-invoke-op-authz-model.md
  - DR-211:   docs/decisions/DR-211-fleet-op-substrate-write-boundary.md

Mirrors: coordinator_core/ops/emit/ sub-package shape.
"""
