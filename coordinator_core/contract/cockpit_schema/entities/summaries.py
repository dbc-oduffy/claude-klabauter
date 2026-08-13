"""
Read-from-disk consumed shapes — the coordinator-artifact summaries the
connector (tc-4) / emitter (tc-3) extract from each repo's `state/` tree and
the store (tc-5) ingests for the cross-repo census. Pydantic port of coordinator-claude
`coordinator/cockpit-contract/src/entities/summaries.ts` (Zod source).

These are part of the frozen C5-consumable field set (tc-2 stub §
Specification): a field gap here forces a re-run of tc-3 AND tc-4, so the
full set is pinned now. `repo` + `coordinator_root_path` are injected by the
connector/emitter (the on-disk frontmatter does not carry which repo it was
read from). Provenance is mandatory, as on every cockpit fact.

RoadmapStatus/RoadmapSummary, TrackerStatus/TrackerSummary, and
HealthStatusLifecycle/HealthPosture/HealthStatusSummary are re-exported by
the TS source from sibling files (roadmap-summary.ts, tracker-summary.ts,
health-status-summary.ts) rather than defined here — this module re-exports
the already-ported sibling modules' symbols for parity, though Python's
import model does not force the TS split.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.producer_vocab import ProducerOpIdentity

from ..common import IsoDate, IsoDateTime
from ..provenance import ContentHash, ProvenanceEnvelope
from .deliverable_spine import DeliverableStatus, WorkstreamType
from .health_status_summary import HealthPosture, HealthStatusLifecycle, HealthStatusSummary
from .roadmap_summary import RoadmapStatus, RoadmapSummary
from .tracker_summary import TrackerStatus, TrackerSummary

__all__ = [
    "HandoffStatus",
    "HandoffKind",
    "ForeignOriginTriple",
    "DeploymentState",
    "HandoffSummary",
    "BacklogType",
    "BugSeverity",
    "BacklogQueueScope",
    "BacklogItemSummary",
    "ReviewVerdict",
    "ReviewTrail",
    "RoadmapStatus",
    "RoadmapSummary",
    "TrackerStatus",
    "TrackerSummary",
    "HealthStatusLifecycle",
    "HealthPosture",
    "HealthStatusSummary",
]

# JS Number.MIN_SAFE_INTEGER / MAX_SAFE_INTEGER — the bounds Zod's
# `z.number().int()` emits on every integer field (§ 1 of the T4e port recipe).
_SafeInt = Annotated[int, Field(ge=-9007199254740991, le=9007199254740991)]

# ── Handoff summary (from state/handoffs/*.md frontmatter) ──────────────────

HandoffStatus = Literal["open", "claimed"]
"""
Handoff lifecycle stage — the two-value enumeration governing the current
actionability of a handoff: `open` (in play, awaiting pickup) or `claimed`
(picked up and acted on).

**`superseded` was retired as a handoff status on 2026-06-26 (handoff-only).**
Supersession of a handoff is now expressed via `deployment_state: continued`
(successor-bearing — paired with `continued_into`) or `deployment_state:
closed` (no successor — paired with `closed_reason`), combined with the
existing `predecessor`/`supersedes:` lineage fields — not a distinct status
value. The retirement sequence: doctrine writers (CLAUDE.md,
spinoff-handoffs.md, skills/handoff/SKILL.md, schemas/handoff.yaml) and live
data were migrated first; the contract was narrowed after (contract follows
doctrine, not vice-versa).

**`active`/`consumed` → `open`/`claimed` (DR-084, this contract version).**
The two-value stage enum was renamed wholesale — the *shape* (two stages) is
unchanged, only the wire tokens. Producers/consumers on the frozen-contract
side of the P3 gate (this cutover) emit/expect the new tokens; the
dual-read/coerce shims that tolerate the old tokens live at the emitter
(`coordinator_core/ops/emit/sections/handoffs.py`, C7-owned) and are out of
this module's scope.

