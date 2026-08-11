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
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core import tracker_store
from coordinator_core.tracker_entities import RESERVED_PROJECT_ID

_MEMBERSHIP_EVENT_KINDS = frozenset({"item_project_added", "item_project_retracted"})
_PERSON_EVENT_KINDS = frozenset({"item_person_added", "item_person_retracted"})


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
    """
    events = tracker_store.read_events(repo_root=repo_root)

    membership: dict[str, set[tuple[str | None, str]]] = {}
    for event in events:
        if event.get("kind") not in _PERSON_EVENT_KINDS:
            continue
        item_id = event.get("item_id")
        role = event.get("role")
        if not item_id or not role:
            continue
        person_id = event.get("person_id")
        edges = membership.setdefault(item_id, set())
        key = (person_id, role)
        if event.get("kind") == "item_person_added":
            edges.add(key)
        else:
            edges.discard(key)

    return membership
