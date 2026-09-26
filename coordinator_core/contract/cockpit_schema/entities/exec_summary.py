from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..common import IsoDateTime
from ..provenance import ContentHash, ProvenanceEnvelope


class DocStalenessEntry(BaseModel):

    model_config = ConfigDict(extra="forbid")

    path: str
    """Repo-relative doc path, e.g. 'README.md'."""
    stale: bool
    """`commits_since >= threshold_commits AND days_since >= threshold_days`."""
    commits_since: int = Field(ge=0)
    """Commits landed since the last content-modifying touch to this doc."""
    days_since: int = Field(ge=0)
    """Calendar days since the last content-modifying touch to this doc."""
    last_touch_sha: str
    """SHA of the last content-modifying commit (AC8 filters applied)."""
    last_touch_date: IsoDateTime
    """
    Committer date of `last_touch_sha`. Offset-bearing ISO-8601 (this
    contract's `IsoDateTime` alias, not a bare string) — a naive
    timestamp fails cockpit's strict codegen regex and quarantines the
    whole exec-summary row on ingest, not just this field.
    """
    changed_areas: list[str]
    """Top-level dirs ranked by touch count since `last_touch_sha` (AC6 evidence)."""
    threshold_commits: int = Field(ge=0)
    """The `commits_since` threshold `stale` was derived from — rides
    alongside `stale` so a consumer can render an explained badge without
    seeing the producer's config."""
    threshold_days: int = Field(ge=0)
    """The `days_since` threshold `stale` was derived from — see `threshold_commits`."""


class ExecSummary(BaseModel):
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
    """Connector-injected registry shortname."""
    coordinator_root_path: str
    """Connector-injected — matches other summary entities."""
    id: str
    """
    Stable entity id — equals the repo shortname for this singleton-per-repo
    artifact. The FK target from any future cross-entity join on exec-summary.
    """
    provenance: ProvenanceEnvelope

    project: str | None
    """Project title from frontmatter (`project:`); null if absent."""
    generated: IsoDateTime | None
    """ISO-8601 UTC timestamp from frontmatter (`generated:`); null if absent."""
    generator: str | None
    """Generator script name from frontmatter (`generator:`); null if absent."""
    identity: str | None
    """Body of the MANAGED identity section; null if absent or file not yet generated."""
    progress: str | None
    """Body of the MANAGED progress section; null if absent."""
    what_makes_it_special: str | None
    """
    Body of the HAND special section — what makes this project stand out.
    Fence name on disk: `special`. D9: nullable, never optional.
    """
    near_term_goals: str | None
    """
    Body of the HAND goals section — near-term goals.
    Fence name on disk: `goals`. D9: nullable, never optional.
    """
    docs_staleness: list[DocStalenessEntry] | None
    """
    Per-doc human-facing-doc staleness verdicts (C6,
    `coordinator_core.ops.doc_staleness`). REQUIRED-WITH-NULL per D9 — key
    always present, `[]` means "detector ran, nothing stale", `null` means
    "detector did not run for this repo" (no doc registry, or the repo
    predates the detector). This is the property an optional-and-absent
    shape would collapse: cockpit's loaders need to tell "ran clean" apart
    from "never ran" to render the row correctly (cockpit's 2026-07-28
    reply, note (b)).
    """

    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
