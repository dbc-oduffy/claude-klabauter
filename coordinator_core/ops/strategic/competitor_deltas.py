"""
coordinator_core.ops.strategic.competitor_deltas — Track B: competitor_deltas derivation.

Purpose: derives the `competitors[]` field of the strategic self-description draft by diffing
a caller-supplied current `snapshot` against a `prev_snapshot` (the concrete diff/derivation
logic ships in a later chunk, C3). Absent snapshot input this is a typed "no delta" result, not
an error — Track B is snapshot-driven and has nothing to compute without one.

Both the snapshot AND the peer/competitor identity marking are PROVIDED INPUTS (PM-ratified
Q1) — this module performs no ingestion/scraping/peer-discovery; it is a pure diff of two
caller-supplied snapshots, keyed on competitor name (the durable `competitor_uid` anchor is a
post-freeze refinement, not this leg's scope).

Time axis (Item 28, wire shape per
state/cross-repo/archive/2026-09-24-example-market-data-repo-em-x-observed-window-reply-klabauter-claude-klabauter.md):
a competitor entry may carry an `observed_window` — a `{"from": ISO-8601, "to": ISO-8601}` pair
(sibling `observed_window_basis` is accepted on the wire but not read here; it does not affect
ordering). Entries WITH an `observed_window` order ascending by its `from` timestamp, ahead of
entries WITHOUT one; entries lacking an `observed_window` are treated as present-as-null and fall
back to the prior name-alphabetical ordering among themselves. This never changes the emitted
dict shape (still name/note/provenance only) — the window is a sort key, never an emitted field.

Absent either snapshot input (snapshot is None OR prev_snapshot is None), this is a typed
"no delta" result — returns [] cleanly. This is a degrade, never a crash — AC3.

Accepted snapshot shapes (defensive — either is handled):
    {"competitors": [{"name": str, ...}, ...]}      # list-of-dicts form
    {"<name>": {...}, ...}                          # name-keyed-dict form, ONLY when every
                                                     # top-level value is itself a dict (the
                                                     # competitor-object shape) — a metadata-shaped
                                                     # snapshot (e.g. {"schema_version": "1.0"})
                                                     # degrades to an empty set instead

Pinned return shape (frozen schema, generatable subset — see
DoE-claude/coordinator/schemas/strategic-self-description.schema.json):
    list[{
        "name":       str,
        "note":       str | None,
        "provenance": "generated",       # THREE-value provenance enum; generator emits ONLY this
    }]

Negative-spec: NEVER emits the "relationship" field (competitor|complement|prior-art|
superseded-by|supersedes) — that enum is human-curated only per the frozen schema; the
generator only ever contributes name/note/provenance. Never emits provenance "curated" or
"asserted". Never mutates state; this module performs no I/O.

Spec backlink: pln-claude-klabauter-generation-leg-machine--127c81 § C1 (stub) / § C3 (derivation)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def _extract_names(snapshot: dict) -> set[str]:
    return set(_extract_entries(snapshot).keys())


def _extract_entries(snapshot: dict) -> dict[str, dict]:
    """Name -> raw competitor entry (dict), preserving `observed_window` when present.

    A string-shaped competitor entry (list-of-strings form) or a name-keyed-dict entry with no
    dict body carries no `observed_window` and maps to `{}`.
    """
    if not isinstance(snapshot, dict):
        return {}

    competitors = snapshot.get("competitors")
    if isinstance(competitors, list):
        entries: dict[str, dict] = {}
        for entry in competitors:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name:
                    entries[name] = entry
            elif isinstance(entry, str) and entry:
                entries[entry] = {}
        return entries

    if competitors is None:
        if snapshot and all(isinstance(v, dict) for v in snapshot.values()):
            return {
                key: value
                for key, value in snapshot.items()
                if isinstance(key, str)
            }
        return {}

    return {}


def _observed_window_from(entry: dict) -> Optional[str]:
    """The entry's `observed_window["from"]` ISO-8601 timestamp, or None if absent/malformed.

    Present-as-null per the wire contract: a missing/non-dict `observed_window`, or a missing/
    non-string `from`, both read as "no window" rather than an error.
    """
    window = entry.get("observed_window") if isinstance(entry, dict) else None
    if not isinstance(window, dict):
        return None
    from_ts = window.get("from")
    return from_ts if isinstance(from_ts, str) and from_ts else None


def _sort_key(name: str, entry: dict) -> tuple:
    from_ts = _observed_window_from(entry)
    if from_ts is not None:
        return (0, from_ts, name)
    return (1, name)


def derive_competitor_deltas(
    repo_root: Path,
    snapshot: Optional[dict] = None,
    prev_snapshot: Optional[dict] = None,
) -> list[dict]:
    del repo_root

    if snapshot is None or prev_snapshot is None:
        return []

    current_entries = _extract_entries(snapshot)
    prev_entries = _extract_entries(prev_snapshot)

    added = sorted(
        set(current_entries) - set(prev_entries),
        key=lambda name: _sort_key(name, current_entries[name]),
    )
    removed = sorted(
        set(prev_entries) - set(current_entries),
        key=lambda name: _sort_key(name, prev_entries[name]),
    )

    deltas: list[dict] = []
    for name in added:
        deltas.append({
            "name": name,
            "note": "New competitor observed since prior snapshot.",
            "provenance": "generated",
        })
    for name in removed:
        deltas.append({
            "name": name,
            "note": "No longer present in the current snapshot (previously tracked).",
            "provenance": "generated",
        })

    return deltas
