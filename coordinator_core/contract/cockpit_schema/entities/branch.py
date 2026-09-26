"""
Branch — a single ref observation, keyed on `(repo, branch_name, tip_sha)`.
Pydantic port of DoE `coordinator/cockpit-contract/src/entities/branch.ts`
(Zod source).

Branch-fact data-quality defenses (the Data Science Reviewer P1-D5, github-connector corpus):
 - `tip_sha` is the observation key: a tip-SHA change is a NEW observation, not
   name-continuity. Daily-branch renames appear as delete+create to a naive
   differ; SHA-keying makes that detectable.
 - `merge_base_sha` is stored alongside `ahead_by` — a rebase moves the merge
   base and silently changes `ahead_by` with no new work. Without it the cockpit
   shows a velocity spike that is a history-rewrite artifact.
 - `ahead_by` / `behind_by` come from the REST compare endpoint and may be
   uncomputed at census time, hence nullable. `merge_base_sha` is null when
   `ahead_by` was not computed.
 - machine/date hints are parsed from `work/{machine}/{date}`; null when the
   branch name does not match the pattern (e.g. `main`, `feature/*`).

NULLABILITY CONTRACT (tc-3/tc-4): every nullable field below is present-as-null
(`field: T | None` with no default), NOT absence-allowed — the key MUST be
present in the emitted payload, carrying `null` when uncomputed (e.g.
`ahead_by: null` before the REST compare runs). Omitting the key entirely
fails validation. See DECISIONS.md § D9.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.common import IsoDateTime, OwnerSlug
from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope

_MAX_SAFE_INTEGER = 9007199254740991


class Branch(BaseModel):

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
    name: str
    tip_sha: str
    merge_base_sha: str | None
    ahead_by: int | None = Field(ge=-_MAX_SAFE_INTEGER, le=_MAX_SAFE_INTEGER)
    behind_by: int | None = Field(ge=-_MAX_SAFE_INTEGER, le=_MAX_SAFE_INTEGER)
    # ISO-8601 UTC; committedDate on the tip commit.
    last_commit_at: IsoDateTime
    last_commit_message: str
    machine_hint: str | None
    date_hint: str | None
    provenance: ProvenanceEnvelope
    content_hash: ContentHash | None = None
