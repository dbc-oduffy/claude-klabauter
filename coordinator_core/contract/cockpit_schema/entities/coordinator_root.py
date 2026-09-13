"""
CoordinatorRoot — the unit of work-state. Pydantic port of DoE
`coordinator/cockpit-contract/src/entities/coordinator-root.ts` (Zod source).

Keyed on `(repo, coordinator_root_path)`, NOT `repo` alone (the Data Science Reviewer P2-D7): a
monorepo (example-stats-repo) or sibling-pair (example-retrieval-repo + example-retrieval-repo-ue-addon)
must not silently collapse to one key. `last_activity_at` is sourced from the
branch-tip `committedDate`, never `pushedAt` — `pushedAt` lies on dormant repos
touched by coordinator sweeps (github-connector corpus § staleness signals).

NULLABILITY CONTRACT (tc-3/tc-4): nullable fields are present-as-null (no
default), never absent (DECISIONS.md § D9). `open_pr_count` is GitHub-only
(tc-4 connector populates it); non-GitHub producers (e.g. tc-3 local emission)
emit null.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import (
    IsoDateTime,
    MachineSlug,
    OwnerSlug,
    Visibility,
)
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

# Zod `z.number().int()` emits JSON Schema `integer` bounded to JS
# Number.MIN_SAFE_INTEGER/MAX_SAFE_INTEGER — reproduced here so
# model_json_schema() byte-matches the committed schema's `minimum`/`maximum`.
SafeInt = Annotated[int, Field(ge=-9007199254740991, le=9007199254740991)]


class CoordinatorRoot(BaseModel):
    """The unit of work-state, keyed on (repo, coordinator_root_path)."""

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
    owner: OwnerSlug
    coordinator_root_path: str
    """Relative to repo root; "." for single-root repos."""
    machine: MachineSlug
    """
    Hostname slug of the machine this root was observed on — the `@machine`
    component of the `owner/repo@machine` fleet identity.

    D11 (string-not-enum): the machine-slug set is unstable (it drifted
    mid-stream when machines were renamed/added); an enum would reproduce the
    exact class of bug D11 documents. Required string, always present.

    This is the SAME hostname source as the envelope's `emitted_by_machine`,
    NOT the branch-name-parsed `Branch.machine_hint` — those can disagree.
    """
    visibility: Visibility
    archived: bool
    is_fork: bool
    """From GitHub census `isFork`; closes the field-gap vs tc-4 census / tc-5 repos table."""
    default_branch: str
    last_activity_at: IsoDateTime
    """ISO-8601 UTC — branch-tip committedDate, NOT pushedAt."""
    open_pr_count: SafeInt | None
    """Open PR count from GitHub census; null for non-GitHub producers (present-as-null, D9)."""
    open_review_count: SafeInt | None = Field(
        default=None,
        json_schema_extra={"x-zod-nullable-optional": True},
    )
    """
    Count of open reviews for VCS producers whose review unit is not a GitHub
    PR. Perforce: shelved changelists (`p4 changes -s shelved`) — pending
    changelists are excluded, being readable only in their own client and so
    the analogue of a dirty working tree, not of a review.

    D9 shape: NULLABLE-OPTIONAL (`anyOf: [integer, null]`, out of `required`),
    which the `x-zod-nullable-optional` marker above is what actually buys —
    NOT the `| None` annotation. `SafeInt | None = None` without the marker
    emits omit-when-absent (bare integer, null ILLEGAL), and a producer
    writing null on a refusal would fail its own published schema. All three
    states are load-bearing here: absent = not collected, null = the server
    refused, integer = a count.

    VERSION-NEUTRAL OPTIONAL, not D9-required — the same carve-out
    `content_hash` takes in this entity, and the shape 4.3.0 and 4.4.0 both
    chose (roadmap_id, actioned_at). Landing these in `required` on an
    additionalProperties:false entity is a MAJOR that DoE's tooling
    hard-throws on; absent here means "producer predates the field", a
    transition-window fact, not the absent/null confusion D9 exists to stop.

    Null spans BOTH "the server refused" (MaxScanRows /
    MaxResults is routine Helix group config, and refusal is not truncation)
    and "not collected" — a bare integer would collapse either into 0, which
    a reader cannot tell from "nothing is in review".

    Which review system this counts is NOT carried by a sibling label field:
    `provenance.source_kind` already discriminates the producer (p4_server /
    p4_workspace), and a second discriminant that could only ever hold one
    value would invite consumers to branch on it instead of on provenance.
    """
    open_review_count_is_lower_bound: bool = False
    """
    True iff `open_review_count` is a floor rather than an exact count — the
    query ran capped (`p4 changes -m N` returned N rows) and the true count is
    >= it. Renders the difference between a chip reading `50` and one reading
    `50+`.

    FALSE whenever `open_review_count` is null: a null count asserts no bound,
    so there is nothing for this flag to qualify. Reader's rule is one line —
    read this flag only when the count is non-null.

    Always false on GitHub-backed roots: GitHub's PR count has no refusal or
    cap state, so `open_pr_count` needs no companion flag.

    AGGREGATION: any surface that SUMS `open_review_count` across roots must
    OR this flag across the same inputs and render the total as `N+`. A rollup
    that adds the integers and drops the flags understates silently the moment
    one contributor is capped — worse than the bare integer this shape
    rejects, because it launders the loss through an aggregate nobody doubts.
    """
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up
    aggregates, empty-path computed records). Version-neutral optional —
    absent on all existing records. Spec: producer-contract § 3.3.
    """
