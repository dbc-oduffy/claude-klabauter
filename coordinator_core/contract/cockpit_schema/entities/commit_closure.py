"""
CommitClosure — one `(repo, item_id, sha)` fact: a commit whose `Closes:`
trailer referenced a work item, plus whether that commit is reachable on the
repo's default branch.

Represents a single closure-reference row as collected by claude-klabauter's
`coordinator_core/ops/emit/sections/commit_closures.py` porter from a `git
log` scan of `Closes:` trailers (DECISION-2/DECISION-3). Multi-commit
resolution ("is item X actually closed") is cockpit's read-side query over
these rows, not a write-time claude-klabauter concern (DECISION-4).

Spec backlinks:
  - docs/plans/2026-07-17-commit-closure-emission-fact.md § Chunk C2, AC2, AC3
  - docs/plans/2026-07-17-commit-closure-emission-fact.md § DECISION-4

Logical identity: (repo, item_id, sha). A re-close, cherry-pick, or
trailer copy-paste that lands the same item_id in two commits emits TWO
distinct rows (one per distinct triple) — no cross-commit dedup at write
time, since each row is a genuine distinct fact. `coordinator_root_path` is
an ADDITIVE connector key present on every connector-emitted entity — it is
NOT part of the logical identity (mirrors `roadmap_dag_node.py`'s identity
note).

"Default branch" in `reachable_on_default_branch` resolves to `origin/main`
verbatim — matching `_stamp_shipped_sha`/`_sha_on_origin_main` semantics
(envelope.py) — not a per-repo-configurable default-branch lookup.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from coordinator_core.contract.cockpit_schema.provenance import ContentHash, ProvenanceEnvelope


class CommitClosure(BaseModel):
    """One `(repo, item_id, sha)` commit-closure-reference fact."""

    model_config = ConfigDict(extra="forbid")

    # Connector-injected registry shortname (per-repo emission scope anchor).
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
    # Connector-injected — matches other per-repo emission entities. Additive,
    # NOT part of the (repo, item_id, sha) logical identity.
    coordinator_root_path: str
    provenance: ProvenanceEnvelope
    # Work-item identifier extracted from the commit's Closes: trailer.
    item_id: str
    sha: str = Field(pattern=r"^[0-9a-f]{40}$", description="Full 40-char commit SHA.")
    # True/false = resolved reachability on origin/main; null = indeterminate
    # (fetch-unavailable or equivalent degrade case) — never coerced to false.
    reachable_on_default_branch: bool | None

    # Review: code-reviewer (Finding 1, P1) — envelope.py's version-gated
    # _stamp_content_hash walks every SECMAP dotpath (including commit_closures,
    # already wired) and unconditionally attaches content_hash once
    # schema_version >= 2.5.0; extra="forbid" would reject the stamped record
    # without this field, matching every sibling per-repo entity.
    content_hash: ContentHash | None = None
    """
    R5 content-hash change-signal (optional; sibling of provenance). Omitted by
    claude-klabauter for records with no resolvable single source file. Version-neutral
    optional — absent on all existing records. Spec: producer-contract § 3.3.
    """
