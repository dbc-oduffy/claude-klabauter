from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coordinator_core.contract.cockpit_schema.common import IsoDateTime

SourceKind = Literal[
    "github_graphql",
    "github_rest",
    "git_commit",
    "local_fs",
    "coordinator_artifact",
    "transcript_summary",
    "sec_edgar",
    "code_comparison",
    "p4_server",
    "p4_workspace",
]
"""Where a datum physically came from."""

Derivation = Literal["raw", "parsed", "rolled_up", "computed", "synthesized"]
"""How far a datum is from its raw source. `computed` = derived from multiple source files
(e.g. a DAG edge); `synthesized` = agent-derived from other facts rather than fetched,
parsed, or aggregated (D42, member-only additive)."""

_VCS_BACKED_KINDS = frozenset(
    {"github_graphql", "github_rest", "git_commit", "p4_server", "p4_workspace"}
)
_GIT_BACKED_KINDS = _VCS_BACKED_KINDS
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

    model_config = ConfigDict(extra="forbid")

    branch: str
    sha: str


class P4Ref(BaseModel):

    model_config = ConfigDict(extra="forbid")

    stream: str
    change: int


class EntityAnchor(BaseModel):

    model_config = ConfigDict(extra="forbid")

    kind: str
    value: str


class ProvenanceEnvelope(BaseModel):

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
    ref: Ref | P4Ref | None
    path: str
    observed_at: IsoDateTime
    derivation: Derivation
    entity_anchor: EntityAnchor | None

    @model_validator(mode="after")
    def _check_ref_git_backed_directionality(self) -> "ProvenanceEnvelope":
        if self.source_kind in _VCS_BACKED_KINDS and self.ref is None:
            raise ValueError(
                "ref is required for VCS-backed sources (github_graphql, github_rest, "
                "git_commit, p4_server, p4_workspace)"
            )
        if self.source_kind in _NON_GIT_KINDS and self.ref is not None:
            raise ValueError(
                "ref must be null for non-git sources (local_fs, coordinator_artifact, "
                "transcript_summary, sec_edgar, code_comparison)"
            )
        return self

    @model_validator(mode="after")
    def _check_anchorless_guard(self) -> "ProvenanceEnvelope":
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
        if self.entity_anchor is not None and (
            self.entity_anchor.kind == "" or self.entity_anchor.value == ""
        ):
            raise ValueError(
                "entity_anchor, when present, must be well-formed: kind and "
                "value must both be non-empty"
            )
        return self


class ContentHash(BaseModel):

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
