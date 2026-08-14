"""
CrossRepoMemoSummary — outstanding cross-repo memos as a queryable snapshot
entity for the cockpit. Pydantic port of DoE
`coordinator/cockpit-contract/src/entities/cross-repo-memo-summary.ts` (Zod source).

METADATA-ONLY: no memo bodies ship to the all-staff web tier. The cockpit
stores only the structural envelope (from/to, status, kind, created date,
related paths) so the dashboard can surface actionable queue depth without
exposing private deliberation content.

BOARD-PUBLIC FIELD — `title`: memo titles ARE visible to all staff on the
Cockpit dashboard (PM-ratified 2026-06-24). Authors are warned of this via
the cross-repo-memo authoring norm documented in
`docs/wiki/cross-repo-communication.md`. No redaction is applied here.

Spec backlink: docs/plans/2026-06-24-cockpit-cockpit-contract-reshape.md
Ask 7 of the cockpit-contract reshape (chunk C6-entity).
Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
Spec backlink: pln-take-ownership-of-the-cross-re-ac97ef § C6 —
  additive `archived` + `decision_note` fields (return-path/queryability feed; DEC-2/DEC-3).
Spec backlink: pln-take-ownership-of-the-cross-re-ac97ef § C8 —
  additive `body` field (full-text content search, bounded/capped emission).

NOT METADATA-ONLY AS OF C8: the `body` field ships the memo's full markdown body content
(bounded/capped — see the field docstring below), a deliberate widening of the
METADATA-ONLY posture stated above for the structural envelope fields. PM-directed
2026-07-24 ("no deferrals") — content search over memo prose was previously out-of-scope
(D1) and is now in-scope. Authors are warned via the cross-repo-memo authoring norm
(same channel as the BOARD-PUBLIC `title` warning above) that memo body text is no longer
opaque to the fleet's content-search surface.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

from coordinator_core.contract.cockpit_schema.common import IsoDate
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

_NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class CrossRepoMemoSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    repo: _NonEmptyStr = Field(
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
    Connector-injected: which coordinator root's memo tree this fact was
    observed in. This is the OBSERVATION LOCUS — the repo whose
    `state/memos/` directory the connector scanned. It is DISTINCT from
    `from_` and `to`, which are the memo's authored cross-repo endpoints
    (the originating EM and the destination repo, as written in the memo
    frontmatter). A memo authored by `.example-doctrine-mirror-repo` to `example-repo` read
    from the `.example-doctrine-mirror-repo` tree has `repo: "dbc-oduffy/.example-doctrine-mirror-repo"`,
    `from: ".example-doctrine-mirror-repo"`, `to: "example-repo"`.
    """
    coordinator_root_path: _NonEmptyStr
    """
    Connector-injected absolute filesystem path to the coordinator root
    where this memo was observed (the observation locus; same locus as
    `repo`, expressed as a path for local-FS tooling).
    """
    title: _NonEmptyStr
    """
    The memo title as authored in its frontmatter.

    BOARD-PUBLIC free prose — this field is visible to all staff on the
    cockpit dashboard (PM-ratified 2026-06-24: titles are staff-visible;
    authors are warned via the cross-repo-memo authoring norm). No length
    cap, no redaction applied.
    """
    from_: _NonEmptyStr = Field(alias="from")
    """
    The memo's authored source endpoint — the coordinator root identity
    of the EM that created the memo (frontmatter `from:` field).
    """
    to: _NonEmptyStr
    """
    The memo's authored destination endpoint — the coordinator root identity
    of the receiving repo (frontmatter `to:` field).
    """
    status: Literal["open", "in_progress", "actioned"]
    """
    Primary memo lifecycle state. Only the three live values are modeled
    here. The grandfathered back-compat values (reviewed, action_taken,
    closed, superseded) are NOT modeled — connectors MUST normalise them
    to one of these three before emitting a CrossRepoMemoSummary.
    """
    created: IsoDate
    """ISO calendar date when the memo was created."""
    kind: Literal["ask", "consult", "fyi"]
    """The memo kind discriminator (frontmatter `kind:` field)."""
    related: list[str]
    """
    Related artifact paths cited in the memo (frontmatter `related:` list).
    Required, never null — connectors inject `[]` when absent on disk.
    """
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file (rolled-up aggregates,
    empty-path computed records). Version-neutral optional — absent on all existing
    records. Spec: producer-contract § 3.3.
    """
    archived: bool = False
    """
    True when this row was sourced from the terminal-flipped archive set
    (`cross-repo/archive/*.md`, `records.query type=archived-memo`) rather than the
    actionable inbox (`cross-repo/inbox/*.md`, `type=cross-repo-memo`). Additive field
    with a default — existing consumers built against the pre-2026-07-24 shape see every
    row as if `archived` were absent (defaults False on read), so this is a
    reader-tolerant additive bump, not a breaking one. 2026-07-24 addition
    (DEC-2/DEC-3, plan `2026-07-24-cross-repo-memo-ownership-and-redesign.md` § C6):
    "what have I promised & closed" needs the archived set in the same feed as the open
    asks, not a separate un-emitted corpus.
    """
    decision_note: str | None = None
    """
    Capped excerpt of the source memo's frontmatter `decision_note` field — the
    answer's SUBSTANCE, not just its existence (the terminal `status` field already
    carried the latter). `None` when the memo carries no `decision_note` (e.g. a
    still-open ask with no disposition yet). Bounded, NOT full memo-body text — this is
    a summary-length excerpt (`_DECISION_NOTE_MAX_CHARS` in the section porter), never
    the raw markdown body. 2026-07-24 addition (DEC-2, same plan/chunk as `archived`
    above). Full-body text is the separate `body` field below (C8, 2026-07-24).
    """
    body: str | None = None
    """
    The source memo's full markdown body (the content AFTER the frontmatter block),
    so the fleet can content-search memo prose rather than only filter on frontmatter
    fields. Bounded/capped, NOT unbounded: the section porter truncates a body over
    `_BODY_MAX_CHARS` with a trailing ellipsis rather than emitting it uncapped or
    dropping it — the everyday case (a normal-sized memo) ships in full; the cap is a
    safety valve against a pathological outlier. `None` when the source file could not
    be re-read at emission time (e.g. deleted between the records.query scan and this
    section's body-read pass) — a read-failure omission, distinct from the size-based
    cap. The sibling `content_hash` field (stamped generically over every section's
    records by the envelope's post-collect pass, over the FULL source file — frontmatter
    + body bytes together) lets a downstream consumer detect that the source file's bytes
    changed since some other observation; it is NOT a direct truncation signal for this
    `body` field, since the hash covers frontmatter bytes this field never carries. No
    separate hash-only-fallback field is modeled here regardless. 2026-07-24 addition (C8, plan
    `2026-07-24-cross-repo-memo-ownership-and-redesign.md`, PM-directed "no
    deferrals" — supersedes the D1 out-of-scope note on the earlier `decision_note`
    addition).
    """
