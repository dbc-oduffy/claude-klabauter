"""
Deliverable-spine shared enums — used by HandoffSummary, PlanSummary, and
RoadmapSummary. Pydantic port of example-doctrine-repo
`coordinator/cockpit-contract/src/entities/deliverable-spine.ts` (Zod source).

Extracted into a standalone module to avoid circular imports in the TS
source: summaries.ts re-exports RoadmapSummary (from roadmap-summary.ts), and
roadmap-summary.ts needs these enums — a direct import from summaries.ts
would create a circular reference. Preserved as a standalone module here for
parity, though Python's import model does not force the same split.

Spec backlink: docs/plans/2026-07-03-fleet-deliverable-spine-identity-and-facets.md § D4, D5.
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from typing import Literal

WorkstreamType = Literal[
    "roadmap",
    "infra",
    "bug",
    "docs",
    "research",
    "refactor",
    "uncategorized",
    "queue-derived-baton",
]
"""
Workstream type — the normalized handoff `category` enum projected into the
emission. 8-value closed set; source-of-truth in
coordinator_core/ops/handoff_normalize.py (`_match_category`).
`queue-derived-baton` added 2026-07-23 to match the sibling
coordinator-claude/claude-central-em repo's newly-shipped 8th `category` enum
value for queue-derived triage batons — without this, a handoff carrying that
category was silently quarantined to the `malformed` bucket in cockpit
emission (Pydantic `ValidationError` on `HandoffSummary`).

`null` vs `"uncategorized"` are two distinct facts, not interchangeable.
`emit/sections/handoffs.py` passes `fm.get("category")` straight through, so
an un-normalized handoff (no `category` field) emits `null`.
`_match_category` only ever runs during normalization, and defaults an
unmatched title to the string `"uncategorized"`. So `null` means "never
normalized" and `"uncategorized"` means "normalized, no keyword fired" —
collapsing them would assert normalization ran when it did not. Consumers
that only care whether a handoff is categorized should treat both as one
bucket.
Spec backlink: docs/plans/2026-07-03-fleet-deliverable-spine-identity-and-facets.md § D5.
"""

DeliverableStatus = Literal[
    "proposed",
    "planned",
    "in-progress",
    "in-review",
    "shipped",
    "abandoned",
]
"""
Deliverable lifecycle status — derived at emit from existing artifact signals.
Named `deliverable_status` (not `status`) on HandoffSummary / PlanSummary /
RoadmapSummary to avoid collision with each entity's own per-artifact `status`
field.
Precedence: `shipped` iff merge-verified; `abandoned` iff ALL live artifacts
are abandoned; otherwise max-progress across the deliverable's artifacts.
Spec backlink: docs/plans/2026-07-03-fleet-deliverable-spine-identity-and-facets.md § D5.
"""
