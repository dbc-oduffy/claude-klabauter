"""
ProvenanceEnvelope — the mandatory source-grounding record on every cockpit
fact. Pydantic port of example-doctrine-repo `coordinator/cockpit-contract/src/provenance.ts`
(Zod source) — see that file's doc-comments for the full D9/DR-301 design
rationale this port preserves byte-decision-identical.

Provenance is a real schema, not a label (the Data Science Reviewer P1-D2): `observed_at` and
`derivation` are load-bearing — without them a dashboard number has no
trustworthiness or staleness signal, so both are required and non-nullable.

`ref` is a structured object (the Data Science Reviewer P2), NOT a flat "work/...@sha" string.

THE load-bearing pydantic gotcha (per the T4e port recipe § 1): a Zod
`.nullable()` field (present-as-null, key MUST be present) ports to
`field: T | None` with **no default** — NOT `Optional[T] = None`. Adding a
default makes the key omittable on input parse (wrong — Zod rejects a missing
key) and drops the field from the emitted JSON Schema `required` array
(byte-identity regression). Every `.nullable()` field below (`ref`,
`entity_anchor`) is deliberately written with no default for this reason.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
Freeze: example-doctrine-repo scratch/subagent-sandbox/bash-to-python-engine-migration/freeze-provenance-envelope.md
(the 4 allOf/if/then conditional clauses this model's 3 model_validators
runtime-enforce are pinned there verbatim; JSON-Schema-level re-injection of
those clauses is the `emit-schema.ts`-equivalent entrypoint port, T4e-c —
NOT this module's job — this module owns runtime accept/reject parity only).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coordinator_core.contract.cockpit_schema.common import IsoDateTime

SourceKind = Literal[
    "github_graphql",
    "github_rest",
    "git_commit",  # raw git-log / commit-object reads — git-backed, non-null ref
    "local_fs",
    "coordinator_artifact",  # query-records.js output, orientation_cache, etc.
    "transcript_summary",  # consumer-derived session-transcript summary — not git-backed, ref-null
    "sec_edgar",  # regulatory-filing provenance source, SEC EDGAR XBRL HTTP fetch — not git-backed, ref-null
    "code_comparison",  # deep-research code-comparison emission mode — not git-backed, ref-null
]
"""Where a datum physically came from."""

Derivation = Literal["raw", "parsed", "rolled_up", "computed", "synthesized"]
"""How far a datum is from its raw source. `computed` = derived from multiple source files
(e.g. a DAG edge); `synthesized` = agent-derived from other facts rather than fetched,
parsed, or aggregated (D42, member-only additive)."""

_GIT_BACKED_KINDS = frozenset({"github_graphql", "github_rest", "git_commit"})
_NON_GIT_KINDS = frozenset(
    {
        "local_fs",
        "coordinator_artifact",
        "transcript_summary",
        "sec_edgar",
        "code_comparison",
    }
)


class Ref(BaseModel):
    """The git ref a fact was observed at — branch + tip SHA, stored split in tc-5."""

    model_config = ConfigDict(extra="forbid")

    branch: str
    sha: str


class EntityAnchor(BaseModel):
    """
    Entity-first join anchor — lets a claim with NO repo (e.g. "Electronic
    Arts" has no git repo to anchor on) join the frozen identity space
    directly. `kind` is a plain str, NOT a closed enum (DR-301). Present-as-
    null sibling to `repo` (model A) on `ProvenanceEnvelope` — see that
    model's docstring.

    Spec backlink: docs/plans/2026-07-14-entity-first-anchor-provenance-envelope.md
    (identity-space-freeze DR, chunk C1).
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    value: str


class ProvenanceEnvelope(BaseModel):
    """
    Mandatory source-grounding record on every emitted cockpit fact.

    Runtime-enforces the 3 chained Zod `.superRefine()` guards (ref-guard →
    anchorless-guard → well-formedness-guard) as 3 `model_validator(mode="after")`
    methods, declared and therefore executed in that same order — pydantic v2
    runs `mode="after"` validators in declaration order, preserving Zod's
    chain-order error-message-first-hit semantics on a multi-violating input.
    """

    model_config = ConfigDict(extra="forbid")

    source_kind: SourceKind
    repo: str = Field(
        description=(
            "Owner-qualified repo identity: '<owner>/<repo>'. Owner carries the "
            "producing repo's own casing (producer-authoritative — there is no "
            "independent casing authority); no '.git' suffix. Consumers must not "
            "assume pre-normalization: normalizing to lower(owner)/lower(repo) for "
            "join, dedup, or storage keying is conformant. See DR-022. This "
            "owner-qualified string is the canonical cross-entity join anchor. "
            "repo: '' means the fact is NOT repo-anchored — see entity_anchor "
            "(entity-first claims with no repo to join on)."
        )
    )
    # BIDIRECTIONAL invariant (D9 / D1), runtime-enforced by
    # `_check_ref_git_backed_directionality` below:
    #   github_graphql / github_rest / git_commit → ref MUST be non-null (git-backed)
    #   local_fs / coordinator_artifact / transcript_summary / sec_edgar /
    #   code_comparison → ref MUST be null (not git-backed)
    # present-as-null (no default — see module docstring).
    ref: Ref | None
    path: str
    observed_at: IsoDateTime
    derivation: Derivation
    # Entity-first join anchor — present-as-null (no default — see module docstring).
    entity_anchor: EntityAnchor | None

    @model_validator(mode="after")
    def _check_ref_git_backed_directionality(self) -> "ProvenanceEnvelope":
        if self.source_kind in _GIT_BACKED_KINDS and self.ref is None:
            raise ValueError(
                "ref is required for git-backed sources (github_graphql, github_rest, git_commit)"
            )
        if self.source_kind in _NON_GIT_KINDS and self.ref is not None:
            raise ValueError(
                "ref must be null for non-git sources (local_fs, coordinator_artifact, "
                "transcript_summary, sec_edgar, code_comparison)"
            )
        return self

    @model_validator(mode="after")
    def _check_anchorless_guard(self) -> "ProvenanceEnvelope":
        # Identity-space-freeze anchorless guard (the Data Science Reviewer F6, tightened): a fact
        # can't be anchorless. repo != "" OR (entity_anchor is not None AND
        # entity_anchor.value != ""). Whole-object invariant — either `repo`
        # or `entity_anchor` can be the fix, so the error is not pinned to
        # either field.
        is_repo_anchored = self.repo != ""
        is_entity_anchored = self.entity_anchor is not None and self.entity_anchor.value != ""
        if not is_repo_anchored and not is_entity_anchored:
            raise ValueError(
                "fact must be anchored: repo must be non-empty, or entity_anchor "
                "must be present with a non-empty value"
            )
        return self

    @model_validator(mode="after")
    def _check_entity_anchor_well_formed(self) -> "ProvenanceEnvelope":
        # Well-formedness guard, UNCONDITIONAL on `repo`: a present
        # entity_anchor must be non-empty `kind` AND non-empty `value`
        # regardless of whether `repo` also anchors the fact (catches the
        # malformed composite the anchorless guard alone would miss: real
        # `repo` paired with `entity_anchor: {kind: "", value: ""}`).
        if self.entity_anchor is not None and (
            self.entity_anchor.kind == "" or self.entity_anchor.value == ""
        ):
            raise ValueError(
                "entity_anchor, when present, must be well-formed: kind and "
                "value must both be non-empty"
            )
        return self


class ContentHash(BaseModel):
    """
    R5 content-hash change-signal — sha256 of the raw source file body, emitted
    by claude-klabauter on each work-state record so example-retrieval-repo can invalidate its
    projection cache on change (keyed on (source_path, hex)). NOT mtime, NOT
    git-rev. Frozen shape: claude-klabauter coordinator_core/contract/change-signal.schema.json.
    Spec: example-retrieval-repo-producer-contract.md § 3.2 (hash convention), § 3.3 (wire shape).
    """

    model_config = ConfigDict(extra="forbid")

    algo: Literal["sha256"]
    hex: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "sha256 over the raw bytes of the whole source artifact at "
            "`source_path` — file-granular. This is a CHANGE-SIGNAL for "
            "consumer cache-invalidation on `(source_path, hex)`, NOT a "
            "record identity. See DECISIONS.md § D32."
        ),
    )
    source_path: str
