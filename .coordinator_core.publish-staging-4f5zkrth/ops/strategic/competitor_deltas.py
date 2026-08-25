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

Spec backlink: pln-makima-generation-leg-machine--127c81 § C1 (stub) / § C3 (derivation)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def _extract_names(snapshot: dict) -> set[str]:
    """Defensively extract the set of competitor names present in a snapshot dict.

    Handles both the list-of-dicts form ({"competitors": [{"name": ...}, ...]}) and the
    name-keyed-dict form ({"<name>": {...}, ...}). Malformed/unrecognized shapes degrade to
    an empty set rather than raising — this module never crashes on caller-supplied input.
    """
    if not isinstance(snapshot, dict):
        return set()

    competitors = snapshot.get("competitors")
    if isinstance(competitors, list):
        names: set[str] = set()
        for entry in competitors:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
            elif isinstance(entry, str) and entry:
                names.add(entry)
        return names

    # name-keyed-dict form: top-level keys are competitor names, but only when the snapshot
    # doesn't use the "competitors" key at all (avoid misreading a malformed list form).
    # Review: code-reviewer (F4) — additionally require every top-level value be a dict (the
    # competitor-object shape) before interpreting keys as names; a metadata-shaped snapshot
    # (e.g. {"schema_version": ..., "generated_at": ...}) would otherwise yield false-positive
    # competitor names. Degrades to an empty set (typed no-signal) when the guard fails.
    if competitors is None:
        if snapshot and all(isinstance(v, dict) for v in snapshot.values()):
            return {key for key in snapshot.keys() if isinstance(key, str)}
        return set()

    return set()


def derive_competitor_deltas(
    repo_root: Path,
    snapshot: Optional[dict] = None,
    prev_snapshot: Optional[dict] = None,
) -> list[dict]:
    """Derive competitors[] delta entries for repo_root from snapshot vs prev_snapshot.

    Pure function of the provided inputs — no I/O, no ingestion, no peer-discovery. Diffs
    `snapshot` (current) against `prev_snapshot` (previous), keyed on competitor name, and
    emits one delta note per added/removed/changed competitor. Absent either snapshot, this
    is a typed "no delta" result: returns [] cleanly (AC3 — degrade, never crash).

    Args:
        repo_root: guarded, resolved repo root (see coordinator_core.cartography._guard.path_guard).
            Unused by the diff itself (kept for signature parity with the other Track B/C
            derivation ops and for future repo-scoped snapshot resolution).
        snapshot: caller-supplied current competitor snapshot, or None.
        prev_snapshot: caller-supplied prior competitor snapshot, or None.

    Returns:
        list[dict] — see module docstring "Pinned return shape". [] if either snapshot is
        absent, or if the diff yields no changes.
    """
    del repo_root  # signature parity; not used by the pure diff

    if snapshot is None or prev_snapshot is None:
        return []

    current_names = _extract_names(snapshot)
    prev_names = _extract_names(prev_snapshot)

    added = sorted(current_names - prev_names)
    removed = sorted(prev_names - current_names)

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
