"""Formerly: standing tripwire (C6b, docs/plans/2026-08-14-baton-closes-when-its-plan-
ships.md § C6b, AC11) pinning that a checked-in allowlist of raw `deliverable_id`
readers stayed disjoint from every module routing `deliverable_id` through
`coordinator_core.ops.deliverable_equivalence.canonicalize()`.

`state/deliverable-equivalence.yaml`'s `entries:` block, `load_equivalence_map`, and
`canonicalize()` are condemned and removed (plan
docs/plans/2026-08-20-the-close-ceremony-stops-paying-for-the-join.md, C1g, evidence
F-1) -- there is no `canonicalize()` left for any module to route through, so this
gate's ROUTED/raw partition can no longer be computed: every `deliverable_id` reader is
raw now, by construction, and the population this test pinned no longer exists as a
distinguishable set. The gate's premise is dissolved, not weakened -- retiring it is a
mechanical consequence of the kill, not a separate decision. See `state/kill-ledger.md`
for the kill record.

Spec backlink: docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md § C6b (AC11)
-- superseded.
"""

from __future__ import annotations
