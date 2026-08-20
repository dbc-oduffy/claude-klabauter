"""
coordinator_core.ops.tracker.fold_ownership — tracker.fold_ownership op.

Purpose: a read op answering "who owns / is assigned this item" (AC5) — fold
one item's `item_person_added`/`item_person_retracted` events (DEC-18's
`(item_id, person_id, role)` natural key) into the currently-ADDED edges,
resolve each surviving `person_id` through the existing `person_merged`
chain, and render each resolved person's joinable `contributor_slug`
alongside the internal id. Model is `tracker.fold_observed_set` in this same
directory — a self-contained op module owning its own JSON-RPC handler
shape, not a second event-store referencer.

WRITE-TARGET CONFINEMENT (DR-241): this module deliberately reaches the
sovereign-tracker substrate ONLY through `coordinator_core.tracker_projection`
— `fold_person_membership` (the existing `item_person` fold, DEC-18
retraction/latest-wins already discharged, `person_merged` resolution
already discharged via that module's own `resolve_person`) and
`fold_person_registry` (the alias map this op inverts to find a person's
`github_id`). `coordinator_core/tests/`'s allowlist guard
(`TestAffirmationEraBoundedRegistrationGuard::
test_ops_tree_referencers_are_exact_match_allowlisted`) permits exactly two
`coordinator_core/ops/**/*.py` modules to reference the underlying
sovereign-tracker event-store module directly — `fold_observed_set.py` and
`session/boot_sweep.py` — via a plain whole-file substring scan that flags
even a docstring mention. `tracker_projection` and `tracker_entities` are
themselves the DR-241-affirmed indirection layer (both top-level
`coordinator_core/*.py` modules on the allowlist's separate top-level scan);
this op inherits their affirmation by calling them, exactly as
`tracker.render_status` does one directory over. Reusing
`fold_person_membership`'s already-tested retraction/merge-resolution fold
here also means this op does not re-walk the merge chain itself — the same
"do not re-walk the chain" discipline the dispatch brief asks for, applied
through the module that already owns that walk rather than through the
lower-level helpers the brief names by way of illustration.

Classification ruling (same DR-241 amendment `tracker.render_status`
discharges): MUTATING, not COMPUTE_ONLY, despite this op's own handler body
being genuinely read-only. The sovereign-tracker substrate test module's
`TestAffirmationEraBoundedRegistrationGuard::
test_tracker_ops_are_classified_mutating_not_compute_only` asserts every
`OP_CLASSIFICATION` key beginning `tracker.` is `OpClass.MUTATING` by
construction, and the DR-241 Amendment (2026-08-20) rules that carve-out
conservative-by-construction, not descriptive — a `COMPUTE_ONLY` exception
needs a named live claude-klabauter-internal consumer plus a fresh amendment, and this
op (a brand-new external-consumer-facing surface) has none at registration
time. See `coordinator_core/ops/tracker/render_status.py`'s own module
docstring for the full ruling text and `coordinator_core/authz/
classification.py`'s comment above this op's `OP_CLASSIFICATION` entry.

`contributor_slug` rendering delegates to `person_resolver.
_derive_contributor_slug` (C1's offline, git/network-free derivation) —
never re-implemented here.

Spec backlink: state/dispatch-briefs/2026-08-19-the-tracker-names-an-owner/C3.md
Spec backlink: docs/plans/2026-08-19-the-tracker-names-an-owner.md
  § Tasks C3, § Acceptance Criteria AC5.
Spec backlink: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
  § Amendment (2026-08-20) — the MUTATING ruling this op's registration discharges.

Negative-spec — a future editor must NOT:
  - Reclassify this op to `OpClass.COMPUTE_ONLY` without a fresh DR-241
    amendment naming a live claude-klabauter-internal consumer.
  - Widen `tracker_entities.ALIAS_NAMESPACES` to add a slug-shaped namespace
    of its own — `github_id` is the existing member this fold resolves
    through; `contributor_slug` is a DERIVED value, never a stored alias.
  - Return a sentinel person, `RESERVED_PROJECT_ID`, or an "unknown" string
    for an unowned item — the empty-owners answer IS the honest result
    (DEC-13's read-time-only discipline, extended here).
  - Omit a returned edge because its `contributor_slug` failed to resolve —
    the slug is `None` in that case; the person is still returned by
    internal `person_id`.
  - Grow this op into a general query surface over this repo's corpus
    (CLAUDE.md § boundary) — it answers ownership for ONE `item_id` only.
  - Import the underlying sovereign-tracker event-store module directly, or
    hand-build a `state/sovereign-tracker/` path literal — reach it only
    through `tracker_projection`'s existing folds, per the write-target
    confinement note above.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.person_resolver import _derive_contributor_slug
from coordinator_core.tracker_projection import fold_person_membership, fold_person_registry

_GITHUB_ID_NAMESPACE = "github_id"


def _resolve_contributor_slug(
    person_id: str, *, github_id_by_person: dict[str, str]
) -> Optional[str]:
    """Return *person_id*'s `contributor_slug`, or `None` when no
    `github_id` alias resolves to it (absent case per the dispatch brief:
    the slug is null, the person is still returned by internal id)."""
    raw_value = github_id_by_person.get(person_id)
    if raw_value is None:
        return None
    try:
        database_id = int(raw_value)
    except (TypeError, ValueError):
        return None
    return _derive_contributor_slug(database_id)


def fold_ownership(item_id: str, *, repo_root: Path) -> dict:
    """Pure, synchronous core: answer *item_id*'s currently-ADDED
    `(person_id, role)` `item_person` edges, with each surviving
    `person_id` already resolved through the `person_merged` chain by
    `tracker_projection.fold_person_membership`, and attach each resolved
    person's `contributor_slug`.

    Returns:
        {"item_id": item_id, "owners": [
            {"person_id": str | None, "role": str, "contributor_slug": str | None},
            ...
        ]}

    An item with zero currently-ADDED edges returns `"owners": []` — the
    explicit UNOWNED answer (never a sentinel person, never
    `RESERVED_PROJECT_ID`, never "unknown"). A `None` `person_id` edge
    (DEC-48) is returned as-is, with `contributor_slug: None` — it has no
    registry identity to resolve or derive a slug from.
    """
    registry = fold_person_registry(repo_root=repo_root)
    membership = fold_person_membership(repo_root=repo_root)

    github_id_by_person: dict[str, str] = {
        person_id: normalized_value
        for (namespace, normalized_value), person_id in registry["aliases"].items()
        if namespace == _GITHUB_ID_NAMESPACE
    }

    owners: list[dict] = []
    for person_id, role in membership.get(item_id, ()):
        contributor_slug = (
            _resolve_contributor_slug(person_id, github_id_by_person=github_id_by_person)
            if person_id is not None
            else None
        )
        owners.append(
            {
                "person_id": person_id,
                "role": role,
                "contributor_slug": contributor_slug,
            }
        )

    return {"item_id": item_id, "owners": owners}


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("tracker.fold_ownership")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """tracker.fold_ownership — answer who owns/is assigned one item (see
    module docstring, `fold_ownership` for the fold this delegates to).

    Wire contract:
        params: {item_id: str}. An optional params.repo_root is a D3
                 consistency check only (contract §3.3 doctrine) — never the
                 path source.
        ->      {"item_id": str, "owners": [
                     {"person_id": str | None, "role": str,
                      "contributor_slug": str | None}, ...
                 ]}. On a D3 mismatch: raises ValueError (no partial/
                 ambiguous envelope — there is no honest "skipped" answer to
                 report for a read).

    `repo_root` handler arg is the git common dir (`_OP_KEY_SCOPE:
    "common_dir"`); the worktree root is derived via
    `main_worktree_root(repo_root)` — never from `params.repo_root`.

    Raises:
        RuntimeError — repo_root is None (engine misconfiguration; production
            always supplies it via `_OP_KEY_SCOPE='common_dir'`).
        ValueError — item_id missing/malformed, or a D3 `repo_root` mismatch.
    """
    if repo_root is None:
        raise RuntimeError(
            "tracker.fold_ownership: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )
    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        raise ValueError(f"tracker.fold_ownership: {mismatch}")

    item_id = params.get("item_id")
    if not isinstance(item_id, str) or not item_id.strip():
        raise ValueError(
            f"tracker.fold_ownership: item_id must be a non-empty string, got {item_id!r}"
        )

    return await asyncio.to_thread(fold_ownership, item_id, repo_root=worktree)