**Legacy/external tolerance — coerce-at-ingest:** readers tolerate a legacy or
external handoff carrying `status: superseded`. The cockpit emitter (claude-klabauter's
Python `artifact.emit`, the sole production emitter as of DR-208/DR-210)
coerces `superseded` → `claimed` at ingest, before the strict pydantic
validator sees the record. `schemas/handoff-archived.yaml` also stays
tolerant of `superseded` upstream — but NOT for the reason previously stated
here. The retracted claim was that `query-records --type handoff-archived`
runs before the coerce step and would otherwise reject historical archived
records; verified false 2026-07-22, the query path performs no schema
validation whatsoever, so it cannot reject anything. Upstream tolerance is
retained for legacy/sibling read-compatibility on its own merits. Any other string-but-unrecognized handoff status that passes the
per-record filter (which excludes only missing/null fields) but is NOT
coerced triggers a **whole-emit abort** at record-validation time — not a
per-record exclusion. Only the one retired `superseded` token is coerced; any
other unexpected status string still hard-aborts the emit.

**Stage vs. `deployment_state` — orthogonal axes, not redundant:** `status`
answers "is this handoff still in play?" — a two-stage gate. `deployment_state`
answers "where is the associated workstream in its delivery lifecycle?" — a
richer progression (`awaiting_gate | ready_to_fire | in_flight | shipped |
continued | closed`). These are INDEPENDENT dimensions: a `claimed` handoff
can carry `deployment_state: in_flight` (picked up but not yet shipped); an
`open` handoff can carry `deployment_state: ready_to_fire` (staged, awaiting
pickup). Consumers keying on one axis must not substitute the other.

Spec backlink: `docs/plans/2026-06-26-retire-superseded-handoff-status.md` § C4;
`docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md` § C6.
"""

HandoffKind = Literal[
    "session-handoff", "spinoff", "spinoff-roadmap", "recovery",
    "spinoff-goal", "spinoff-roadmap-creator",
    # Retained deliberately after spike-result stopped being a LIVE handoff kind
    # (coordinator-claude handoff.schema.json 3.0.0). The cockpit `handoffs` array is NOT
    # live-only: it carries archived records too, and an archived record may
    # carry any historical kind — the same reason handoff-archived.schema.json
    # keeps this value permanently. Narrowing it here silently DROPS archived
    # spike-result records from the emission rather than failing loudly.
    "spike-result",
    # D1 baton-kind vocabulary rename targets (2026-07-29): "spinoff-roadmap"
    # -> "roadmap-baton", "spinoff-roadmap-creator" -> "roadmap-seed",
    # "spinoff-goal" -> "goal-seed". ADDED, not substituted — this same
    # archived-record non-narrowing rule that keeps "spike-result" permanently
    # also keeps every pre-rename token (including the three being retired on
    # the live surface) permanently: an archived record may carry any
    # historical kind, and narrowing here would silently drop it from the
    # emission instead of failing loudly.
    "roadmap-baton", "roadmap-seed", "goal-seed",
]
"""
Handoff kind. NORMALISATION CONTRACT (tc-3/tc-4): on-disk frontmatter omits
the `kind:` key for plain continuation handoffs — the connector/emitter MUST
inject `"session-handoff"` when `kind:` is absent before emitting a
HandoffSummary, or the (required, non-optional) `kind` field below will fail
validation.

Spec backlink: docs/plans/2026-07-29-baton-kind-vocabulary-one-axis-per-field.md § D1/C8a.
"""

BatonClass = Literal["continuation", "deflection", "intention"]
"""
Why the baton exists, independent of which ceremony emitted it. The axis a
consumer filters on to separate work laid toward a goal (`intention`) from work
deflected out of a session (`deflection`) from a session continuing its own work
(`continuation`) — without hand-rolling a membership set over `kind`, which is
what five divergent readers each did differently before this field existed.

DERIVED, never stored. No on-disk handoff frontmatter carries a `baton_class:`
key; the emitter computes it from `kind` at emit time via the single canonical
function (`coordinator_core/frontmatter/baton_class.py`), which reads its mapping
from the `x-baton-class` key in the vendored `handoff.schema.json`. There is
exactly one place a stored copy could disagree with its derivation — nowhere,
because there is no stored copy.

