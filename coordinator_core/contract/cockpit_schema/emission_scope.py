"""
EmissionScope — pydantic port of coordinator-claude `coordinator/cockpit-contract/src/emission-scope.ts`
(Zod source). The scope discriminator every ingestable cockpit emission entity
declares, and the per-case envelope invariant it forces (DECISIONS.md § D25).

Closes the "ingestable-by-luck" gap: ingestability rides on a declared scope +
matching envelope, not on each producer happening to carry a `repo` field.

Spec backlink: DECISIONS.md § D25
Spec backlink: cross-repo/inbox/2026-07-13-claude-klabauter-em-emission-scope-envelope-invariant.md
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import MachineSlug
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

EmissionScope = Literal["per-repo", "fleet-derived", "fleet-authored"]
"""The three ingestability scopes an emission entity may declare."""


class FleetAuthoredAuthority(BaseModel):
    """
    Designated-emitter authority for a fleet-authored synthesis (case 3).
    Because a synthesis is divergent text across emitters, a content hash will
    NOT collapse it; a single designated emitter is the disambiguator.
    Per-repo emissions omit it.
    """

    model_config = ConfigDict(extra="forbid")

    emitter: MachineSlug
    """Machine slug of the single authoritative emitter."""
    emitter_repo: str = Field(
        description=(
            "Owner-qualified '<owner>/<repo>' of the designated authoritative "
            "emitter. Producer-authoritative case (consumers may normalize "
            "to lower(owner)/lower(repo); see DR-022), no '.git' suffix."
        )
    )


class PerRepoEmission(BaseModel):
    """Case 1: owner-qualified `repo` + `provenance`."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["per-repo"]
    repo: str = Field(
        description=(
            "Owner-qualified '<owner>/<repo>'. Producer-authoritative case "
            "(consumers may normalize to lower(owner)/lower(repo); see "
            "DR-022), no '.git' suffix. Cross-entity join anchor."
        )
    )
    provenance: ProvenanceEnvelope


class FleetDerivedEmission(BaseModel):
    """Case 2: `content_hash` natural key."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["fleet-derived"]
    content_hash: ContentHash


class FleetAuthoredEmission(BaseModel):
    """Case 3: `authority` designated-emitter."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["fleet-authored"]
    authority: FleetAuthoredAuthority


ScopedEmission = Annotated[
    Union[PerRepoEmission, FleetDerivedEmission, FleetAuthoredEmission],
    Field(discriminator="scope"),
]
"""
The envelope invariant with teeth. The `scope` discriminant forces the
matching envelope so no ingestable entity can omit its scope key:
    per-repo       → owner-qualified `repo` + `provenance`     (case 1)
    fleet-derived  → `content_hash` natural key                (case 2)
    fleet-authored → `authority` designated-emitter             (case 3)
"""
