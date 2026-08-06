"""
CoordinatorRoot — the unit of work-state. Pydantic port of example-doctrine-repo
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

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
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
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up
    aggregates, empty-path computed records). Version-neutral optional —
    absent on all existing records. Spec: producer-contract § 3.3.
    """