Spec backlink: docs/plans/2026-07-29-baton-kind-vocabulary-one-axis-per-field.md § D2/C3a.
"""


# ProducerOpIdentity is defined in coordinator_core.producer_vocab (imported
# above) — a leaf module with no third-party imports, so that
# session.producer_resolve can validate against it without pulling pydantic
# and the cockpit-contract package onto the handoff-creation hot path. See
# that module's docstring for the full machine-minted/hand-authored contract
# (carried across verbatim from what used to live here).

ProducerTypedCommand = Literal["other-command", "unresolved"]
"""
Named-literal half of `HandoffSummary.producer.typed_command`'s value space.
The full field type is `ProducerTypedCommand | str | None`: a normalized
coordinator slash-command name (plain `str`, the common case), `"other-
command"` (a typed slash verb outside coordinator's own command set),
`"unresolved"` (capture failed), or `None` (machine-minted with nothing
typed this turn — distinct from `"unresolved"`: the field never even had a
capture attempt).

Spec backlink: docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md § C6a.
"""


class ForeignOriginTriple(BaseModel):
    """
    A foreign-kind origin reference — for origin kinds whose artifacts are
    NOT themselves emitted as HandoffSummary rows (goal, roadmap-creator, and
    any future ceremony rungs). We emit the full `{id, kind, label}` triple
    so cockpit can render without a stub-less dangle back to a record it
    doesn't ingest.

    Spec backlink: docs/plans/2026-07-07-spinoff-provenance-ancestry.md § C7;
    shape mirrors cockpit's hand-authored swap seam (example-cockpit-repo
    `src/lib/contract/ancestry-origin.ts` `foreignOriginTripleSchema`).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    """The artifact's stable id (namespace-prefixed, e.g. `gol-…`)."""
    kind: str
    """
    The artifact kind (e.g. `"goal"`, `"roadmap_creator"`).
    Unknown values are forward-compat — carry through, render as muted "other".
    """
    label: str
    """Human-readable display label for rendering without a back-lookup."""


class _HandoffProducer(BaseModel):
    """
    Namespaced record of who/what produced a handoff, along two independent
    axes. Both axes must round-trip, and the combination of the two must
    keep three states distinguishable: "no ceremony ran" (`op_identity ==
    "hand-authored"`), "session typed nothing this turn" (`typed_command ==
    None`), and "the field stopped resolving" (`typed_command ==
    "unresolved"`) — these must never collapse into a single null.

    Spec backlink: docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md § C6a.
    """

    model_config = ConfigDict(extra="forbid")

    op_identity: ProducerOpIdentity
    """Machine vs. human authorship. `hand-authored` has a truth-condition
    independent of `typed_command` — it does not depend on what (if
    anything) the session typed."""
    typed_command: ProducerTypedCommand | str | None
    """A normalized coordinator command name (plain `str`), `"other-
    command"` (a typed slash verb outside coordinator's own set),
    `"unresolved"` (capture failure), or `None` (machine-minted with
    nothing typed this turn). D9: nullable, never optional."""


DeploymentState = Literal[
    "awaiting_gate", "ready_to_fire", "in_flight", "shipped", "continued", "closed"
]
"""
Delivery lifecycle progression for the workstream associated with a handoff.
Orthogonal to `HandoffStatus` — see the `HandoffStatus` docstring for the
stage/`deployment_state` partition explanation.

**`abandoned` split into `continued`/`closed` (DR-084).** The old single
terminal-without-shipping value collapsed two epistemically distinct cases:
a dead-holder node WITH a successor (now `continued`, paired with the
required `continued_into` successor reference — an automated writer MAY
stamp this only on positive succession proof) and a deliberate stop WITHOUT
a successor (now `closed`, paired with the required `closed_reason`;
human/session decision only, never automated). See `continued_into` and
`closed_reason` on `HandoffSummary` below.

Spec backlink: `pln-handoff-lifecycle-vocabulary-o-22ada6` § C6.
"""


class _ShippedIn(BaseModel):
    """Resolved commit sha + date when the workstream shipped. Anonymous nested shape (inlined, no $ref)."""

    model_config = ConfigDict(extra="forbid")

    sha: str
    date: IsoDate


