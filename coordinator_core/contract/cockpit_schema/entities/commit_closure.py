"""
CommitClosure — one `(repo, item_id, sha)` fact: a commit whose `Closes:`
trailer referenced a work item, plus whether that commit is reachable on the
repo's default branch.

Represents a single closure-reference row as collected by claude-klabauter's
`coordinator_core/ops/emit/sections/commit_closures.py` porter from a `git
log` scan of `Closes:` trailers (DECISION-2/DECISION-3). Multi-commit
resolution ("is item X actually closed") is cockpit's read-side query over
these rows, not a write-time claude-klabauter concern (DECISION-4).

A row is either a CLOSE row (`reverts_sha` null, from a `Closes:` trailer match) or a REVERT
row (`reverts_sha` non-null, from git's own auto-generated revert-body linkage) — never both.
See `reverts_sha`'s own field docstring for the revert-row shape (C3, DR-318 §D4/D8).

Spec backlinks:
  - docs/plans/2026-07-17-commit-closure-emission-fact.md § Chunk C2, AC2, AC3
  - docs/plans/2026-07-17-commit-closure-emission-fact.md § DECISION-4
  - docs/plans/2026-08-18-sat-07-tier-a-wiring.md § Chunk C3, DR-318 §D4, §D8

Logical identity: (repo, item_id, sha). A re-close, cherry-pick, or
trailer copy-paste that lands the same item_id in two commits emits TWO
distinct rows (one per distinct triple) — no cross-commit dedup at write
time, since each row is a genuine distinct fact. `coordinator_root_path` is
an ADDITIVE connector key present on every connector-emitted entity — it is
NOT part of the logical identity (mirrors `roadmap_dag_node.py`'s identity
note).

"Default branch" in `reachable_on_default_branch` resolves to `origin/main`
verbatim — matching `_stamp_shipped_sha`/`_sha_on_origin_main` semantics
(resolvers.py) — not a per-repo-configurable default-branch lookup.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope


class CommitClosure(BaseModel):

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
    coordinator_root_path: str
    provenance: ProvenanceEnvelope
    item_id: str
    sha: str = Field(pattern=r"^[0-9a-f]{40}$", description="Full 40-char commit SHA.")
    reachable_on_default_branch: bool | None

    reverts_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
        description=(
            "Non-null only on a revert row: the full 40-char sha of the commit this row's "
            "own `sha` verifiably reverts (git's own auto-generated body linkage). Null on "
            "a close row. See DR-318 §D4/D8."
        ),
    )

    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file. Version-neutral
    optional — absent on all existing records. Spec: producer-contract § 3.3.
    """
