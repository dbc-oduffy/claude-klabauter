"""
PriorityLedgerEntry — the AUTHORED priority-ledger record `priority.set` writes at
`<central-state-root>/priority-ledger/<target_id>.yaml`, one file per target,
projected into the cockpit contract. Pydantic port of coordinator-claude
`coordinator/schemas/priority-ledger.schema.json` (this repo carries no
`cockpit-contract/src/entities/*.ts` Zod twin for this entity — the TS source
tree was already retired by the T4e migration before this entity existed;
see the C6b determination note below).

AUTHORED, NOT DERIVED — do not conflate with `HandoffSummary.pm_priority` /
`pm_priority_origin` / `pm_priority_source_id` (C6a, `entities/summaries.py`).
Those three fields are the RESOLVED-FROM-ANCESTOR answer for one handoff,
computed at emit time by `ops/emit/priority_resolve.py`'s nearest-explicit-
ancestor walk. This entity is the raw authored assignment the walk reads —
what the PM (or cockpit, via the intent-drain seam) actually SET, one row per
target, with no inheritance applied. A resolved-from pointer belongs on the
derived side only; see the ledger schema's own NEGATIVE-SPEC (2) for why
`inherited_from`/`resolved_from`/`priority_predecessor` are permanently
absent from this shape, mirrored here as `additionalProperties`-equivalent
`extra="forbid"`.

`priority: "none"` is the ledger's EXPLICIT-CLEAR SENTINEL (see the JSON
schema's top-level description) — a real authored row that terminates an
upward inheritance walk, NOT a deletion. Do not special-case it away here;
`priority_resolve.py` depends on seeing it.

C6b DETERMINATION (executor-verified against current disk, not plan-asserted
— see docs/plans/2026-07-26-priority-ledger.md § C6b): this entity is
registered in `ENTITY_SCHEMAS` (cockpit_schema/__init__.py) and re-exported
from `entities/__init__.py`, matching the `FinancialMetricSummary` precedent
(`092cfa95`) for "add a new authored entity + ride an existing
`CONTRACT_VERSION` bump" — NOT the `CommitClosure` precedent (`852e2169`),
which stays OUT of `ENTITY_SCHEMAS` by design because it has no coordinator-claude-hosted
contract counterpart and is validated shape-based instead
(`validate.py:440-467`). This entity DOES need a coordinator-claude-hosted contract
counterpart: `state/cross-repo-commitments/2026-07-26-doe-owns-durable-
priority-ledger-for-cockpit.yaml` commits coordinator-claude to publishing the ledger shape
to cockpit, so `ENTITY_SCHEMAS` membership (and the coordinator-claude-side JSON-schema
re-vendor it implies, tracked the same way `092cfa95`'s spinoff baton
tracked `FinancialMetricSummary`'s) is the correct shape, not the excluded
one.

Envelope/collector surface — verified, not asserted: neither `envelope.py`
(no `priority_ledger_entries` skeleton key, no SECMAP row) nor a dedicated
`ops/emit/sections/priority_ledger_entries.py` collector is added here.
`092cfa95` itself is the precedent for this half too — it registered
`FinancialMetricSummary` in `ENTITY_SCHEMAS` with NO `envelope.py` touch and
no collector file (grep confirms no `financial_metric` hit anywhere under
`ops/emit/`); `financial-metric-summary` and this entity both publish a
schema for external reference/validation without claude-klabauter owning a populated
array in the emitted snapshot yet. `CommitClosure` is the opposite shape
(SECMAP + collector, no `ENTITY_SCHEMAS`) precisely because claude-klabauter itself is
the sole producer of commit-closure facts; here, `priority.set` writes the
source-of-truth YAML directly and `priority_resolve.py` already reads it
in-process for the derived `pm_priority*` fields — there is no producer/
consumer gap for a collector to bridge yet. Wiring an actual
`priority_ledger_entries[]` array into the envelope is a separate, later
concern (not named by any chunk in the priority-ledger plan) and must not be
inferred from this registration.

Spec backlink: coordinator/schemas/priority-ledger.schema.json (coordinator-claude repo)
Spec backlink: docs/plans/2026-07-26-priority-ledger.md § C6b
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from coordinator_core.contract.cockpit_schema.common import IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ProvenanceEnvelope

PriorityLedgerTargetKind = Literal["handoff", "plan", "roadmap", "deliverable"]

PriorityLedgerTier = Literal["urgent", "high", "medium", "low", "none"]
"""Ordered urgent (hottest) > high > medium > low > none. `none` is the
EXPLICIT-CLEAR SENTINEL, not an absence — see module docstring."""

PriorityLedgerSource = Literal["op", "external-intent"]
"""The writing MECHANISM, never a consumer repo name — see `source_repo`
for the originating repo identifier."""


class PriorityLedgerEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    """
    Connector-injected registry shortname — the coordinator root whose
    `priority-ledger/` directory this row was observed in. Matches the
    injection convention every other summary entity uses (D4).
    """
    coordinator_root_path: str
    """Connector-injected absolute filesystem path to that coordinator root."""

    target_id: str
    """
    Identifier of the prioritized target. Matches the source filename
    (`<target_id>.yaml`) — there is no separate `id` field on the authored
    record (filename-as-identity, D2); this field is the connector's read of
    that filename, not a duplicated authored key.
    """
    target_kind: PriorityLedgerTargetKind
    """The kind of artifact `target_id` refers to."""
    priority: PriorityLedgerTier
    """Priority tier this row explicitly authors for `target_id`."""
    source: PriorityLedgerSource
    """The writing mechanism that produced this entry."""

    # Nullable fields (D9 present-as-null, not optional) — the JSON schema
    # permits omitting these keys entirely; the connector normalizes an
    # absent key to null rather than dropping the field from the emitted
    # record.
    set_by: str | None
    """Identifier of the session/agent/person that set this priority."""
    set_at: IsoDateTime | None
    """Timestamp this priority was authored."""
    source_repo: str | None
    """
    The originating repo identifier for this entry; null when
    `source == "op"` (an in-tree PM/EM authoring action, not a cross-repo
    intent record).
    """
    note: str | None
    """Free-form prose note."""

    provenance: ProvenanceEnvelope
