"""
coordinator_core.ops.handoff_children — JSON-RPC "handoff.has_live_children" operation.

Purpose: Port of: handoff-has-live-children.sh (DoE 50ec0809, 2026-07-19) into the
coordinator_core resident service.  Replaces the query-records.js double-spawn with
an in-memory frontmatter-index walk over the handoff DAG, and surfaces the exit-code
contract (0/1/2) as reply fields for the thin bash veneer (C7) to map back to shell
exit codes.

Exit-code contract (mirrored from the bash source):
    0  — has live children (referenced=true): do NOT archive the candidate.
    1  — safe to archive (referenced=false): no live handoff names the candidate.
    2  — internal error / indeterminate (fail-closed): veneer MUST treat as do-not-archive.

Live-set semantics: "live" means present in the combined handoff index (state/handoffs/
+ archive/handoffs/).  This mirrors the bash script's collection of both --type handoff
and --type handoff-archived records from query-records.js.  No per-handoff
session-liveness filtering is applied in the core membership check (faithful to the
bash implementation; the liveness seam is imported and called to verify the canonical
path is reachable and to surface live-session count as metadata).

Self-registration: importing this module calls register_op("handoff.has_live_children",
_handoff_has_live_children) and register_op("handoff.blocked_by_dependents",
_handoff_blocked_by_dependents) as a side-effect.  Add this module to
coordinator_core/ops/__init__.py to trigger registration at start_server() time.

RAW-PID-LIVENESS floor (enforced here, see coordinator-tripwires.md § RAW-PID-LIVENESS):
this module MUST NOT call ps -p, kill -0, or psutil.pid_exists on any stored PID.
All session-liveness queries route through coordinator_core.liveness.

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C4

Negative-spec (hard-won):
  - Does NOT commit to, or modify, coordinator substrate (handoffs, review-trail, git).
    This is a read-only query op.
  - Does NOT walk transitively — reverse_membership is single-hop (mirrors referencedBy
    in walk-handoff-dag.js).  The candidate's ancestors are NOT checked.
  - Does NOT glob state/handoffs/ with os.listdir directly — uses _collect_handoff_paths
    which mirrors the query-records.js scope (both live + archived subtrees).
  - Does NOT handle bare repos, separate-git-dir setups, or non-standard .git locations
    — inherits this constraint from `main_worktree_root`
    (`coordinator_core/ops/fleet/_common.py`).
  - Does NOT proceed on a partially-scanned live set — `_collect_handoff_paths`
    reports which of state/handoffs/ or archive/handoffs/ it could not enumerate
    (permission-denied or similar), and `_handoff_has_live_children` returns
    exit_code=2 (indeterminate/fail-closed) rather than a `referenced` verdict
    computed on incomplete data.

`blocked_by_dependents` (reverse baton-dependents resolver)
-------------------------------------------------------------------------------
Purpose: which LIVE handoffs list a candidate's own stub id in their
`blocked_by`?  No reverse index over `blocked_by` exists anywhere else in the
engine — every existing reader (`_classify_blocked_by`, `_has_asymmetry`,
`classify_gate`, `_gate_cascade_clear` in `reconcile/gate_eval.py`) reads a
handoff's OWN `blocked_by`, never who points AT it.  `reverse_membership`
above cannot see this: it walks `EDGE_KIND_META` lineage
(predecessor/additional_predecessors/forked_from) only, and `blocked_by` is
not an edge kind.  This is a NEW, sibling primitive — `reverse_membership`
stays unmodified.

Homed here (NOT `coordinator_core/archival.py`): `ops/handoff_reconcile.py`
imports `reverse_membership` FROM archival.py, so composing
`_collect_all_handoffs_for_gate_index` (which lives in
`coordinator_core/reconcile/handoff_corpus.py`, C2a-extracted out of
`handoff_reconcile.py`) from inside archival.py would still risk an import
cycle via that shared lineage. Separately, archival.py's
negative-spec states "Does NOT scan the filesystem directly — callers supply
the dag_index"; `_collect_all_handoffs_for_gate_index` scans the filesystem.
Placing this resolver in archival.py would either break the import graph or
silently vacate that negative-spec — it lives here instead, sibling to
`_handoff_has_live_children`.

Composes, does not reinvent: `_collect_all_handoffs_for_gate_index`
(coordinator_core/reconcile/handoff_corpus.py, the shared live+archive/
handoffs+archive/completed walker) for the corpus, and
`_is_terminal_or_archived_child`
(coordinator_core.archival, imported at module top level — see below) for the
"live" predicate, which carries two hard-won bug fixes (2026-07-09
positive-classification-only exclusion; 2026-07-17 a `consumed` child with
`deployment_state: in_flight` is RETAINED as live) that this resolver must
not re-derive. Id resolution is a plain `entry in identifiers` membership
check — see Finding 6 note at the match site: an `_index_by_id`-backed
fallback was removed as unreachable, since both `identifiers` and
`_collect_all_handoffs_for_gate_index`'s entries are `_read_meta`-sourced
from the exact same fields.

IMPORT DISCIPLINE: `_collect_all_handoffs_for_gate_index` is imported
FUNCTION-LOCALLY inside `blocked_by_dependents`, not at module
top level (now sourced from `coordinator_core.reconcile.handoff_corpus`,
C2a-extracted out of `handoff_reconcile.py` — see module docstring above).
`_EAGER_OP_MODULES` (`coordinator_core/ops/__init__.py`) imports
`handoff_children` at slot 120 and `handoff_reconcile` at slot 236 — NOT
wrapped in a swallow-and-continue try/except-pass (that shape was explicitly
REJECTED there, 2026-07-21): every catch prints the real module name +
exception to stderr AND records it in `_POISONED_MODULES` so dispatch-time
lookups can re-surface the true cause. A top-level import here would still
pull `handoff_reconcile`'s transitive import set into slot 120, and a
failure there still surfaces loudly at that import site — but package-init
callers (test collection, ad-hoc imports) would silently proceed past it
with `handoff.has_live_children` unregistered until something actually
dispatches it, which is the real (quieter, not silent) risk this
function-local placement insures against. The edge is acyclic today
(verified); this is cheap insurance against a future back-edge, mirroring
`handoff_archive_transition.py`'s own § Reuse precedent for `archive_stamp`.
`_is_terminal_or_archived_child` is imported at module top level alongside
`reverse_membership` — both come from `archival.py`, already a module-level
dependency of this file, so there is no cycle risk to insure against there.

Tri-state, not a bool: a non-empty `scan_errors` maps to `state=="indeterminate"`,
NEVER `"none"` — conflating "we could not fully look" with "we looked and
found nothing" is the exact failure this guard exists to prevent.  A
candidate with no resolvable identifier (no `stub_id`, `id`, or `handoff_id`)
also fails closed to `"indeterminate"`.

Adapter note: `collect_live_handoff_paths` (ops/fleet/_common.py), which
`_collect_all_handoffs_for_gate_index` calls for the live-set half of its
walk, RAISES `OSError` on an unreadable `state/handoffs/` rather than
returning it via its `scan_errors` list — none of `_collect_all_handoffs_for_
gate_index`'s own existing call sites catch this (see
`handoff_corpus._collect_open_handoffs` and `handoff_reconcile.py`'s
`reconcile_open` call site, both bare).
`blocked_by_dependents` catches this `OSError` explicitly and folds it into
`scan_errors` itself, so an unreadable live subtree fails closed to
`"indeterminate"` here rather than raising past this resolver.

Spec backlink: pln-roadmap-baton-supersession-haz-b82ac3 § C1 (PIN-1)

Negative-spec (`blocked_by_dependents`):
  - Does NOT restate the "live" predicate — composes
    `archival._is_terminal_or_archived_child` rather than re-deriving it.
  - Does NOT walk transitively — single-hop, mirrors `reverse_membership`.
  - Does NOT widen `_DEFAULT_EDGE_KINDS` or touch `_handoff_has_live_children`
    — `blocked_by` is not an edge kind and this is a wholly separate resolver.
  - Does NOT treat an unresolvable candidate identifier, or a non-empty
    `scan_errors`, as "no dependents" — both fail closed to `"indeterminate"`.
  - Does NOT mutate any coordinator substrate — read-only, same as the rest
    of this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from coordinator_core.archival import _is_terminal_or_archived_child, reverse_membership
from coordinator_core.dag import (
    ARCHIVAL_EDGE_KINDS,
    CONTINUATION_EDGE_KINDS,
    _read_meta,
    build_reverse_edge_index,
)
from coordinator_core.ipc import register_op
from coordinator_core.liveness import resolve_live_session_ids
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

_LOG = logging.getLogger(__name__)

# Default edge-kinds set — mirrors EDGE_KINDS_CSV in the bash veneer (:44).
# Derived from dag.ARCHIVAL_EDGE_KINDS (the SSOT) rather than restated —
_DEFAULT_EDGE_KINDS: Set[str] = set(ARCHIVAL_EDGE_KINDS)

#: Conclusion-shaped counterpart to `_DEFAULT_EDGE_KINDS`, for a caller asking
#: node?". `_DEFAULT_EDGE_KINDS` (all three edge kinds) is correct for the
#: ARCHIVAL question; it is WRONG for the CONCLUSION question — see
#: `dag.ARCHIVAL_EDGE_KINDS` / `dag.CONTINUATION_EDGE_KINDS` for the full
#: `_DEFAULT_EDGE_KINDS` itself stays UNWIDENED — archival callers depend on
#: `dag.CONTINUATION_EDGE_KINDS`, the SSOT. `coordinator_core/workstream_
#: string (`_LEG_B_EDGE_KINDS`) ON PURPOSE — see that module's own comment for
#: that keeps it, this constant, and `coverage._CONTINUATION_EDGE_KINDS` in
CONCLUSION_EDGE_KINDS = ",".join(sorted(CONTINUATION_EDGE_KINDS))


def _collect_handoff_paths(worktree_root: Path) -> "tuple[List[str], List[str]]":
    paths: List[str] = []
    scan_errors: List[str] = []

    state_dir = worktree_root / "state" / "handoffs"
    if state_dir.is_dir():
        try:
            with os.scandir(state_dir) as it:
                for entry in it:
                    if entry.name.endswith(".md") and entry.is_file():
                        paths.append(entry.path)
        except OSError as exc:
            _LOG.warning(
                "handoff.has_live_children: cannot scan live handoff dir %s — %s; "
                "cannot rule out a live child under it",
                state_dir, exc,
            )
            scan_errors.append(f"{state_dir}: {exc}")

    archive_dir = worktree_root / "archive" / "handoffs"
    if archive_dir.is_dir():
        stack: List[str] = [str(archive_dir)]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.name.endswith(".md") and entry.is_file():
                            paths.append(entry.path)
            except OSError as exc:
                _LOG.warning(
                    "handoff.has_live_children: cannot scan archived handoff subtree %s — "
                    "%s; cannot rule out a live child under it",
                    getattr(exc, "filename", current), exc,
                )
                scan_errors.append(f"{getattr(exc, 'filename', current)}: {exc}")

    return paths, scan_errors


def _parse_edge_kinds(raw: object) -> Optional[Set[str]]:
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = {k.strip() for k in raw.split(",") if k.strip()}
        return parts if parts else None
    if isinstance(raw, (list, tuple)):
        parts = {str(k).strip() for k in raw if k}
        return parts if parts else None
    if isinstance(raw, set):
        return {str(k) for k in raw if k} or None
    return None


def _is_archive_resident_path(p: str) -> bool:
    parts = Path(p).parts
    return any(
        parts[i] == "archive" and parts[i + 1] == "handoffs"
        for i in range(len(parts) - 1)
    )


def _allowed_candidate_roots(worktree_root: Path) -> List[Path]:
    return [
        worktree_root / "state" / "handoffs",
        worktree_root / "archive" / "handoffs",
    ]


def _resolve_candidate(candidate: str, allowed_roots: List[Path]) -> Tuple[Optional[str], Optional[str]]:
    resolved = contained_path(Path(candidate), allowed_roots)
    if resolved is None:
        return None, f"candidate escapes state/handoffs or archive/handoffs: {candidate}"
    candidate_abs = str(resolved)
    if not os.path.isfile(candidate_abs):
        return None, f"candidate not found on disk: {candidate}"
    return candidate_abs, None


async def _enumerate_live_set(worktree_root: Path) -> Tuple[List[str], Optional[str]]:
    live_paths, scan_errors = await asyncio.to_thread(_collect_handoff_paths, worktree_root)
    if scan_errors:
        return [], (
            "enumeration incomplete — cannot rule out a live child under an "
            "unscannable subtree: " + "; ".join(scan_errors)
        )
    if not live_paths:
        return [], (
            "empty live set: cannot determine children "
            "(handoff-has-live-children.sh:196-199 fail-closed guard)"
        )
    return live_paths, None


async def _build_index(
    worktree_root: Path,
    live_paths: List[str],
    metas: Optional[Dict[str, dict]] = None,
) -> Tuple[Optional[dict], Optional[str]]:
    """(reverse-edge index, None) over the NON-archived nodes of `live_paths`,
    or (None, error message) — an unbuildable index decides nothing.

    Index only the non-archived nodes. `reverse_membership` drops every
    archive-resident child afterwards via `_is_terminal_or_archived_child`
    rule 1, which is a POSITIVE, unconditional path-segment exclusion — it is
    not one of that predicate's fail-closed branches, so an archived node can
    never survive into the returned live set no matter what its frontmatter
    says or whether it can be read at all. Indexing them is therefore pure
    work for an answer that is discarded: 951 nodes indexed to decide over
    235. Measured 172ms -> 31ms, with zero verdict differences across all 235
    candidates in this corpus — corroboration, not the argument; the argument
    is that rule 1 cannot fail open. Rules 2 and 3 (terminal status /
    deployment_state) are NOT pre-filtered here: those ARE the fail-closed
    branches, and they stay where they are.

    `live_paths` itself is still what callers pass to `reverse_membership`,
    so its empty-set fail-closed guard still judges the true corpus.

    `metas`, when supplied, is a pre-read `{abspath: frontmatter}` lookup with
    per-missing-path `_read_meta` fallback inside `build_reverse_edge_index`
    itself — a path absent from `metas` is not decided in `metas`'s favour,
    it is just read normally.
    """
    index_set = [p for p in live_paths if not _is_archive_resident_path(p)]
    try:
        index = await asyncio.to_thread(
            build_reverse_edge_index,
            index_set,
            str(worktree_root / "state" / "handoffs"),
            None,
            metas,
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"unbuildable reverse-edge index: {exc}"
    return index, None


async def has_live_children_many(
    candidates: List[str],
    repo_root: Optional[Path] = None,
    *,
    edge_kinds: Optional[Set[str]] = None,
) -> Dict[str, int]:
    """``{candidate: exit_code}`` for many candidates over ONE corpus pass.

    Same question, same guards, same verdicts as ``handoff.has_live_children``
    below — this exists because that op is target-independent right up to its
    final comparison, so asking it N times pays N x M frontmatter reads to
    answer something that needs M. It enumerates the handoff corpus and reads
    every node's edges once PER CALL, and `_FRONTMATTER_CACHE` does not save
    it: that cache memoises PARSING while every `_read_meta` still re-reads
    and re-hashes the bytes, deliberately, to close the stamp-read/content-read
    TOCTOU window. The cost is the asking, so the fix is to ask once.

    Measured caller: `reap-orphaned-in-flight-handoffs.py` spent 3.1s of a
    3.1s run here — 36,638 `_read_meta` calls for 19 orphans against ~950
    handoffs — with DR-344's bar at 500ms for the whole script.

    Not a new mechanism: `dag.build_reverse_edge_index` /
    `_referenced_by_indexed` already exist for exactly this shape and are
    already in production behind `fleet.archive_terminal_handoffs` (which hit
    the same wall at 96,534 opens / 21.5s). This routes the per-orphan caller
    onto them via `reverse_membership`'s own `index=` parameter, so the
    terminal-and-archived child exclusion still runs afterwards, unchanged and
    shared by both branches — the indexed and unindexed paths cannot answer
    differently.

    Fail-closed exactly as the singular op: a corpus that cannot be fully
    enumerated, an empty live set, a candidate escaping
    state/handoffs//archive/handoffs/, a candidate absent from disk, or a
    `reverse_membership` failure all yield 2 (indeterminate) for the affected
    candidate — never 1 (safe to release). Whole-corpus failures mark EVERY
    candidate indeterminate; a single candidate's failure never contaminates
    its siblings.
    """
    if not candidates:
        return {}
    if repo_root is None:
        return {c: 2 for c in candidates}

    worktree_root = main_worktree_root(repo_root)
    allowed_roots = _allowed_candidate_roots(worktree_root)

    live_paths, corpus_error = await _enumerate_live_set(worktree_root)
    if corpus_error is not None:
        return {c: 2 for c in candidates}

    index, index_error = await _build_index(worktree_root, live_paths)
    if index_error is not None:
        return {c: 2 for c in candidates}

    codes: Dict[str, int] = {}
    for candidate in candidates:
        candidate_abs, candidate_error = _resolve_candidate(candidate, allowed_roots)
        if candidate_error is not None:
            codes[candidate] = 2
            continue
        try:
            children = reverse_membership(
                candidate_abs, live_paths, edge_kinds=edge_kinds, index=index
            )
        except Exception:  # noqa: BLE001
            codes[candidate] = 2
            continue
        codes[candidate] = 0 if len(children) > 0 else 1
    return codes


async def has_live_children_from_metas(
    candidate: str,
    repo_root: Path,
    *,
    edge_kinds: object = None,
    exclude: Optional[List[str]] = None,
    metas: Optional[Dict[str, dict]] = None,
) -> Dict[str, Any]:
    """Same question, same reply shape as `_handoff_has_live_children`, but
    building its reverse-edge index over an ALREADY-read corpus (`metas`)
    instead of re-scanning frontmatter this call's caller already read.

    C1 (leg (b) reads the corpus once): `_predicate_refusal`'s leg (b) is the
    one caller-shape that re-derives the whole-corpus answer per candidate
    inside a cascade that has ALREADY read every record once via
    `_collect_live_candidates_for_kind`. This function is that same question
    — `has_live_children_many`'s single-candidate shape — but taking the
    pre-read frontmatter as a `metas` lookup instead of re-reading it via
    `_read_meta` inside `build_reverse_edge_index`.

    `metas` is a LOOKUP with `_read_meta` fallback per missing path — that
    fallback already lives inside `build_reverse_edge_index` itself. This
    function does not add its own; a path absent from `metas` is not treated
    as decided in `metas`'s favour, it is just read normally.

    `edge_kinds` accepts the same shapes `_parse_edge_kinds` normalises for
    the JSON-RPC op (CSV string, list/tuple, set, or None for default) —
    `deliverable_cascade._predicate_refusal` passes the CSV constant
    `CONCLUSION_EDGE_KINDS` here, the same value it passes to
    `_handoff_has_live_children`'s params dict on the other branch.

    Composes `has_live_children_many`'s already-decided shape rather than
    reinventing it: same containment guard, same fail-closed empty/scan-error
    live-set guard, same `_is_archive_resident_path` index-set split (index built over
    non-archive paths only; `reverse_membership` still judges against the
    FULL live set, so archive-resident referencers are still excluded via
    `_is_terminal_or_archived_child`, never by omission from the index).

    Returns the same reply shape as `has_live_children_many`'s per-candidate
    question, per the plan's own citation (docs/plans/2026-08-30-the-terminal-
    cascade-reads-the-corpus-once.md, C1 Part A): `referenced`, `children`,
    `exit_code` on the success branch, plus `error` on the exit_code=2
    (indeterminate) branch. `children` is present on every branch (fail-closed
    still carries `children: []`, never omits it — see `_indeterminate`'s own
    note on why `referenced` alone is the field that goes missing).

    Deliberately NOT the same shape as `_handoff_has_live_children` (5 keys —
    it also carries `live_session_count`, informational metadata from the
    liveness seam this function does not call): this function's success
    branch has no `live_session_count`, and its `_indeterminate`-sourced
    fail-closed branch inherits `live_session_count: 0` from that shared
    helper (also used by `_handoff_has_live_children`) rather than omitting
    it. No caller reads the field on either branch today (Review: code-
    reviewer, Finding 1) — widening the success branch to add it would exceed
    this chunk's scope.
    """
    parsed_edge_kinds = _parse_edge_kinds(edge_kinds)

    worktree_root = main_worktree_root(repo_root)

    candidate_abs, candidate_error = _resolve_candidate(
        candidate, _allowed_candidate_roots(worktree_root)
    )
    if candidate_error is not None:
        return _indeterminate(candidate_error)

    live_paths, corpus_error = await _enumerate_live_set(worktree_root)
    if corpus_error is not None:
        return _indeterminate(corpus_error)

    index, index_error = await _build_index(worktree_root, live_paths, metas)
    if index_error is not None:
        return _indeterminate(index_error)

    try:
        children = reverse_membership(
            candidate_abs,
            live_paths,
            exclude=exclude if exclude else None,
            edge_kinds=parsed_edge_kinds,
            index=index,
        )
    except (ValueError, TypeError) as exc:
        return _indeterminate(f"reverse_membership error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _indeterminate(f"unexpected error in reverse_membership: {exc}")

    referenced = len(children) > 0
    return {
        "referenced": referenced,
        "children": sorted(children),
        "exit_code": 0 if referenced else 1,
    }


def _indeterminate(error_msg: str) -> dict:
    return {
        "children": [],
        "live_session_count": 0,
        "exit_code": 2,
        "error": error_msg,
    }


def _blocked_by_indeterminate(
    error_msg: str,
    *,
    identifiers: Optional[List[str]] = None,
    scan_errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "state": "indeterminate",
        "dependents": [],
        "identifiers": sorted(identifiers) if identifiers else [],
        "scan_errors": list(scan_errors) if scan_errors else [],
        "error": error_msg,
    }


def blocked_by_dependents(
    candidate_path: "str | Path",
    worktree_root: Path,
    exclude: Optional[List[str]] = None,
) -> Dict[str, Any]:
    key = str(candidate_path)
    return blocked_by_dependents_many([candidate_path], worktree_root, exclude)[key]


def blocked_by_dependents_many(
    candidate_paths: "Sequence[str | Path]",
    worktree_root: Path,
    exclude: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    # Function-local imports — see module docstring's IMPORT DISCIPLINE note.
    from coordinator_core.reconcile.handoff_corpus import _collect_all_handoffs_for_gate_index

    replies: Dict[str, Dict[str, Any]] = {}
    if not candidate_paths:
        return replies

    exclude_abs: Set[str] = {str(Path(p).resolve()) for p in (exclude or [])}

    pending: List[Tuple[str, str, List[str]]] = []
    for candidate_path in candidate_paths:
        key = str(candidate_path)
        candidate_abs = str(Path(candidate_path).resolve())
        candidate_meta = _read_meta(candidate_abs) or {}
        identifiers = sorted(
            {
                v
                for v in (
                    candidate_meta.get("stub_id"),
                    candidate_meta.get("id"),
                    candidate_meta.get("handoff_id"),
                )
                if isinstance(v, str) and v
            }
        )
        if not identifiers:
            replies[key] = _blocked_by_indeterminate(
                "candidate has no resolvable identifier (stub_id/id/handoff_id): "
                f"{candidate_abs}"
            )
            continue
        pending.append((key, candidate_abs, identifiers))

    if not pending:
        return replies

    try:
        all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(worktree_root)
    except OSError as exc:
        detail = f"{getattr(exc, 'filename', worktree_root)}: {exc}"
        for key, _candidate_abs, identifiers in pending:
            replies[key] = _blocked_by_indeterminate(
                "enumeration incomplete — cannot rule out a live blocked_by dependent "
                f"under an unscannable subtree: {detail}",
                identifiers=identifiers,
                scan_errors=[detail],
            )
        return replies

    if scan_errors:
        for key, _candidate_abs, identifiers in pending:
            replies[key] = _blocked_by_indeterminate(
                "enumeration incomplete — cannot rule out a live blocked_by dependent "
                "under an unscannable subtree: " + "; ".join(scan_errors),
                identifiers=identifiers,
                scan_errors=scan_errors,
            )
        return replies

    holders_by_id: Dict[str, Set[str]] = {}
    malformed: List[Tuple[str, str]] = []
    for h in all_handoffs:
        h_path = h.get("_path")
        if not h_path:
            continue
        h_abs = str(Path(h_path).resolve())

        blocked_by = h.get("blocked_by")
        if isinstance(blocked_by, str):
            blocked_by = [blocked_by]
        if blocked_by is not None and not isinstance(blocked_by, (list, tuple)):
            malformed.append(
                (
                    h_abs,
                    f"{h_abs}: blocked_by has unexpected type "
                    f"{type(blocked_by).__name__!r} (expected str/list/tuple)",
                )
            )
            continue
        if blocked_by is None:
            continue

        for entry in blocked_by:
            if not isinstance(entry, str) or not entry:
                continue
            holders_by_id.setdefault(entry, set()).add(h_abs)

    terminal_cache: Dict[str, bool] = {}

    def _terminal(h_abs: str) -> bool:
        cached = terminal_cache.get(h_abs)
        if cached is None:
            cached = _is_terminal_or_archived_child(h_abs)
            terminal_cache[h_abs] = cached
        return cached

    for key, candidate_abs, identifiers in pending:
        visible_malformed = [
            msg
            for h_abs, msg in malformed
            if h_abs != candidate_abs and h_abs not in exclude_abs
        ]
        if visible_malformed:
            replies[key] = _blocked_by_indeterminate(
                "enumeration incomplete — cannot rule out a live blocked_by "
                "dependent: " + "; ".join(visible_malformed),
                identifiers=identifiers,
                scan_errors=visible_malformed,
            )
            continue

        dependents: Set[str] = set()
        for identifier in identifiers:
            for h_abs in holders_by_id.get(identifier, ()):
                if h_abs == candidate_abs or h_abs in exclude_abs:
                    continue
                if h_abs in dependents:
                    continue
                if _terminal(h_abs):
                    continue
                dependents.add(h_abs)

        if dependents:
            replies[key] = {
                "state": "dependents",
                "dependents": sorted(dependents),
                "identifiers": identifiers,
                "scan_errors": [],
                "error": None,
            }
        else:
            replies[key] = {
                "state": "none",
                "dependents": [],
                "identifiers": identifiers,
                "scan_errors": [],
                "error": None,
            }
    return replies


@register_op("handoff.blocked_by_dependents")
def _handoff_blocked_by_dependents(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.blocked_by_dependents" handler.

    Thin op-registration wrapper around `blocked_by_dependents` (PIN-1) — see
    that function's docstring for the resolver's full design rationale, and
    the module docstring's "`blocked_by_dependents`" section for why it is
    homed here rather than archival.py. Registration accepted by
    DoE-claude-em (cross-repo/inbox/2026-08-02-doe-claude-em-baton-lifecycle-
    three-asks-reply.md, Ask 3).

    Params (all optional except candidate):
        candidate (str)       — absolute or repo-relative path of the handoff
                                 whose dependents are being resolved.  Required.
        exclude   (list[str]) — optional paths to drop from the scan set
                                 before matching (mirrors `blocked_by_dependents`'s
                                 own `exclude` param).

    Returns the exact five-key dict documented on `blocked_by_dependents`:
        state:       "dependents" | "none" | "indeterminate"  (tri-state)
        dependents:  sorted list of absolute path strings.
        identifiers: sorted list of the candidate's own resolved ids.
        scan_errors: list of strings; non-empty only when state=="indeterminate".
        error:       str or None; non-None only when state=="indeterminate".

    Tri-state contract preserved at the envelope boundary: `blocked_by_dependents`
    itself already fails closed to "indeterminate" on a scan error or an
    unresolvable candidate identifier (see its own docstring/negative-spec) —
    this wrapper adds no additional collapsing. `repo_root` resolution failure
    and candidate containment failure below are the only NEW failure modes
    this wrapper introduces, and both also fail closed to "indeterminate",
    mirroring `_handoff_has_live_children`'s own containment guard.
    """
    candidate: str = params.get("candidate") or ""
    exclude: List[str] = params.get("exclude") or []

    if not candidate:
        return _blocked_by_indeterminate("missing required param: candidate")

    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)
    else:
        return _blocked_by_indeterminate(
            "no repo_root resolved — _origin_worktree missing from request"
        )

    allowed_roots = [
        worktree_root / "state" / "handoffs",
        worktree_root / "archive" / "handoffs",
    ]
    resolved_candidate = contained_path(Path(candidate), allowed_roots)
    if resolved_candidate is None:
        return _blocked_by_indeterminate(
            f"candidate escapes state/handoffs or archive/handoffs: {candidate}"
        )
    candidate_abs = str(resolved_candidate)
    if not os.path.isfile(candidate_abs):
        return _blocked_by_indeterminate(f"candidate not found on disk: {candidate}")

    return blocked_by_dependents(candidate_abs, worktree_root, exclude=exclude if exclude else None)