class _AcceptanceCriteria(BaseModel):
    """
    Metadata-only progress ratio: done/total acceptance-criterion count.
    Anonymous nested shape (inlined, no $ref).
    """

    model_config = ConfigDict(extra="forbid")

    done: _SafeInt
    total: _SafeInt


class HandoffSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor."
        )
    )
    """Injected by connector/emitter from which repo it was read."""
    coordinator_root_path: str
    title: str
    created: IsoDate
    status: HandoffStatus
    kind: HandoffKind
    baton_class: BatonClass | None
    """Derived from `kind` at emit time by the one canonical function — never read from
    frontmatter. NULLABLE, never optional (D9), and the null case is load-bearing rather
    than defensive: `baton_class()`'s actual contract nulls on ANY `kind` absent from the
    `x-baton-class` mapping, not on `spike-result` specifically — `spike-result` is simply
    the one unmapped value in the CURRENT `HandoffKind` enum, kept so archived records
    carrying it are not silently dropped from the emission (a spike result is not a baton,
    so it has no class). A future `HandoffKind` addition landed without a matching
    `x-baton-class.mapping` entry nulls the same way, silently, unless the schema-parity
    coverage this migration's C2 chunk owns catches the gap. A required non-nullable field
    here would fail emit on every archived spike-result record. Legacy pre-rename kinds DO
    resolve — they canonicalise through the alias map first, so an archived `spinoff-roadmap`
    emits `intention`, not null."""
    deployment_state: DeploymentState
    workstream: str
    predecessor: str
    """Handoff path or "none"."""
    scope: list[str]
    """Affected file paths (frontmatter `scope:` list) — stored as JSON array in tc-5."""
    claimed_by: str | None
    """Session id that claimed this handoff (frontmatter `claimed_by:` field). D9: nullable, never optional."""
    claimed_at: IsoDateTime | None
    """ISO-8601 UTC timestamp when the handoff was picked up (frontmatter `claimed_at:` field). D9: nullable, never optional."""
    continued_into: str | None
    """Successor handoff id-or-path. Present (required, cross-field-enforced upstream) when
    `deployment_state` is `continued`; the positive succession proof — an automated writer
    that cannot name the successor cannot stamp `continued`. D9: nullable, never optional."""
    closed_reason: Literal["cancelled", "displaced", "stale"] | None
    """One-line reason the workstream closed without shipping or continuing. Present (required,
    cross-field-enforced upstream) when `deployment_state` is `closed`. Closed list, extended
    only by DR (DR-084 Addendum). D9: nullable, never optional."""
    shipped_in: _ShippedIn | None
    """Resolved commit sha + date when the workstream shipped. Cockpit wants the date for timeline rendering. D9: nullable, never optional."""
    picked_up_by: str | None
    """Session id that picked up the handoff. D9: nullable, never optional."""
    acceptance_criteria: _AcceptanceCriteria | None
    """
    Metadata-only progress ratio: done/total acceptance-criterion count. NO raw
    criterion text — privacy hard constraint for the all-staff web tier; the
    full typed per-item array is the structured-handoff spinoff's deliverable.
    D9: nullable, never optional.
    """
    provenance: ProvenanceEnvelope

    # Optional (true absence-allowed) AND `.nullable()` — the ONE
    # `.nullable().optional()` combo observed in this corpus (Zod source:
    # `z.array(z.string()).nullable().optional()` / `z.string().nullable().
    # optional()`): absent-on-omit AND null-tolerant-when-present. Unlike a
    # plain `.optional()` field (unwrapped to bare T by emit_schema.py's
    # `_unwrap_optional_non_nullable`), this genuinely keeps the
    # `anyOf: [T, null]` shape — the `x-zod-nullable-optional` marker below
    # tells that unwrap pass to leave it alone; the marker itself never
    # reaches the emitted JSON Schema (stripped by `_strip_pydantic_noise`).
    # NOTE: no `Field(description=...)` here — the Zod source carries a
    # JSDoc `/** ... */` comment (Python docstring below, dev-facing only)
    # but never calls `.describe()`, so the committed schema.json carries
    # no `description` keyword for either field; adding one would be a
    # port-introduced field this entity's Zod source never emits.
    additional_predecessors: list[str] | None = Field(
        default=None,
        json_schema_extra={"x-zod-nullable-optional": True},
    )
    """
    Fan-in lineage: SHA refs of additional predecessor handoffs beyond
    `predecessor`. Present only when a handoff has multiple parents
    (fan-in merge point). Version-neutral optional — absent on all existing
    records; null tolerated from producers that emit the key explicitly
    without a value.
    """
    forked_from: str | None = Field(
        default=None,
        json_schema_extra={"x-zod-nullable-optional": True},
    )
    """
    Fan-out lineage: SHA ref of the handoff this one was forked from.
    Present only on spinoff-branch handoffs that track forked_from ancestry
    for render/lineage purposes (NOT aggregated into LoE). Version-neutral
    optional — absent on all existing records; null tolerated from
    producers that emit the key explicitly without a value.
    """
    disposed_successors: list[str] | None = Field(
        default=None,
        json_schema_extra={"x-zod-nullable-optional": True},
    )
    """
    Parent-declares-children lineage: SHA refs of successor handoffs that
    were disposed (deleted) by `/distill` disposal, preserved so the
    lineage isn't severed. Present only on handoffs whose successors were
    disposed. Version-neutral optional — absent on almost all records; null
    tolerated from producers that emit the key explicitly without a value.
    """

    # Deliverable spine identity fields (D9 present-as-null).
    # Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § C1.
    deliverable_id: str | None
    """Durable join key — minted at the earliest artifact, carried verbatim by all downstream artifacts of the same deliverable. Null during the pre-backfill window."""
    plan_id: str | None
    """Stable id of the plan whose execution this handoff records; null if no plan is associated or the plan predates threading."""
    initiative: str | None
    """Initiative FK — references state/initiatives/<id>.yaml. Null when no initiative attaches."""
    caption: str | None
    """Business-legible one-sentence outcome line (CEO-board caption). Authored in frontmatter; null when not set."""
    status_reason: str | None
    """Free-text one-liner explaining the lifecycle status (e.g. "abandoned — superseded by X")."""
    owner: str | None
    """Authoring workstream/EM owner."""
    last_meaningful_activity: IsoDateTime | None
    """Derived at emit: ISO-8601 UTC timestamp of the most-recent commit touching this deliverable's artifacts."""
    workstream_type: WorkstreamType | None
    """Derived at emit: normalized handoff category (workstream type), from the already-normalized `category` field (C4 projection)."""
    shipped_sha: str | None
    """Derived at emit: verified merge SHA (ancestor of origin/main). Null if not yet merged or fetch failed."""
    deliverable_status: DeliverableStatus | None
    """Derived at emit: deliverable lifecycle status. Named `deliverable_status` (not `status`) to avoid collision with the per-handoff `status: HandoffStatus` field."""

    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """

    # ── Ancestry-origin fields (D9 present-as-null) ──────────────────────
    # Spec backlink: docs/plans/2026-07-07-spinoff-provenance-ancestry.md § C7.
    #
    # Per-kind residency split (ratified, coordinator-claude contract-shape decision — do not
    # re-open): session/handoff/plan_id are EMITTED kinds (a HandoffSummary
    # row or, for plan_id, a resolvable id already carried elsewhere in this
    # contract) so a bare id suffices for cockpit to resolve. origin_goal_id
    # is a FOREIGN kind (goal records are not emitted in this contract) so we
    # emit the full `{id, kind, label}` triple to avoid a stub-less dangle.

    origin_session: str | None
    """The session UUID that spawned this artifact. Bare id (emitted-kind). D9: nullable, never optional."""
    origin_handoff: str | None
    """The handoff path that spawned this artifact. Bare id (emitted-kind). D9: nullable, never optional."""
    origin_plan_id: str | None
    """The plan id (`pln-…`) this artifact originates from. Bare id (emitted-kind). D9: nullable, never optional."""
    origin_goal_id: list[ForeignOriginTriple] | None
    """
    The goal(s) that anchor this artifact. FOREIGN KIND — not emitted
    elsewhere in this contract — so we emit an array of full
    `{id, kind, label}` triples so cockpit can render without a back-lookup.
    D9: nullable, never optional.
    """

    # ── Wire-level handoff_id derivation (C4) ────────────────────────────
    # Spec backlink: emit/sections/handoffs.py `collect()` — see that module's
    # docstring for the full rationale (basename-not-provenance.path keying,
    # cross-repo collision guard, and why this has no direct precedent in the
    # origin_*/filename-as-identity families).
    handoff_id: str
    """
    Stable identity key for this handoff record: the authored `hnd-<slug>-<6hex>`
    frontmatter value when present, else a value deterministically derived from
    (repo, basename). Always populated — never null. See `handoff_id_derivation`
    for which case produced this value.
    """
    handoff_id_derivation: Literal["authored", "derived"]
    """Which case produced `handoff_id`: `authored` (frontmatter carried a valid
    `hnd-<slug>-<6hex>` id) or `derived` (synthesized from (repo, basename))."""

    # ── Priority-ledger resolution (derived, C6a) ────────────────────────
    # Spec backlink: docs/plans/2026-07-26-priority-ledger.md § C6a. All four
    # populated by `coordinator_core.ops.emit.priority_resolve.resolve_priority`
    # — the SOLE resolution implementation (see that module's docstring); this
    # section never re-walks the predecessor spine or re-implements the walk.
    pm_priority: Literal["urgent", "high", "medium", "low"] | None
    """Resolved effective priority for this handoff (`urgent`/`high`/`medium`/`low`,
    or null when unset/ambiguous/explicitly-cleared), per the nearest-explicit-
    ancestor algorithm. D9: nullable, never optional.
    Review: code-reviewer -- Finding 5 -- narrowed from bare `str | None`; the
    resolver's `"none"` sentinel is normalized to `None` before reaching this
    field (`priority_resolve._priority_value`), so `str` was wider than any
    value this field can actually carry, and `HandoffSummary` (`extra="forbid"`)
    is the cheapest place to fail loud on a stray value."""
    pm_priority_origin: Literal["explicit", "inherited", "suggested", "none", "ambiguous"] | None
    """Which resolution step produced `pm_priority` — see `priority_resolve`'s
    module docstring for the four-step algorithm. D9: nullable, never optional."""
    pm_priority_source_id: str | None
    """
    DERIVED-ONLY ancestor pointer: the `handoff_id` of the ancestor whose
    explicit ledger entry `pm_priority` was inherited from. Populated only
    when `pm_priority_origin == "inherited"`; null otherwise (including
    "explicit", where the source IS this record itself).

    This field is the ONLY place a resolved-from-ancestor pointer may live
    in this contract. It must NEVER become an authored field on a handoff
    or a ledger entry — a persisted resolved-from pointer would be a fourth
    lineage axis, and is exactly the shape the rejected priority-stamping
    design would smuggle back in (see priority-ledger.schema.json
    NEGATIVE-SPEC (2), coordinator-claude repo). This field exists precisely so
    that design never needs reviving. D9: nullable, never optional.
    """
    suggested_priority: str | None
    """The record's own frontmatter `suggested_priority` value, passed
    through unresolved — the resolution algorithm's own step-3 fallback
    input, not itself a resolved value. D9: nullable, never optional."""

    # ── Producer axis (C6a) ───────────────────────────────────────────────
    # Spec backlink: docs/plans/2026-08-12-producer-axis-on-the-baton-contract.md § C6a.
    # Model + emit pass-through only — the resolver that supplies the value
    # is a separate chunk; this field carries null until that chunk lands.
    producer: _HandoffProducer | None
    """
    Namespaced op_identity/typed_command producer record — see
    `_HandoffProducer`. D9: nullable, never optional (required-with-null:
    present-as-null, never an absent key — `extra="forbid"` makes an
    unknown emitted key a hard validation failure)."""


# ── Backlog item summary (debt / bug / improvement YAML) ────────────────────

BacklogType = Literal["debt", "bug", "improvement"]

BugSeverity = Literal["P0", "P1", "P2", "P3"]
"""Bug severity ladder (P3 observed on-disk alongside the corpus-noted P0-P2)."""

BacklogQueueScope = Literal["central", "project"]
"""Backlog queue scope — discriminates the central universal queue from per-project backlogs (C-F4)."""


class BacklogItemSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: BacklogType
    """debt | bug | improvement — the type tag discriminating which queue this came from."""
    id: str
    created: IsoDate
    status: str = Field(
        description=(
            "Backlog-item lifecycle state, passed through verbatim from the "
            "source queue/backlog YAML. Canonical authoring vocabulary "
            "(coordinator/docs/wiki/{debt,bug,improvement}-backlog-schema.md "
            "§ Status enum): base {open, closed, deferred} for every type; "
            "the bug type additionally emits wontfix. closed/wontfix denote "
            "closure and deferred a set-aside (both carry closed_at on "
            "disk); open is active. Because emission is a verbatim "
            "pass-through, non-canonical drift tokens (e.g. done) MAY "
            "appear — consumers computing open/closed counts should treat "
            "unrecognized tokens as open (safe bias). Intentionally NOT "
            "enum-gated at the contract level, to avoid quarantining "
            "historical drift."
        )
    )
    title: str
    repo: str = Field(
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor."
        )
    )
    """
    Connector-injected registry shortname of the repo this item was read
    from (D4). Census keying dimension — matches `repo` on HandoffSummary and
    other summary entities. DISTINCT from `from_repo`: `repo` = which repo
    the YAML file lives in (connector-injected); `from_repo` = YAML-authored
    authoring-EM identity (the `from_repo:` field in the YAML itself).
    """
    from_repo: str = Field(
        description=(
            "Coordinator queue-scope origin label — the YAML-authored "
            "from_repo: authoring-EM identity (e.g. 'claude-central-em', "
            "'example-cockpit-repo-em'). This is NOT a repository identity. "
            "Free-text and unvalidated; known to drift (e.g. '.claude', "
            "bare 'claude-klabauter', underscore variants). Do NOT use it as "
            "a join key, BucketId, or ACL anchor — use `repo` "
            "(owner-qualified '<owner>/<repo>') for the canonical "
            "cross-entity join."
        )
    )
    coordinator_root_path: str
    queue_scope: BacklogQueueScope
    """
    Whether this item came from the central universal queue
    (`coordinator-improvement-queue.md`) or a per-project backlog
    (`state/{debt,bug,improvement}-backlog/`). Non-nullable (C-F4).
    """
    severity: BugSeverity | None
    """Present only for bug items; null otherwise."""
    risk: str | None
    """
    Present only for debt items; null otherwise. Free-text on disk (the
    debt-backlog `risk:` field is a prose sentence, not an enum — verified
    against state/debt-backlog/*.yaml), so tc-5 stores this as a TEXT column.
    """
    provenance: ProvenanceEnvelope

    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """


# ── Review-trail record (from state/review-trail/*.json) ────────────────────
#
# NOTE — intentionally dropped on-disk fields: the on-disk review-trail JSON
# also carries `scope` (chain|session), `scope_kind` (diff|plan|integration),
# and `session_id` (the authoring session). These fields are consumed
# elsewhere for scope accounting but are NOT emitted to this
# cockpit entity. Do not add them to the pydantic model without a
# tc-3/tc-4/tc-5 migration plan.

ReviewVerdict = Literal["ok", "warn", "blocked", "waived"]


class ReviewTrail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor."
        )
    )
    """Injected by connector/emitter."""
    coordinator_root_path: str
    sha_range: str
    reviewer: str
    verdict: ReviewVerdict
    diff_loc: _SafeInt
    """Lines of diff reviewed."""
    reviewed_at: IsoDateTime
    """
    ISO-8601 UTC — the review's own date. The review-trail JSON body carries
    no canonical date field (verified across state/review-trail/*.json); the
    date is encoded in the filename (`YYYY-MM-DD-...json`), so the
    connector/emitter injects it here. tc-5 needs this for `WHERE
    reviewed_at BETWEEN ...` queries — provenance.observed_at is the
    OBSERVATION time, not the review date.
    """
    workstream: str | None
    """
    Join key back to the handoff's `workstream` slug. D9: nullable, never
    optional — archived review-trail records predate this field;
    tolerant-reader rule: present-as-null for records that lack it.
    """
    provenance: ProvenanceEnvelope

    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
