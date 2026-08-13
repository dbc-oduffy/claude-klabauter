"""
LessonSummary — one captured lesson from the full {lessons.md} ∪ {lessons-outbox/}
∪ {drained/} union surface. Pydantic port of coordinator-claude
`coordinator/cockpit-contract/src/entities/lesson-summary.ts` (Zod source).

Spec backlink: state/roadmap/cockpit-contract-ext-2026-06-22/COORDINATOR-RESOLUTIONS.md § R4
+ pinned field set (C-F1/C-F2/C-F3/C-F7).

Coverage: ALL lessons, not only promoted ones. The EMITTER is the structuring
boundary — it performs a FULL OUTER JOIN of lessons.md ∪ outbox ∪ drained
(C-F3: NOT a simple LEFT-JOIN; outbox/drained-only entries with no lessons.md
body are also emitted). parse_status is non-nullable on every record; partial
records still carry every field they CAN populate.

C-F5 (revised): `created` is nullable but captured lessons are now born-
attributable — per-entry YAML records carry real `created`/`from_repo`/
`evidence`/`target_wiki` at capture time, so captured entries CAN have
non-null values for these fields. Cockpit date-bounded queries that excluded
on `promotion_state` to dodge null `created` should revisit their filter; the
null-`created` assumption no longer holds for new captures.

`repo` and `coordinator_root_path` are connector-injected (D4), never in
on-disk prose. Nullable fields follow D9 (present-as-null, not optional).

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDateTime
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

LessonScope = Literal["universal", "project"]

LessonPromotionState = Literal["captured", "pending", "drained"]

LessonParseStatus = Literal["ok", "partial"]
"""
parse_status — result of the prose→fields decomposition.
- ok: all required fields fully parsed.
- partial: at least one decomposed field could not be extracted
  (DEGRADED-BUT-COUNTED; the entry is still emitted so "all lessons"
  coverage is honest).
Spec: COORDINATOR-RESOLUTIONS § R4 C-F2.
"""


class LessonSummary(BaseModel):
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
    """Connector-injected registry shortname — census keying dimension. Non-nullable."""
    coordinator_root_path: str
    """Connector-injected — matches other summary entities."""
    lesson_key: str = Field(pattern=r"^[0-9a-f]{16}$")
    """
    Synthetic identity: 16-char lowercase hex hash of normalized title.
    PRIMARY KEY for tc-5. Union-dedup key across {lessons.md} ∪ {outbox} ∪
    {drained/} (C-F1). Shape: first 16 nibbles of SHA-256(normalize(title)) —
    e.g. "a3f9c2e1b4d8f6a7".
    """
    title: str
    """Bold-title from the capture entry."""
    scope: LessonScope
    """Parsed from [universal] tag; 'project' when tag absent — emitter MUST supply, no schema default."""
    body: str
    """
    Full capture-entry body — raw prose for partial records; parsed body for
    ok records. Stored as TEXT in tc-5 (C-F8: side-table deferred to v2 if
    corpus grows).
    """
    promotion_state: LessonPromotionState
    """promotion_state precedence: drained > pending > captured (C-F3)."""
    parse_status: LessonParseStatus
    """Non-nullable on every record — ok for clean decomposition, partial for degraded (C-F2)."""
    provenance: ProvenanceEnvelope

    # Nullable fields (D9 present-as-null). `change_kind` is populated only from
    # outbox/drained records. `target_wiki`, `from_repo`, `evidence`, and
    # `created` can be populated from either the per-entry YAML (born-
    # attributable captures) or the outbox/drained join; null only when absent
    # from all sources.

    change_kind: str | None
    """8-value enum on the outbox/drained record; null for captured-only entries (not in per-entry YAML)."""
    target_wiki: str | None
    """
    Target wiki path. Populated from the per-entry YAML (born-attributable
    captures) or the outbox/drained record; null when absent from both sources.
    """
    from_repo: str | None
    """
    Authoring-EM identity (e.g. "claude-central-em"), from outbox/drained join
    OR the per-entry YAML (born-attributable captures). Answers a different
    question than `repo` (C-F7) — NOT redundant. Null only when neither the
    per-entry YAML nor the outbox record supplies a value; captured lessons
    that carry from_repo in their YAML emit it non-null.
    """
    evidence: str | None
    """
    Free-text evidence from the outbox record or per-entry YAML. Born-
    attributable captured lessons can supply non-null evidence at capture
    time; null only when absent from both the YAML and any outbox/drained
    record.
    """
    created: IsoDateTime | None
    """
    ISO-8601 timestamp. Previously minted only at promotion time; captured
    lessons are now born-attributable and carry real `created` from the
    per-entry YAML written at capture. Null only when absent from both the
    per-entry YAML and any outbox/drained record. C-F5 revised: date-bounded
    queries can no longer assume null-created == not-yet-promoted.
    """
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up
    aggregates, empty-path computed records). Version-neutral optional —
    absent on all existing records. Spec: producer-contract § 3.3.
    """
