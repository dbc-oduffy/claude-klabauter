"""
coordinator_core.session_hierarchy.derive — pure session/workstream hierarchy transform.

Purpose: derive the session/workstream hierarchy projection from handoff
frontmatter lineage. Queries all handoffs (state/handoffs/ and
archive/handoffs/), extracts ``claimed_by`` fields (the derive-bridge: a
claimed handoff's ``claimed_by`` IS the harness session_id of the consuming
session; reads are dual-tolerant and fall back to the retired ``consumed_by``
name for not-yet-migrated frontmatter — DR-084 transitional tolerance,
restored 2026-07-23; see
coordinator_core/ops/emit/sections/handoffs.py module docstring for the exit
condition), and builds a list of session-hierarchy records keyed on
``session_id``. Resolves ``parent_session_id`` via a one-hop predecessor
walk. Emits synthetic workstream-type nodes grouping sessions by workstream
slug.

Byte-parity port: this is a field-for-field transliteration of the jq program
embedded in derive-session-hierarchy.sh (example-doctrine-repo f0aa2d56, 2026-07-16) — the jq
program WAS the business logic (hitlist class 4). Every branch below cites the
mirrored jq clause so a future diff against the retired bash stays checkable.

Coverage honesty (mirrors the bash header comment): the derive-bridge is
``claimed_by`` (nee ``consumed_by``), present on only the claimed-pickup
minority of handoffs.
Unconsumed/handoff-less sessions have no lineage-to-session_id bridge and are
NOT emitted here — completeness-honesty is a documented invariant of this
projection, not a gap to "fix" in this module.

Negative-spec:
- Does NOT re-implement handoff enumeration or the atomic write — the
  query-records equivalent + atomic write still live in
  ``coordinator_core.ops.session_hierarchy_derive``. The claim-bridge read
  (``_claimed_by``) IS a read: it routes through
  ``coordinator_core.claim_state.resolve_claim_state`` (ledger-first, DR-084
  dual-tolerant mirror fallback) rather than growing a second ledger reader
  here — see this plan's C6b chunk
  (docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md). The
  already-queried in-memory ``frontmatter`` dict remains the final fallback
  when the ledger-first read itself fails (unreadable path, no repo_root),
  so a record never regresses below what the pre-migration pure read saw.
- Does NOT sort session records or workstream nodes independently — emission
  order is session records first, then workstream nodes appended (jq's
  ``$session_records + $workstream_nodes``), preserved exactly since C3/C4
  consumers may assume it.
- Does NOT deduplicate workstream nodes by insertion order — jq's ``unique``
  SORTS the input array before dedup (generic jq value ordering, which for a
  homogeneous string array is lexicographic), so the workstream slug set here
  is ``sorted(set(...))``, not a dict-preserving-first-seen-order dedup.
- DOES coalesce session records by ``session_id`` (post-port addition, no jq
  ancestor) — a session that consumed handoffs from multiple workstreams
  emits exactly one record, not one per consumed handoff. See the coalescing
  pass in ``derive()`` for merge semantics.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g3
Port of: derive-session-hierarchy.sh (example-doctrine-repo f0aa2d56, 2026-07-16)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.claim_state import resolve_claim_state

# jq: test("blitz|mise-en-place|bug-blitz"; "i") — case-insensitive substring match.
_BLITZ_RE = re.compile(r"blitz|mise-en-place|bug-blitz", re.IGNORECASE)

_BASE_SYSTEM = {
    "provenance_completeness": "complete",
    "capture_source": "derived_handoff_lineage",
    "completeness": "complete",
}


def _basename(path: str) -> str:
    """jq: ``.path | split("/") | last`` — last '/'-separated path component."""
    return path.rsplit("/", 1)[-1]


def _claimed_by(
    path: str,
    fm: Dict[str, Any],
    *,
    repo_root: Optional[Path] = None,
) -> Optional[Any]:
    """Ledger-first read of the claim-bridge field, routed through the one
    shared accessor (``coordinator_core.claim_state.resolve_claim_state``) —
    a dead/desynced ledger holder degrades to the tracked-frontmatter mirror,
    never silently drops the bridge (this plan's own 2026-08-07 branch-switch
    desync incident: a live ledger claim with a reverted mirror used to make
    a fully-worked session vanish from this projection).

    ``repo_root``, when supplied, is joined onto ``path`` (a repo-relative
    string, matching ``query_records``'s record shape) to build the absolute
    path the accessor needs; omitted, ``path`` is passed through as-is and
    ``resolve_claim_state`` falls back to its own cwd-relative resolution.

    Falls back to the already-queried, DR-084 dual-tolerant in-memory ``fm``
    read (new ``claimed_by`` preferred, retired ``consumed_by`` name as
    fallback — restored 2026-07-23, see
    coordinator_core/ops/emit/sections/handoffs.py module docstring for the
    exit condition) whenever the accessor itself cannot resolve anything
    (unreadable path, no ledger, no mirror) — a record here never regresses
    below what the pre-migration pure read already had in hand.
    """
    mirror_fallback = fm.get("claimed_by")
    if mirror_fallback is None:
        mirror_fallback = fm.get("consumed_by")

    abs_path = (Path(repo_root) / path) if repo_root is not None else Path(path)
    try:
        state = resolve_claim_state(abs_path, repo_root=repo_root)
    except Exception:
        return mirror_fallback

    return state.holder if state.holder is not None else mirror_fallback


def _jq_truthy(value: Any) -> bool:
    """jq truthiness: only ``null``/``false`` are falsy; everything else (including
    ``0``/``""``/``[]``) is truthy. Python's bare ``if x`` diverges on falsy-but-
    jq-truthy values (empty string, zero), so this predicate is used everywhere a
    ported jq ``if COND then`` guard is evaluated, per the branch-level docstring
    on each caller.
    """
    return value is not None and value is not False


def _system_block(created_by_session: str) -> Dict[str, Any]:
    """Build the ``system`` sub-object, conditionally merging ``created_by_session``.

    Mirrors the jq idiom of re-emitting the WHOLE ``system`` dict (not patching
    one key) when ``$session`` (``CREATED_BY_SESSION``) is non-empty — a plain
    dict literal in Python produces the identical JSON shape either way, so this
    is a non-issue here, but the re-merge shape is preserved for auditability
    against the jq source.
    """
    if len(created_by_session) > 0:
        return {**_BASE_SYSTEM, "created_by_session": created_by_session}
    return dict(_BASE_SYSTEM)


def derive(
    handoffs_active: List[Dict[str, Any]],
    handoffs_archived: List[Dict[str, Any]],
    created_by_session: str = "",
    *,
    repo_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Derive the session-hierarchy record list from queried handoff records.

    Args:
        handoffs_active: ``[{"path": str, "frontmatter": dict}, ...]`` from
            ``--type handoff``.
        handoffs_archived: same shape, from ``--type handoff-archived``.
        created_by_session: ``CS_SESSION_ID`` env value, or ``""`` if unset.
            Non-empty triggers the conditional ``system.created_by_session``
            merge on every emitted record (jq: ``if ($session | length) > 0``).
        repo_root: optional, forwarded to ``_claimed_by``'s ledger-first
            accessor to resolve each record's repo-relative ``path`` to an
            absolute one. The current caller
            (``coordinator_core.ops.session_hierarchy_derive``) does not pass
            this yet; omitted, the accessor falls back to its own
            cwd-relative resolution, with the in-memory frontmatter mirror as
            the final fallback (see ``_claimed_by`` docstring).

    shell-doc-ok: quotes the jq oracle's own filter expression, where `$session`
    is a jq variable rather than a shell expansion.

    Returns:
        session records first, then synthetic workstream-type nodes appended
        (emission order is part of the contract — see module negative-spec).
    """
    all_handoffs = list(handoffs_active) + list(handoffs_archived)

    # jq: build predecessor-basename -> claimed_by lookup table, indexed by the
    # last path component (predecessor fields store a bare filename). Dual-tolerant:
    # prefers new claimed_by, falls back to old consumed_by for not-yet-migrated frontmatter.
    pred_lookup: Dict[str, Optional[str]] = {}
    for rec in all_handoffs:
        fm = rec.get("frontmatter") or {}
        pred_lookup[_basename(rec["path"])] = _claimed_by(rec["path"], fm, repo_root=repo_root)

    # jq: $all | [.[] | select(.frontmatter.consumed_by != null)] — ported as
    # claimed_by-preferred, consumed_by-fallback per the dual-tolerant read rule.
    consumed = [
        rec
        for rec in all_handoffs
        if _claimed_by(rec["path"], rec.get("frontmatter") or {}, repo_root=repo_root) is not None
    ]

    session_records: List[Dict[str, Any]] = []
    for rec in consumed:
        path = rec["path"]
        fm = rec.get("frontmatter") or {}

        workstream_raw = fm.get("workstream") or ""
        session_type = "blitz" if _BLITZ_RE.search(workstream_raw) else "session"

        # jq: ($fm.predecessor // "none") as $pred | if pred in (none-ish) then null
        # else ($pred_lookup[$pred] // null) end. `//` substitutes "none" only when
        # predecessor is null/false (jq-falsy) — an explicit "" survives `//` and is
        # caught by the null/"" check on the next line instead.
        pred_raw = fm.get("predecessor")
        pred = pred_raw if _jq_truthy(pred_raw) else "none"
        parent_session_id: Optional[str]
        if pred in ("none", None, ""):
            parent_session_id = None
        else:
            parent_session_id = pred_lookup.get(pred) or None

        record: Dict[str, Any] = {
            "session_id": _claimed_by(path, fm, repo_root=repo_root),
            "session_type": session_type,
            "workstream": fm.get("workstream") or "unknown",
            "parent_session_id": parent_session_id,
            "linked_handoffs": [path],
        }
        # jq: + if $fm.branch then {branch: $fm.branch} else {} end — conditionally
        # add branch (omit key entirely when absent, not `branch: null`).
        if _jq_truthy(fm.get("branch")):
            record["branch"] = fm["branch"]
        # jq: + if ($session | length) > 0 then {system: {...+created_by_session}} else {} end
        record["system"] = _system_block(created_by_session)
        session_records.append(record)

    # No jq ancestor: the ported jq program built ONE record per consumed
    # handoff, keyed on consumed_by, with no dedup pass — a session that
    # consumed handoffs from two workstreams produced two records sharing a
    # session_id. Downstream consumers (example-cockpit-repo, example-retrieval-repo) ingest
    # this emission and independently pick a winner (last-seen vs
    # first-seen) on duplicate-key rows, so their work-state stores diverge
    # on exactly those rows. This pass coalesces to one record per distinct
    # session_id so the emission itself is no longer ambiguous.
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in session_records:
        groups.setdefault(rec["session_id"], []).append(rec)

    merged_records: List[Dict[str, Any]] = []
    seen_sids: set[str] = set()
    for rec in session_records:
        sid = rec["session_id"]
        if sid in seen_sids:
            continue
        seen_sids.add(sid)
        group = groups[sid]

        winner = max(group, key=lambda r: _basename(r["linked_handoffs"][0]))

        parent_session_id = winner["parent_session_id"]
        if parent_session_id is None:
            for candidate in group:
                if candidate["parent_session_id"] is not None:
                    parent_session_id = candidate["parent_session_id"]
                    break

        linked_handoffs = sorted({r["linked_handoffs"][0] for r in group})

        merged: Dict[str, Any] = {
            "session_id": sid,
            "session_type": winner["session_type"],
            "workstream": winner["workstream"],
            "parent_session_id": parent_session_id,
            "linked_handoffs": linked_handoffs,
        }
        if "branch" in winner:
            merged["branch"] = winner["branch"]
        merged["system"] = winner["system"]
        merged_records.append(merged)

    session_records = merged_records

    # jq: ([$consumed[] | .frontmatter.workstream // "unknown"] | unique) — unique SORTS.
    workstreams = sorted({(rec.get("frontmatter") or {}).get("workstream") or "unknown" for rec in consumed})

    workstream_nodes: List[Dict[str, Any]] = []
    for ws in workstreams:
        node: Dict[str, Any] = {
            "session_id": "workstream:" + ws,
            "session_type": "workstream",
            "workstream": ws,
            "parent_session_id": None,
            "linked_handoffs": [],
            "system": _system_block(created_by_session),
        }
        workstream_nodes.append(node)

    return session_records + workstream_nodes
