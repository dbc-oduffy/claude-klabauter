"""
coordinator_core.tracker_projection — the item-project membership fold.

Purpose: fold `item_project_added`/`item_project_retracted` entity events
(minted by `coordinator_core.tracker_entities`) into a per-item real-edge
membership set, and materialize cockpit's pinned wire shape on top of it.
This module CONSUMES `coordinator_core.tracker_store.read_events`' output;
it never reaches into the store's internals and never adds a filter,
projection, or pagination there (DEC-12, AC12) — that growth is exactly
what `tracker_store.read_events`'s own module docstring forbids.

Spec backlink: docs/plans/2026-08-05-sat-02-sovereign-tracker-relational-
spine.md § Decision (DEC-13, DEC-18, DEC-21/22/23), § Acceptance Criteria
(AC3/AC4/AC5/AC6/AC9/AC12/AC16/AC18), § Tasks C3/C4.

C4 adds `fold_person_membership` — the `item_person` analogue of
`fold_membership`, folded over DEC-18's three-part `(item_id, person_id,
role)` natural key rather than the two-part project edge. There is no
`unassigned`-style reserved sentinel for `item_person` (DEC-13/DEC-22 are
project-membership-specific); an item with zero `item_person` edges folds
to an empty set, and that is correct here, not a totality violation —
`fold_membership`'s AC3/AC4 totality guarantee is a project-fold property
this sibling fold does not inherit or need.

The pivot this module exists to implement (DEC-13): the `unassigned`
membership edge is NEVER stored. Events record only real project edges;
`fold_membership` emits `RESERVED_PROJECT_ID` for any item whose folded
real-edge set is empty, as a read-time projection artifact only — never a
write-time one.

The property under test CHANGED at cockpit's ruling (DEC-21): mutual
exclusivity (an item cannot show both a real project and `unassigned`) is
now an invariant that CANNOT fail — DEC-13 made the violating state
unrepresentable, since no write path can ever emit an `unassigned` edge.
The property that CAN fail, and that this module's tests guard, is FOLD
TOTALITY: every item's folded membership set is non-empty. An item that
folds to `{}` vanishes silently from a project-keyed render — the same
silent-drop harm the DR's §4.5 exists to prevent, arriving by the other
door.

Negative-spec:
  - Do NOT read or write a stored `unassigned` membership edge, anywhere in
    this module. `RESERVED_PROJECT_ID` only ever appears in this module's
    OUTPUT (a fold result), never in an event fetched from or handed back
    to `tracker_store`.
  - Do NOT add a filter, projection, or pagination parameter to
    `tracker_store.read_events` to make this module's job easier (DEC-12).
    This module owns the fold; the store owns ordering only.
  - Do NOT move the fold across the wire. `fold_membership_wire` stops at
    this repo's boundary (DEC-23) — storage shape is ours, wire shape is
    cockpit's, and folding again on their side would turn one invariant
    discharged once into an invariant every consumer re-implements.
  - Do NOT assemble the `ingest_emission` envelope here (which top-level
    JSON keys the wire payload carries). That is sat-07 scope; this module
    stops at one item's materialized `projects: string[]`.
  - Do NOT test mutual exclusivity as if it were still a live invariant
    (DEC-21) — it cannot fail post-DEC-13 and a test asserting it passes
    vacuously. Test fold totality instead.
  - This module is library code. It registers NO op (sat-03 AC13) — a draft
    of the sat-03 plan proposed `@register_op("tracker.render_status")` as
    COMPUTE_ONLY, reasoning a pure read attracts no DR-208 §5 substrate-write
    carve-out; that is refuted by the shipped guard
    `coordinator_core/tests/test_tracker_store.py`'s
    `TestAffirmationEraBoundedRegistrationGuard::
    test_tracker_ops_are_classified_mutating_not_compute_only`, which asserts
    every `OP_CLASSIFICATION` key beginning `tracker.` is `OpClass.MUTATING`
    under DR-241 by construction. The read seam belongs to sat-06, which
    already names it; a future COMPUTE_ONLY registration under the
    `tracker.` prefix requires a fresh DR-241 amendment first, never an
    allowlist widening. A `tracker.*` op with no caller (sat-04/sat-06 don't
    exist yet) would be the vestigial-surface failure this roadmap's
    `beads_rust` `compaction_level` cautionary tale names — PM ruling
    2026-08-11 carries the over-breadth question itself forward to sat-06,
    not reopened here. Note for that future op: the sibling guard method
    `test_ops_tree_referencers_are_exact_match_allowlisted` scans
    `coordinator_core/ops/**/*.py` for the literal token `tracker_store` as a
    plain whole-file substring check (no AST awareness on that walk), so
    even a docstring mention of `tracker_store` in a new op module makes it
    an offender against the DR-241 allowlist.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from coordinator_core import tracker_store
from coordinator_core.tracker_entities import (
    RESERVED_PROJECT_ID,
    TrackerEntityError,
    normalize_alias,
)

_MEMBERSHIP_EVENT_KINDS = frozenset({"item_project_added", "item_project_retracted"})
_PERSON_EVENT_KINDS = frozenset({"item_person_added", "item_person_retracted"})
_PERSON_REGISTRY_EVENT_KINDS = frozenset(
    {
        "person_created",
        "person_alias_added",
        "person_alias_retracted",
        "person_merged",
    }
)


def fold_membership(*, repo_root: Path) -> dict[str, set[str]]:
    """Fold every item's real project edges from `tracker_store.read_events`.

    Reads events in `read_events`' own ratified `(applied_at, observed_at,
    id)` order (never re-derived here) and applies each
    `item_project_added`/`item_project_retracted` as a set add/discard in
    that order — the last operation on a given `(item, project)` pair wins,
    which is exactly latest-wins semantics for a two-state (present/absent)
    edge. Cites, does not copy, the deterministic-ordering prior art in
    `coordinator_core/ops/render_project_tracker.py` (fold on a pinned
    order, never wall-clock) and example-retrieval-repo's `current_claim_status`
    latest-wins pattern.

    `item_created` events seed every reachable item with an empty set —
    this is what makes AC3's totality guarantee reach an item with zero
    project mutations at all, not only items that happen to appear in an
    `item_project_*` event. The `elif kind in _MEMBERSHIP_EVENT_KINDS`
    branch independently seeds via the same `membership.setdefault(item_id,
    set())` call, so an item known to the stream ONLY via an
    `item_project_added` or `item_project_retracted` event — no
    `item_created` at all — is seeded correctly too; totality does not
    depend on `item_created` alone. Do not "simplify" this elif branch by
    dropping its `setdefault` on the theory that `item_created` already
    covers seeding — it does not, for an item with no `item_created` event,
    and AC3's tests exist to catch exactly that regression.

    Returns `RESERVED_PROJECT_ID` as the item's SOLE membership entry
    whenever its folded real-edge set is empty (DEC-13, AC4) — never `{}`,
    and never alongside a real edge (AC5). This is the one and only site in
    this module that ever writes `RESERVED_PROJECT_ID` into a result; it is
    never read from or written to the event stream itself.
    """
    events = tracker_store.read_events(repo_root=repo_root)

    membership: dict[str, set[str]] = {}
    for event in events:
        kind = event.get("kind")
        if kind == "item_created":
            item_id = event.get("item_id")
            if item_id:
                membership.setdefault(item_id, set())
        elif kind in _MEMBERSHIP_EVENT_KINDS:
            item_id = event.get("item_id")
            project_id = event.get("project_id")
            if not item_id or not project_id:
                continue
            edges = membership.setdefault(item_id, set())
            if kind == "item_project_added":
                edges.add(project_id)
            else:
                edges.discard(project_id)

    return {
        item_id: (real_edges if real_edges else {RESERVED_PROJECT_ID})
        for item_id, real_edges in membership.items()
    }


def fold_membership_wire(*, repo_root: Path) -> dict[str, list[str]]:
    """Materialize `fold_membership`'s output as cockpit's pinned wire shape
    (DEC-23, AC18): `projects: string[]`, sorted for determinism, with
    `"unassigned"` present in the array for a zero-real-edge item.

    Never emits an empty array — `fold_membership`'s own totality guarantee
    (AC3/AC4) is what makes that true here, not a separate check in this
    function. Never emits raw `item_project_added`/`item_project_retracted`
    events; this is the one function that turns our storage shape into
    their wire shape, and nothing else in this module or `tracker_entities`
    does so (DEC-23).

    Stops at ONE item's `projects: string[]`. Assembling the multi-item
    `ingest_emission` envelope (which top-level JSON keys the wire payload
    carries) is sat-07 scope, not this module's (see the plan's D1).
    """
    folded = fold_membership(repo_root=repo_root)
    return {item_id: sorted(projects) for item_id, projects in folded.items()}


def fold_person_membership(*, repo_root: Path) -> dict[str, set[tuple[str | None, str]]]:
    """Fold every item's `item_person` edges from `tracker_store.read_events`
    (C4).

    Applies each `item_person_added`/`item_person_retracted` event as a set
    add/discard over the DEC-18 three-part natural key, keyed per item on
    the `(person_id, role)` pair — the same latest-wins-by-construction
    pattern `fold_membership` uses for the two-part project edge, in
    `read_events`' own ratified order (never re-derived here).

    A person may hold several simultaneous roles on one item (AC9): both
    `(person_id, "assignee")` and `(person_id, "raised_by")` can coexist in
    one item's set, since they are distinct tuple keys.

    Unlike `fold_membership`, this fold has NO reserved-sentinel totality
    guarantee to enforce — DEC-13/DEC-22's `unassigned` row is
    project-membership-specific. An item with zero `item_person` edges
    folds to an empty set here, correctly; this function does not seed
    every `item_created` item the way `fold_membership` does.

    `person_id` is resolved through `fold_person_registry`'s merge chain
    (C3's `resolve_person`) before it enters a tuple key, so a RETIRED id
    never surfaces in this function's output (AC9, C4). The registry is
    folded exactly ONCE per call, not per edge. Resolution is READ-TIME
    only — this never writes a resolved id back into an event, mirroring
    how `RESERVED_PROJECT_ID` never touches disk in `fold_membership`. A
    `None` `person_id` is left as `None` (AC10, DEC-48): resolution is
    additive, never a tightening, and a null-person edge folds exactly as
    it does today.
    """
    events = tracker_store.read_events(repo_root=repo_root)
    registry = fold_person_registry(repo_root=repo_root, events=events)

    membership: dict[str, set[tuple[str | None, str]]] = {}
    for event in events:
        if event.get("kind") not in _PERSON_EVENT_KINDS:
            continue
        item_id = event.get("item_id")
        role = event.get("role")
        if not item_id or not role:
            continue
        person_id = event.get("person_id")
        if person_id is not None:
            person_id = resolve_person(person_id, registry=registry)
        edges = membership.setdefault(item_id, set())
        key = (person_id, role)
        if event.get("kind") == "item_person_added":
            edges.add(key)
        else:
            edges.discard(key)

    return membership


class PersonRegistry(TypedDict):
    """The shape `fold_person_registry` returns and `resolve_person`/
    `resolve_alias` consume — precise enough to give `registry["merges"]`
    and `registry["aliases"]` access sites real static-checking value, in
    place of the module's previous bare `dict` at those sites."""

    persons: dict[str, dict[str, str | None]]
    aliases: dict[tuple[str, str], str]
    merges: dict[str, tuple[str, tuple[str | None, str | None, str | None]]]


_AXES = ("manual_close", "code_complete", "qa_verified")


def _fold_axis_states(item_id: str, *, repo_root: Path) -> dict[str, str | None]:
    """Fold one item's per-axis current state in a single pass over
    `read_events` (C6, F9).

    Built ONCE per call as an in-memory dict — never persisted, never
    reused across calls (DR-215 spawn-per-call has no process to cache
    across) — and read for all three axes off this one fold rather than
    re-walking `read_events` per axis.

    Consumes `read_events`' own ratified `(applied_at, observed_at, id)`
    order without re-deriving or re-sorting it; suggest events are already
    excluded by that reader's null-`applied_at` filter. AC8's tie-break
    (identical `applied_at` resolved by `observed_at`, then by `id`) is
    therefore satisfied by consuming that order, not by writing a second,
    drifting comparator here.

    Snapshot seeding (C5's `kind == "snapshot"` event) is special-cased as
    the fold SEED rather than genesis: the axis starts from the snapshot's
    `folded_to_state`, and exactly the events whose `id` appears in the
    snapshot's `folded_event_ids` are skipped — nothing else.

    This skip is CONTENT-BOUND, never position-bound: `folded_event_ids` is
    an exact-identity set, and an event's membership in it never depends on
    where that event sorts relative to anything. A late-arriving offline
    event from another machine whose `applied_at` is earlier than the
    snapshot's `as_of_applied_at` was never folded, so it is never in the
    skip set, and folds normally on top of the seed.

    Seed ORDERING is a distinct concern from skipping, and it DOES use
    `as_of_applied_at` — necessarily so. A snapshot's own `applied_at` is
    stamped at compaction (fold) time, strictly no earlier than
    `as_of_applied_at` but with no fixed upper bound; `read_events`' global
    `(applied_at, observed_at, id)` sort therefore places the snapshot
    record itself well after any event that lands in the gap between
    `as_of_applied_at` and the snapshot's own `applied_at` — a peer
    machine's append, or simply an event appended while the fold ran. That
    event is correctly unskipped (it was never folded) and would apply
    normally — but folding it in `read_events`' raw order and THEN hitting
    the snapshot at its own later position would unconditionally overwrite
    the axis state, silently discarding it. A snapshot summarizes exactly
    the folded set as of `as_of_applied_at`, so it is faithful to treat it
    as a VIRTUAL event positioned AT `as_of_applied_at` rather than at its
    own `applied_at`: an unfolded event earlier than that boundary is
    correctly superseded by the seed (it is folded into `folded_to_state`
    itself, or — if genuinely never folded — sorts before the seed and is
    then correctly overwritten by it, matching full replay's own
    last-write-wins order); an unfolded event later than the boundary
    correctly applies on top of it. Both then equal full replay. Multiple
    snapshots for one `(item_id, axis)` (C5's re-snapshot trigger) are a
    normal case, not a corner: each is merged into the same per-axis
    ordering at its own `as_of_applied_at`, in the same single pass.
    """
    events = tracker_store.read_events(repo_root=repo_root)

    axis_events: dict[str, list[dict]] = {axis: [] for axis in _AXES}
    for event in events:
        if event.get("item_id") != item_id:
            continue
        axis = event.get("axis")
        if axis not in axis_events:
            continue
        axis_events[axis].append(event)

    def _sort_key(event: dict) -> tuple:
        if event.get("kind") == "snapshot":
            # Virtual position: the fold boundary, not the record's own
            # (later) applied_at. Tie-break components sort a snapshot
            # BEFORE a real event stamped at the same applied_at — the
            # seed represents everything already folded through that
            # instant.
            return (event.get("as_of_applied_at"), "", "", 0)
        return (
            event.get("applied_at"),
            event.get("observed_at") or "",
            event.get("id") or "",
            1,
        )

    states: dict[str, str | None] = {}
    for axis, this_axis_events in axis_events.items():
        skip_ids: set[str] = set()
        state: str | None = None
        for event in sorted(this_axis_events, key=_sort_key):
            if event.get("kind") == "snapshot":
                state = event.get("folded_to_state")
                skip_ids |= set(event.get("folded_event_ids") or ())
                continue
            if event.get("id") in skip_ids:
                continue
            to_state = event.get("to_state")
            if to_state is None:
                raise tracker_store.TrackerStoreError(
                    f"malformed transition event for item {item_id!r} axis "
                    f"{axis!r}: to_state is None (id={event.get('id')!r}) — "
                    "emit_transition's to_state is a required str; a null "
                    "to_state on a non-snapshot event can only be data "
                    "corruption or an emitter bug, never a legal cascade"
                )
            state = to_state
        states[axis] = state

    return states


def current_state(item_id: str, axis: str, *, repo_root: Path) -> str | None:
    """Return `axis`'s current `to_state` for `item_id` (C6): the last
    event for that `(item_id, axis)` pair in `read_events` order, seeded by
    a snapshot event when one is present. See `_fold_axis_states` for the
    content-bound snapshot-seeding contract. Returns `None` when the item
    has no event on `axis` and no snapshot seeds it.
    """
    return _fold_axis_states(item_id, repo_root=repo_root).get(axis)


def render_status(item_id: str, *, repo_root: Path) -> str:
    """Render `item_id`'s computed open/closed status (C6, AC6).

    Closed iff:

        manual_close.current == "closed"
        OR (code_complete.current == "asserted" AND qa_verified.current == "verified")

    Open otherwise. This is computed fresh from the fold on every call —
    never a stored lifecycle enum (F3) — via one `_fold_axis_states` pass
    covering all three axes, not three separate `current_state` folds.
    """
    states = _fold_axis_states(item_id, repo_root=repo_root)
    closed = states["manual_close"] == "closed" or (
        states["code_complete"] == "asserted" and states["qa_verified"] == "verified"
    )
    return "closed" if closed else "open"


def fold_person_registry(
    *, repo_root: Path, events: list[dict] | None = None
) -> PersonRegistry:
    """Fold the sat-05 person registry — canonical persons, the alias map,
    and the merge chain — in ONE pass over `tracker_store.read_events`
    (C3).

    Consumes `read_events`' own ratified `(applied_at, observed_at, id)`
    order without re-deriving it, exactly as `fold_membership` applies its
    add/discard pass in that order. Latest-wins by construction: a later
    `person_alias_added`/`person_alias_retracted` on the same `(namespace,
    normalized_value)` pair overwrites an earlier one, mirroring
    `tracker_entities._alias_owner`'s own fold shape.

    *events*, if given, MUST already be `read_events(repo_root=repo_root)`'s
    own full output in its own ratified order — passing it in only lets a
    caller that has already paid for one on-disk read (e.g.
    `fold_person_membership`) avoid paying for a second. This never adds a
    filter, projection, or pagination parameter to `read_events` itself
    (DEC-12) — the store still owns ordering only, and this function still
    owns the fold. When omitted, this function reads the full stream itself
    exactly as before.

    Returns a dict with exactly three keys:

    - ``"persons"``: `person_id -> {"display_name": str}`, latest-wins on
      `person_created` (a person id is minted once by `mint_person_id`, but
      this fold does not assume single-write and simply lets a later event
      for the same id win, matching every other fold in this module).
    - ``"aliases"``: `(namespace, normalized_value) -> person_id`, present
      only while currently claimed — a `person_alias_retracted` removes the
      entry rather than leaving a stale mapping.
    - ``"merges"``: `from_id -> (into_id, sort_key)`, where `sort_key` is
      the winning `person_merged` event's own `(applied_at, observed_at,
      id)` triple. This sort_key is retained (not discarded) specifically
      so `resolve_person` can identify, deterministically, which edge in a
      cross-shard merge cycle sorts LAST under `read_events`' own ratified
      order — see `resolve_person`'s docstring for why that edge is the one
      dropped.
    """
    if events is None:
        events = tracker_store.read_events(repo_root=repo_root)

    persons: dict[str, dict] = {}
    aliases: dict[tuple[str, str], str] = {}
    merges: dict[str, tuple[str, tuple]] = {}

    for event in events:
        kind = event.get("kind")
        if kind not in _PERSON_REGISTRY_EVENT_KINDS:
            continue
        if kind == "person_created":
            person_id = event.get("person_id")
            if not person_id:
                continue
            persons[person_id] = {"display_name": event.get("display_name")}
        elif kind == "person_alias_added":
            namespace = event.get("namespace")
            normalized_value = event.get("normalized_value")
            person_id = event.get("person_id")
            if not namespace or normalized_value is None or not person_id:
                continue
            aliases[(namespace, normalized_value)] = person_id
        elif kind == "person_alias_retracted":
            namespace = event.get("namespace")
            normalized_value = event.get("normalized_value")
            if not namespace or normalized_value is None:
                continue
            aliases.pop((namespace, normalized_value), None)
        elif kind == "person_merged":
            from_id = event.get("from_id")
            into_id = event.get("into_id")
            if not from_id or not into_id:
                continue
            sort_key = (
                event.get("applied_at"),
                event.get("observed_at"),
                event.get("id"),
            )
            merges[from_id] = (into_id, sort_key)

    return {"persons": persons, "aliases": aliases, "merges": merges}


def resolve_person(person_id: str, *, registry: PersonRegistry) -> str:
    """Walk DEC-42's fixed-point merge chain for *person_id* and return the
    surviving id at the end of it (C3).

    **Cross-shard cycle rule.** A merge cycle can be assembled across two
    machines, each shard carrying one leg — `person_merged`'s own write-time
    cycle guard (`tracker_entities.emit_person_merged`) is same-machine
    ergonomics ONLY (see its docstring) and does NOT make a cross-shard
    cycle unreachable once two shards are git-merged; this function must
    not assume the write side already closed that door. When the walk
    revisits a node already on its current path, the cycle it just closed
    is resolved DETERMINISTICALLY: among the edges that make up the cycle,
    the one whose `(applied_at, observed_at, id)` sort_key sorts LAST under
    `read_events`' own ratified order is dropped, and the walk stops at
    that edge's source node (its `from_id`) rather than following it.
    Every machine folding the same bytes computes the same sort order and
    therefore drops the same edge — convergence needs no cross-shard
    coordination.

    The walk keeps a visited list (`path`, checked via `in`) along its path
    as a cycle guard, but raises
    `TrackerEntityError` ONLY for a genuinely unreachable malformed chain —
    a `from_id == into_id` self-referential edge, which the cycle-drop rule
    above cannot meaningfully resolve since dropping it changes nothing.
    An ordinary cross-shard cycle never raises; it resolves via the drop
    rule above.
    """
    merges = registry["merges"]

    path: list[str] = [person_id]
    edges: list[tuple[str, tuple]] = []
    current = person_id

    while current in merges:
        into_id, sort_key = merges[current]
        if into_id == current:
            raise TrackerEntityError(
                f"person {current!r} has a self-referential person_merged "
                "edge (from_id == into_id) — malformed and unreachable, "
                "not an ordinary cross-shard cycle"
            )
        if into_id in path:
            cycle_start = path.index(into_id)
            cycle_edges = edges[cycle_start:] + [(current, sort_key)]
            drop_source, _drop_sort_key = max(cycle_edges, key=lambda e: e[1])
            return drop_source
        edges.append((current, sort_key))
        path.append(into_id)
        current = into_id

    return current


def resolve_alias(namespace: str, raw_value: str, *, registry: PersonRegistry) -> str | None:
    """Resolve an alias to its currently-surviving person id (C3).

    Normalizes *raw_value* via `tracker_entities.normalize_alias` (the same
    DEC-44 rule used at write time), looks the `(namespace,
    normalized_value)` pair up in `registry["aliases"]`, and — if claimed —
    resolves the owning person id through `resolve_person`'s merge-chain
    walk. Returns `None` for an unregistered alias; does not raise.
    """
    normalized_value = normalize_alias(namespace, raw_value)
    person_id = registry["aliases"].get((namespace, normalized_value))
    if person_id is None:
        return None
    return resolve_person(person_id, registry=registry)
