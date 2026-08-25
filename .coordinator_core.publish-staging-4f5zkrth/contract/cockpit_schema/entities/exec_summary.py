"""
ExecSummary — per-repo executive-summary entity. Pydantic port of DoE
`coordinator/cockpit-contract/src/entities/exec-summary.ts` (Zod source).

A coordinator-mandated, one-screen "why this project matters" brief that
surfaces the four-section body of `docs/exec-summary.md` into the cockpit
emission envelope. One entity per repo; `id` equals the repo shortname
(singleton-per-repo artifact).

Source artifact: docs/exec-summary.md
  Frontmatter: kind, repo, project, generated, generator
  Body sections (HTML-comment fences):
    MANAGED: identity, MANAGED: progress
    HAND: special (→ what_makes_it_special), HAND: goals (→ near_term_goals)

All four section bodies and the two timestamp/generator frontmatter fields
are nullable (D9: present-as-null) because HAND sections may be unpopulated
on newly-created files and MANAGED sections may be absent before the
generator first runs. `docs_staleness` (C6) is a fifth required-nullable
field, distinct in kind from the six above — it is not sourced from
`docs/exec-summary.md` at all, but from a separate detector
(`coordinator_core.ops.doc_staleness`) over the repo's declared human-facing
doc registry; see that field's own docstring for its null-vs-`[]` contract.

Spec backlink: docs/wiki/exec-summary-artifact.md
Spec backlink: docs/plans/2026-07-03-exec-summary-per-repo-brief.md
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
Spec backlink: DoE-claude:pln-human-facing-doc-staleness-det-d9c047 § C6
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..common import IsoDateTime
from ..provenance import ContentHash, ProvenanceEnvelope


class DocStalenessEntry(BaseModel):
    """
    One declared human-facing doc's staleness verdict — leaf node of
    `ExecSummary.docs_staleness`. Mirrors the "ok"-status shape
    `coordinator_core.ops.doc_staleness.compute_doc_staleness` returns; a
    per-doc `status` other than "ok" (absent path, no content-modifying
    history) is not represented here — the producer only emits an entry
    for a doc it could fully evaluate.
    """

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
    timestamp fails opticon's strict codegen regex and quarantines the
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

    # Nullable fields (D9 present-as-null, NOT optional).
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
    shape would collapse: opticon's loaders need to tell "ran clean" apart
    from "never ran" to render the row correctly (opticon's 2026-07-28
    reply, note (b)).
    """

    # Optional (true absence-allowed), WITH default — inverse of the
    # nullable fields above.
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    makima for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
