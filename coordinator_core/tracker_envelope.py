"""
coordinator_core.tracker_envelope — the shard-to-envelope fold for cockpit's
`ingest_emission` (sat-07 C4).

Purpose: our event store is per-machine JSONL shards
(`tracker_store.EVENTS_SHARD_GLOB = "events.*.jsonl"`); cockpit's
`ingest_emission` takes ONE JSON object, not JSONL. `build_ingest_envelope`
is the fold between those two facts and nothing else (AC12) — it does not
re-derive ordering, does not re-implement the project-membership fold, and
does not assemble anything cockpit-side.

Spec backlink: docs/plans/2026-08-18-sat-07-tier-a-wiring.md § Task C4,
§ Acceptance Criteria AC12/AC13.

STATED ASSUMPTION, not a ratified contract: the top-level key names below
(`tracker_items` / `tracker_events`) are taken from sat-02's own plan,
docs/plans/2026-08-05-sat-02-sovereign-tracker-relational-spine.md:680-681
— "ingest_emission takes ONE JSON object with top-level entity-array keys
(e.g. tracker_items / tracker_events)". That "e.g." is illustrative prose,
not cockpit's pinned wire spec the way DEC-23's per-item `projects: string[]`
shape is (DR §4.5 / their `recs-05-action-item-population.md`). The EM has
an open cross-repo question out to example-cockpit-repo-em on the exact top-level
key names; this module is non-blocking in both directions on the answer —
Cockpit quarantines unknown top-level keys replayably, so an extra or
differently-named key here is a tolerable forward-compat cost, not a crash,
and this chunk does not gate on cockpit's reply. Correct the two constants
below in place once the answer lands; nothing else in this module should
need to change.

Negative-spec:
  - Do NOT read shard files directly, and do NOT re-sort or re-derive
    event order. Read exclusively through `tracker_store.read_events`,
    which already spans every shard and returns them in the ratified
    `(applied_at, observed_at, id)` order (a sibling plan, tmrg-03, owns
    that ordering contract and is in execution).
  - Do NOT re-implement `fold_membership_wire`'s totality/`"unassigned"`
    guarantee here. Per-item `projects: string[]` comes from
    `tracker_projection.fold_membership_wire` and nowhere else (AC13).
  - Do NOT construct or emit an `unassigned` membership EDGE anywhere in
    this module — `"unassigned"` only ever appears as a read-time fold
    result inherited from `fold_membership_wire`.
  - `read_events` filters to `applied_at`-populated events by design, so
    folding through it omits every suggest-tier transition event (same
    filter D6/F10 note in `tracker_store.read_events`). AC12 says "every
    shard", not "every event" — those are different claims, and whether
    suggest-tier events belong in this envelope is an open question this
    module does not resolve.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core import tracker_store
from coordinator_core.tracker_projection import fold_membership_wire

# STATED ASSUMPTION — see module docstring. Not cockpit-ratified; correct in
# place if example-cockpit-repo-em's answer to the open cross-repo question names
# different top-level keys.
TRACKER_ITEMS_KEY = "tracker_items"
TRACKER_EVENTS_KEY = "tracker_events"


def build_ingest_envelope(repo_root: Path) -> dict:
    """Fold this repo's per-machine event shards into ONE JSON-serializable
    object for cockpit's `ingest_emission` (AC12).

    Returns a dict with two top-level entity-array keys (see the module
    docstring's STATED ASSUMPTION on their exact names):
      - `tracker_items` — one materialized entry per `item_created` event,
        each carrying `fold_membership_wire`'s materialized
        `projects: string[]` (AC13) merged onto the item's own fields.
      - `tracker_events` — every `applied_at`-populated event across every
        shard, in `read_events`' own ratified order, unmodified.

    Every item in `tracker_items` carries a non-empty `projects` array —
    `"unassigned"` present for a zero-real-edge item, never an empty array
    — because that guarantee is `fold_membership_wire`'s (AC13), inherited
    here by construction rather than re-checked.
    """
    events = tracker_store.read_events(repo_root=repo_root)
    projects_by_item = fold_membership_wire(repo_root=repo_root)

    items: list[dict] = []
    for event in events:
        if event.get("kind") != "item_created":
            continue
        item_id = event.get("item_id")
        if not item_id:
            continue
        items.append(
            {
                "id": item_id,
                "title": event.get("title"),
                "body": event.get("body"),
                "created_at": event.get("created_at"),
                "projects": projects_by_item[item_id],
            }
        )

    return {
        TRACKER_ITEMS_KEY: items,
        TRACKER_EVENTS_KEY: events,
    }
