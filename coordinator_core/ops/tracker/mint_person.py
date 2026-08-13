"""
coordinator_core.ops.tracker.mint_person — tracker.mint_person op.

Purpose: sat-06's producer-facing op — the registered production path that
mints a person record through the sovereign-tracker person registry. This is
the op sat-05 named and nobody wrote; it is what makes AC1 of
`docs/plans/2026-08-12-person-identity-primitive-first-slice.md` true (a
person minted and resolved through a REGISTERED op, not a test).

This module reaches the person-registry event helpers ONLY through
`coordinator_core.tracker_entities`'s public `mint_person_id` /
`emit_person_created` / `emit_person_alias_added` functions and
`coordinator_core.tracker_projection`'s `fold_person_registry` /
`resolve_alias` — never the underlying sovereign-tracker append/read module
directly, and never a hand-built `state/sovereign-tracker/` path literal.
Both `tracker_entities.py` and `tracker_projection.py` are themselves
DR-241-affirmed referencers of that underlying module (see DR-241's
Amendment (2026-08-11) — sat-05 person-registry event handler affirmation) —
this op inherits that affirmation for the events it emits rather than
re-deriving it (see per-bound compliance table below for what THIS module
owes on its own).

WRITE BOUND (PM ruling 2026-08-12) — person records are per-repo
self-sufficient. This op does NOT call `coordinator_core.tracker_holder.
write_root_for` at all, and must not gain such a call. The `repo_root`
passed to every `emit_person_*` call below is the LOCAL repo's own worktree
root — the same repo_root this op's own caller already has, derived from the
git common dir exactly as `tracker.fold_observed_set` derives it — never a
holder or peer repo's root. Writes land ONLY in the local repo's own
sovereign-tracker event store, never cross-tree. Authorized by DR-241
Invariant 5 (confinement to "the consuming repo's own tree, and never
Claude-klabauter's tree on a different repo's behalf (DEC-11)").

DR-241 D2 five-bound COMPLIANCE (not a fresh affirmation — this op's own
handler code against bounds already ratified; DR-241 Invariant 2: "Partial
compliance is not compliance"):

  (i) Idempotent by content-derived, globally-unique event id. Every event
      this op emits (`person_created`, `person_alias_added`) is minted by
      `tracker_entities._mint_event_id` (`evt-<machine>-<digest12>`,
      already DR-241-affirmed at the 2026-08-11 amendment) — this op never
      mints an id of its own and never bypasses that minting. This op's OWN
      idempotence is lock-free compare-and-retry (see below), which does not
      touch id generation at all.
  (ii) Commutative modulo total order. `person_created`/`person_alias_added`
      carry a real, non-null `applied_at`/`observed_at` and participate in
      the ratified `(applied_at, observed_at, id)` read-time order, exactly
      as the 2026-08-11 amendment affirms for `tracker_entities.py`. This op
      adds no ordering assumption of its own — it only calls the emitters
      and, on collision, folds the registry through `fold_person_registry`
      before reading, never assuming file-append order.
  (iii) Git-reversible. This op issues only `_emit`-mediated appends (via
      `emit_person_created` / `emit_person_alias_added`) — no in-place
      mutation, no delete. A `git revert` of the append commit (or a
      pre-commit `git checkout`/`git restore` of the one shard file) removes
      exactly the events this op wrote and nothing else.
  (iv) No terminality-re-verify. This op never checks whether a prior
      person/alias record is "terminal" before writing — every write here is
      a fresh append (a creation or an alias add), never a transition out of
      an active directory with a prior state to confirm.
  (v) In-process command-type dispatch only. Registered below via
      `@register_op("tracker.mint_person")` — no UDS/HTTP surface.
  Confinement of the write target: this module never imports the underlying
  sovereign-tracker append/read module directly and never hand-builds a
  `state/`/`archive/` path literal — every write reaches the store only
  through `tracker_entities`'s own emit functions, which are the DR-241-
  affirmed referencer (see the allowlist guard in
  `coordinator_core/tests/` covering DR-241's sanctioned referencer set).
  Per-repo, not fleet-wide (DEC-11): see WRITE BOUND above — no
  `write_root_for` call, no cross-repo write, no claude-klabauter-tree default.

All five bounds plus confinement and per-repo hold — Invariant 2's "all
five, not a partial subset" is satisfied for this op's own handler code.

IDEMPOTENCE — lock-free compare-and-retry, NOT pre-check-then-write. A
resolve-then-mint pre-check has a read-to-append race that fails loud into a
half-written person (a `person_created` with some but not all aliases). This
handler instead ALWAYS attempts `emit_person_created` followed by one
`emit_person_alias_added` per resolved alias; if a concurrent session won
the race, `emit_person_alias_added` raises `TrackerEntityError` on the first
colliding alias (per-`(namespace, normalized_value)` collision guard
already affirmed in `tracker_entities.emit_person_alias_added`). On that
collision, this handler re-resolves via `tracker_projection.resolve_alias`
using the SAME alias that collided, and returns the winning person's id,
emitting nothing further. This acquires no additional lock — sat-05's
no-second-`locked_rmw` anti-scope is not violated.

ACCEPTED COST: the losing (retrying) call's own `person_created` event and
any aliases it already emitted before the collision are left behind as an
orphan — a person id with a partial or empty alias set, never referenced by
`resolve_alias`. This is the accepted cost of lock-free idempotence;
`person_merged` convergence for these orphans is explicitly out of scope
here.

ALIAS NAMESPACE MAPPING — every `person_resolver.ALIAS_BUNDLE_KEYS` member
(`"github"`, `"github_id"`, `"display"`, `"email"`) maps 1:1 onto the
identically-named `tracker_entities.ALIAS_NAMESPACES` member. `github_id`
gets a namespace of its own rather than riding under `"github"`: the two are
different KINDS of value (a renameable handle vs. a permanent numeric id),
and the namespace is the axis a consumer enumerates on. Collapsing them would
make `resolve_alias("github", "240204332")` resolve as though a numeric id
were a handle, and would leave no way to ask for the rename-proof id alone —
`(namespace, normalized_value)` distinctness prevents a collision, not a
category error.

Spec backlink: docs/plans/2026-08-12-person-identity-primitive-first-slice.md
  § Tasks C4, § Acceptance Criteria AC1.
Spec backlink: docs/decisions/DR-241-sovereign-tracker-substrate-write-carveout.md
  § D2 — Bounds of the sanction; § Amendment (2026-08-11) — the person-registry
  event handler affirmation this op's writes rely on.

Negative-spec — a future editor must NOT:
  - Call `coordinator_core.tracker_holder.write_root_for` from this module,
    or resolve `repo_root` against any repo other than the caller's own
    local worktree. Person records are per-repo self-sufficient (WRITE
    BOUND above) — there is no cross-tree write to add here.
  - Mint an anonymous person when the resolved alias bundle is empty
    (DEC-41). An empty bundle is a clean no-op, reported honestly.
  - Add a pre-check-then-write idempotence pattern (a `resolve_alias` call
    BEFORE attempting `emit_person_created`) — this reintroduces the
    read-to-append race the compare-and-retry design above exists to avoid.
  - Acquire a second lock anywhere in this op's retry path (sat-05's
    anti-scope). The retry above is a pure re-read via
    `tracker_projection.fold_person_registry`/`resolve_alias`, no lock.
  - Store a resolved `person_id` back into an emitted event — resolution is
    always a read-time projection, never persisted.
  - Import the underlying sovereign-tracker append/read module directly, or
    hand-build a `state/`/`archive/` path literal — every write must reach
    the store only through `tracker_entities`'s own emit functions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.person_resolver import resolve_operating_person
from coordinator_core.tracker_entities import (
    TrackerEntityError,
    emit_person_alias_added,
    emit_person_created,
    mint_person_id,
)
from coordinator_core.tracker_projection import fold_person_registry, resolve_alias

# Maps `person_resolver.ALIAS_BUNDLE_KEYS` bundle keys to the
# `tracker_entities.ALIAS_NAMESPACES` namespace each resolves under — 1:1 on
# every member. See "ALIAS NAMESPACE MAPPING" in the module docstring for why
# `github_id` carries a namespace of its own rather than riding under `github`.
_BUNDLE_KEY_TO_NAMESPACE: dict[str, str] = {
    "github": "github",
    "github_id": "github_id",
    "display": "display",
    "email": "email",
}

def _mint_person_core(*, bundle: dict[str, str], repo_root: Path) -> dict:
    """Pure(ish) core: mint a person from an already-resolved alias bundle.

    Returns a structured result dict:
        {"minted": bool, "reason": str, "person_id": str | None}

    - `minted: False, reason: "empty_bundle"` — *bundle* is empty; DEC-41,
      no anonymous person minted, `person_id` is None.
    - `minted: True, reason: "created"` — a new person was minted and all
      resolved aliases were attached.
    - `minted: True, reason: "collision_resolved"` — a concurrent session
      won the mint race; this call emitted nothing further and returns the
      winner's `person_id`.
    """
    if not bundle:
        return {"minted": False, "reason": "empty_bundle", "person_id": None}

    person_id = mint_person_id()
    # Review: coordinator:code-reviewer P1 — track the (namespace, value) of
    # the alias actually IN FLIGHT when a collision strikes, not a hardcoded
    # "github" retry. A collision on `github_id`/`display`/`email` AFTER this
    # call's own `github` alias already landed uncontested must resolve
    # through the alias that actually collided — resolving `github` in that
    # case finds THIS call's own orphan, not the true pre-existing winner,
    # and silently mislabels a real conflict as resolved.
    # Review: coordinator:code-reviewer P2 — scope the collision-recoverable
    # `try` to ONLY the alias-emission loop. `emit_person_created` failing is
    # not an alias collision (nothing of this call's own has succeeded yet)
    # and must never attempt an alias-based recovery.
    emit_person_created(person_id, display_name=bundle.get("display", ""), repo_root=repo_root)

    collision_namespace: Optional[str] = None
    collision_raw_value: Optional[str] = None
    try:
        for bundle_key, namespace in _BUNDLE_KEY_TO_NAMESPACE.items():
            raw_value = bundle.get(bundle_key)
            if raw_value is None:
                continue
            collision_namespace, collision_raw_value = namespace, raw_value
            emit_person_alias_added(person_id, namespace, raw_value, repo_root=repo_root)
    except TrackerEntityError:
        if collision_namespace is None:
            # No alias was ever attempted (an empty resolved bundle already
            # short-circuits above) — unreachable in practice, but re-raise
            # rather than guess at a recovery target.
            raise
        registry = fold_person_registry(repo_root=repo_root)
        winner_id = resolve_alias(
            collision_namespace, collision_raw_value, registry=registry
        )
        if winner_id is None:
            raise
        return {
            "minted": True,
            "reason": "collision_resolved",
            "person_id": winner_id,
        }

    return {"minted": True, "reason": "created", "person_id": person_id}


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("tracker.mint_person")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """tracker.mint_person — resolve the operating human and mint a person
    record through the sovereign-tracker person registry, opt-in on a
    non-empty resolved alias bundle only. See module docstring for the full
    contract.

    Wire contract:
        params: {} (no caller-supplied params; self-selecting, same shape as
                 tracker.fold_observed_set). An optional params.repo_root is
                 a D3 consistency check only (contract §3.3 doctrine) —
                 never the path source.
        ->      {"minted": bool, "reason": str, "person_id": str | None}. On
                 a D3 mismatch: {"minted": False, "reason": <mismatch
                 string>, "person_id": None} — same envelope shape as the
                 "empty_bundle" disposition, fail-closed.

    `repo_root` handler arg is the git common dir (`_OP_KEY_SCOPE:
    "common_dir"`); the worktree root is derived via
    `main_worktree_root(repo_root)` — never from `params.repo_root`.

    Raises:
        RuntimeError — repo_root is None (engine misconfiguration; production
            always supplies it via `_OP_KEY_SCOPE='common_dir'`).
    """
    if repo_root is None:
        raise RuntimeError(
            "tracker.mint_person: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )
    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return {"minted": False, "reason": mismatch, "person_id": None}

    bundle = await asyncio.to_thread(resolve_operating_person)
    return await asyncio.to_thread(_mint_person_core, bundle=bundle, repo_root=worktree)