async def _handoff_has_live_children(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.has_live_children" handler.

    Port of: handoff-has-live-children.sh (DoE 50ec0809, 2026-07-19) into the
    coordinator_core resident service.  Accepts the same logical parameters as
    the bash --exclude / --edge-kinds flags and returns reply fields that the
    C7 veneer maps to shell exit codes 0/1/2.

    Params (all optional except candidate):
        candidate   (str)            — absolute or repo-relative path of the
                                       handoff to test.  Required.
        exclude     (list[str])      — paths to drop from the scan set before
                                       checking (mirrors --exclude repeatable flag).
        edge_kinds  (str | list)     — CSV string or list of edge-kind names to
                                       follow.  Defaults to all three kinds when
                                       absent (mirrors EDGE_KINDS_CSV :44).

    Returns a dict with keys:
        referenced         (bool)    — True → has live children (veneer → exit 0);
                                       False → safe to archive  (veneer → exit 1).
                                       ABSENT on error/indeterminate (exit_code=2) —
                                       callers MUST check exit_code before referenced.
        children           (list)    — sorted list of absolute paths that reference
                                       the candidate.
        live_session_count (int)     — count of currently-live coordinator sessions
                                       (informational; from liveness.resolve_live_session_ids).
        exit_code          (int)     — authoritative field: 0, 1, or 2.
        error              (str)     — set on internal error / indeterminate (exit_code=2).

    Exit-code contract (mirrored from bash):
        exit_code 0 → referenced=True  → do NOT archive
        exit_code 1 → referenced=False → safe to archive
        exit_code 2 → error/indeterminate → fail-closed, treat as do-not-archive
    """
    candidate: str = params.get("candidate") or ""
    exclude: List[str] = params.get("exclude") or []
    edge_kinds: Optional[Set[str]] = _parse_edge_kinds(params.get("edge_kinds"))

    if not candidate:
        return _indeterminate("missing required param: candidate")

    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)
    else:
        return _indeterminate(
            "no repo_root resolved — _origin_worktree missing from request"
        )

    candidate_abs, candidate_error = _resolve_candidate(
        candidate, _allowed_candidate_roots(worktree_root)
    )
    if candidate_error is not None:
        return _indeterminate(candidate_error)

    live_paths, corpus_error = await _enumerate_live_set(worktree_root)
    if corpus_error is not None:
        return _indeterminate(corpus_error)

    try:
        children = reverse_membership(
            candidate_abs,
            live_paths,
            exclude=exclude if exclude else None,
            edge_kinds=edge_kinds,
        )
    except (ValueError, TypeError) as exc:
        return _indeterminate(f"reverse_membership error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _indeterminate(f"unexpected error in reverse_membership: {exc}")

    #    (RAW-PID-LIVENESS floor: no ps -p / kill -0 / psutil.pid_exists here).
    live_sids = await asyncio.to_thread(resolve_live_session_ids)

    referenced = len(children) > 0
    return {
        "referenced": referenced,
        "children": sorted(children),
        "live_session_count": len(live_sids),
        "exit_code": 0 if referenced else 1,
    }
