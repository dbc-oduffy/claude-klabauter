"""
coordinator_core.ops.handoff_children — JSON-RPC "handoff.has_live_children" operation.

Purpose: Port of: handoff-has-live-children.sh (coordinator-claude 50ec0809, 2026-07-19) into the
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
`_collect_all_handoffs_for_gate_index` (which lives in `handoff_reconcile.py`)
from inside archival.py would be an import cycle. Separately, archival.py's
negative-spec states "Does NOT scan the filesystem directly — callers supply
the dag_index"; `_collect_all_handoffs_for_gate_index` scans the filesystem.
Placing this resolver in archival.py would either break the import graph or
silently vacate that negative-spec — it lives here instead, sibling to
`_handoff_has_live_children`.

Composes, does not reinvent: `_collect_all_handoffs_for_gate_index`
(ops/handoff_reconcile.py, the shared live+archive/handoffs+archive/completed
walker) for the corpus, and `_is_terminal_or_archived_child`
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
top level.  `_EAGER_OP_MODULES` (`coordinator_core/ops/__init__.py`) imports
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
gate_index`'s own existing call sites catch this (see `handoff_reconcile.py`'s
`_collect_open_handoffs` and its `reconcile_open` call site, both bare).
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
from typing import Any, Dict, List, Optional, Set

from coordinator_core.archival import _is_terminal_or_archived_child, reverse_membership
from coordinator_core.dag import ARCHIVAL_EDGE_KINDS, CONTINUATION_EDGE_KINDS, _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.liveness import resolve_live_session_ids
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default edge-kinds set — mirrors EDGE_KINDS_CSV in the bash veneer (:44).
# Derived from dag.ARCHIVAL_EDGE_KINDS (the SSOT) rather than restated —
# see coordinator_core/tests/test_dag_edge_kind_ssot.py for the drift guard.
# ---------------------------------------------------------------------------
_DEFAULT_EDGE_KINDS: Set[str] = set(ARCHIVAL_EDGE_KINDS)

#: Conclusion-shaped counterpart to `_DEFAULT_EDGE_KINDS`, for a caller asking
#: "may THIS workstream conclude?" rather than "is it safe to archive THIS
#: node?". `_DEFAULT_EDGE_KINDS` (all three edge kinds) is correct for the
#: ARCHIVAL question; it is WRONG for the CONCLUSION question — see
#: `dag.ARCHIVAL_EDGE_KINDS` / `dag.CONTINUATION_EDGE_KINDS` for the full
#: rationale (example-cockpit-repo-em, 2026-08-05, cross-repo/inbox/2026-08-05-
#: example-cockpit-repo-em-wsc-leg-b-counts-spinoffs-as-live-children.md).
#:
#: `_DEFAULT_EDGE_KINDS` itself stays UNWIDENED — archival callers depend on
#: it and `TestBlockedByDependentsPinnedFunctionUnchanged.
#: test_default_edge_kinds_unwidened` pins it. Every conclusion-shaped caller
#: must pass THIS constant explicitly via the `edge_kinds` param.
#:
#: A wire-shaped CSV string, deterministically ordered (sorted — set iteration
#: order must never leak into a wire value) and derived from
#: `dag.CONTINUATION_EDGE_KINDS`, the SSOT. `coordinator_core/workstream_
#: complete/__init__.py` keeps its OWN local duplicate of this exact CSV
#: string (`_LEG_B_EDGE_KINDS`) ON PURPOSE — see that module's own comment for
#: why (a measured, not assumed, cold-invocation import-cost decision), and
#: `coordinator_core/tests/test_dag_edge_kind_ssot.py` for the drift guard
#: that keeps it, this constant, and `coverage._CONTINUATION_EDGE_KINDS` in
#: sync.
CONCLUSION_EDGE_KINDS = ",".join(sorted(CONTINUATION_EDGE_KINDS))


# ---------------------------------------------------------------------------
# Filesystem scanner — replaces query-records.js double-spawn (:150-158)
# ---------------------------------------------------------------------------


def _collect_handoff_paths(worktree_root: Path) -> "tuple[List[str], List[str]]":
    """Return (paths, scan_errors) for all handoff files (live + archived) in repo.

    Scans two subtrees to mirror the bash script's query-records.js double-spawn:
      - state/handoffs/*.md          → --type handoff
      - archive/handoffs/**/*.md     → --type handoff-archived

    Args:
        worktree_root: The main worktree root (NOT the git common dir / .git dir).
                       Callers must derive this via main_worktree_root(common_dir)
                       before calling — do NOT pass a raw common_dir / .git path here.

    Returns:
        paths        — list of absolute path strings successfully enumerated.
        scan_errors  — human-readable strings, one per subtree that could not be
                       scanned (empty when both subtrees scanned cleanly).

    NOTE: uses iterdir() / os.walk(onerror=...), NOT glob()/rglob("*.md") —
    Path.glob()'s selector silently swallows PermissionError while walking
    (verified: unreadable dir → glob() yields an empty iterator, no exception),
    which made the previous `except OSError: pass` here dead code for the exact
    permission-denied case it existed to guard (mirrors roadmap_dag.py's
    `_collect_stub_paths` fix). This function is the enumeration behind this
    op's archive/close safety gate: a live child sitting under an unreadable
    subtree must never be silently indistinguishable from "candidate has no
    children" — `_handoff_has_live_children` below now fails closed
    (exit_code=2) rather than risk a false "safe to archive" verdict on
    incomplete data.
    """
    paths: List[str] = []
    scan_errors: List[str] = []

    # Live handoffs: state/handoffs/*.md
    state_dir = worktree_root / "state" / "handoffs"
    if state_dir.is_dir():
        try:
            entries = list(state_dir.iterdir())
        except OSError as exc:
            _LOG.warning(
                "handoff.has_live_children: cannot scan live handoff dir %s — %s; "
                "cannot rule out a live child under it",
                state_dir, exc,
            )
            scan_errors.append(f"{state_dir}: {exc}")
        else:
            for p in entries:
                if p.suffix == ".md" and p.is_file():
                    paths.append(str(p.resolve()))

    # Archived handoffs: archive/handoffs/**/*.md (includes month-foldered subdirs)
    archive_dir = worktree_root / "archive" / "handoffs"
    if archive_dir.is_dir():
        walk_errors: List[OSError] = []
        for dirpath, _dirnames, filenames in os.walk(archive_dir, onerror=walk_errors.append):
            for fn in filenames:
                if fn.endswith(".md"):
                    p = Path(dirpath) / fn
                    if p.is_file():
                        paths.append(str(p.resolve()))
        for exc in walk_errors:
            _LOG.warning(
                "handoff.has_live_children: cannot scan archived handoff subtree %s — "
                "%s; cannot rule out a live child under it",
                getattr(exc, "filename", archive_dir), exc,
            )
            scan_errors.append(f"{getattr(exc, 'filename', archive_dir)}: {exc}")

    return paths, scan_errors


# ---------------------------------------------------------------------------
# Edge-kinds normaliser
# ---------------------------------------------------------------------------


def _parse_edge_kinds(raw: object) -> Optional[Set[str]]:
    """Normalise the edge_kinds param to a set of strings, or None for default.

    Accepts:
      None / absent → None (reverse_membership uses its own default = all three)
      str (CSV)     → split on comma, strip, build set
      list / tuple  → convert to set of strings
      set           → used as-is
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = {k.strip() for k in raw.split(",") if k.strip()}
        return parts if parts else None
    if isinstance(raw, (list, tuple)):
        parts = {str(k).strip() for k in raw if k}
        return parts if parts else None
    if isinstance(raw, set):
        # Review: code-reviewer — set branch did not filter falsy elements (e.g. None → "None"),
        # unlike the list/tuple branch which filters via `if k`. Align both branches.
        return {str(k) for k in raw if k} or None
    return None


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("handoff.has_live_children")
async def _handoff_has_live_children(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.has_live_children" handler.

    Port of: handoff-has-live-children.sh (coordinator-claude 50ec0809, 2026-07-19) into the
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
    # asyncio deferred to first use here (not module scope) — this is the only function
    # in the module touching the asyncio namespace at runtime; a module-scope
    # `import asyncio` dragged asyncio.base_events (~7ms) into every eager op import
    # even for callers that never invoke handoff.has_live_children. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    # ------------------------------------------------------------------
    # 1. Parse + validate params
    # ------------------------------------------------------------------
    candidate: str = params.get("candidate") or ""
    exclude: List[str] = params.get("exclude") or []
    edge_kinds: Optional[Set[str]] = _parse_edge_kinds(params.get("edge_kinds"))

    if not candidate:
        return _indeterminate("missing required param: candidate")

    # ------------------------------------------------------------------
    # 2. Build live set — replaces query-records.js double-spawn (:150-158)
    # ------------------------------------------------------------------
    # C1b-ii: repo_root is the router-supplied git common dir.
    #
    # Review: code-reviewer (F1) — repo_root is the git common dir (<worktree>/.git), so
    # main_worktree_root() (= common_dir.parent) correctly derives the worktree root.
    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)  # router common_dir → worktree root
    else:
        return _indeterminate(
            "no repo_root resolved — _origin_worktree missing from request"
        )

    # Containment (Review: op-family path-containment sweep, 2026-07-08): candidate
    # MUST resolve under state/handoffs/ or archive/handoffs/ — this op's live-set
    # spans both (see docs/problems/2026-07-08-op-family-path-containment-investigation.md
    # § 4). `exclude` is never resolved to a read, so it is NOT guarded here.
    allowed_roots = [
        worktree_root / "state" / "handoffs",
        worktree_root / "archive" / "handoffs",
    ]
    resolved_candidate = contained_path(Path(candidate), allowed_roots)
    if resolved_candidate is None:
        return _indeterminate(f"candidate escapes state/handoffs or archive/handoffs: {candidate}")
    candidate_abs = str(resolved_candidate)
    if not os.path.isfile(candidate_abs):
        return _indeterminate(f"candidate not found on disk: {candidate}")

    live_paths, scan_errors = await asyncio.to_thread(_collect_handoff_paths, worktree_root)

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # Fail-closed on an unscannable subtree — a live child could be sitting
    # under it, which would make `referenced` falsely False (safe-to-archive)
    # if we silently proceeded on whatever partial set we could read. This is
    # the same fail-closed posture as the empty-live-set guard just below,
    # applied to the "we couldn't fully look" case rather than the "we looked
    # and found nothing" case.
    if scan_errors:
        return _indeterminate(
            "enumeration incomplete — cannot rule out a live child under an "
            "unscannable subtree: " + "; ".join(scan_errors)
        )
    # --- end Tier 2 ---

    # Fail-closed on empty live set — mirrors the bash oracle's fail-closed guard
    if not live_paths:
        return _indeterminate(
            "empty live set: cannot determine children "
            "(handoff-has-live-children.sh:196-199 fail-closed guard)"
        )

    # ------------------------------------------------------------------
    # 3. Reverse-membership check via archival.reverse_membership
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 4. Liveness seam — route through canonical coordinator_core.liveness
    #    (RAW-PID-LIVENESS floor: no ps -p / kill -0 / psutil.pid_exists here).
    #    resolve_live_session_ids() returns empty frozenset on error; the result
    #    is informational metadata only — the core membership decision (referenced)
    #    matches the bash implementation which does not filter by session liveness.
    # ------------------------------------------------------------------
    # resolve_live_session_ids() is a pure in-process meta.json scan
    # (liveness.py:120-147, TTL-cached) — no subprocess shell-out. Still
    # wrapped in to_thread so the scan never stalls the asyncio event loop
    # (AC-3 Gap-3 — ipc.py:486-491); the field is informational only and
    # does not affect exit_code / referenced.
    live_sids = await asyncio.to_thread(resolve_live_session_ids)  # frozenset[str]; empty on any error

    # ------------------------------------------------------------------
    # 5. Build reply
    # ------------------------------------------------------------------
    referenced = len(children) > 0
    return {
        "referenced": referenced,
        "children": sorted(children),
        "live_session_count": len(live_sids),
        "exit_code": 0 if referenced else 1,
    }


# ---------------------------------------------------------------------------
# Error-shape helper — exit_code=2, fail-closed
# ---------------------------------------------------------------------------


def _indeterminate(error_msg: str) -> dict:
    """Return a fail-closed exit_code=2 reply dict (mirrors bash :14-17).

    `referenced` is omitted (effectively None for callers using .get()) so
    that a careless caller checking `referenced` before `exit_code` does NOT
    see False (which maps to "safe to archive" on exit_code=1) — it sees a
    missing/None field that cannot be confused with a definitive False verdict.
    The authoritative signal is always exit_code=2 (W11).
    """
    return {
        "children": [],
        "live_session_count": 0,
        "exit_code": 2,
        "error": error_msg,
    }


# ---------------------------------------------------------------------------
# blocked_by_dependents — reverse baton-dependents resolver (C1, PIN-1)
# ---------------------------------------------------------------------------


def _blocked_by_indeterminate(
    error_msg: str,
    *,
    identifiers: Optional[List[str]] = None,
    scan_errors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Fail-closed reply for `blocked_by_dependents` — always the five-key shape."""
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
    """Which LIVE handoffs list this candidate's stub id in their `blocked_by`?

    PIN-1's reverse baton-dependents resolver — see the module docstring's
    "`blocked_by_dependents`" section for the full design rationale (surface
    correction, import discipline, composed primitives).

    Args:
        candidate_path: Absolute or resolvable path of the handoff whose
                         dependents are being resolved.
        worktree_root:  The main worktree root (NOT the git common dir) —
                         same convention as `_handoff_has_live_children`.
        exclude:        Optional paths to drop from the scan set before
                         matching (mirrors `_handoff_has_live_children`'s
                         `exclude` param — e.g. a scaffolded successor's own
                         path, so it does not see itself as a dependent of
                         its predecessor whose `blocked_by` list it inherited).

    Returns a dict with exactly five keys (present on EVERY outcome):
        state:       "dependents" | "none" | "indeterminate"  (tri-state)
        dependents:  sorted list of absolute path strings; non-empty only
                     when state=="dependents".
        identifiers: sorted list of the candidate's own resolved ids
                     (stub_id / id / handoff_id) that were matched against.
        scan_errors: list of strings from `_collect_all_handoffs_for_gate_index`
                     (plus the `collect_live_handoff_paths` OSError adapter
                     below) — non-empty only when state=="indeterminate".
        error:       str or None; non-None only when state=="indeterminate".
    """
    # Function-local imports — see module docstring's IMPORT DISCIPLINE note.
    from coordinator_core.ops.handoff_reconcile import _collect_all_handoffs_for_gate_index

    candidate_abs = str(Path(candidate_path).resolve())
    exclude_abs: Set[str] = {str(Path(p).resolve()) for p in (exclude or [])}

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
        return _blocked_by_indeterminate(
            "candidate has no resolvable identifier (stub_id/id/handoff_id): "
            f"{candidate_abs}"
        )

    # Adapter: collect_live_handoff_paths (ops/fleet/_common.py), called by
    # _collect_all_handoffs_for_gate_index for the live-set half of its walk,
    # RAISES OSError on an unreadable state/handoffs/ rather than surfacing it
    # via scan_errors — none of that function's existing call sites catch it.
    # Caught here explicitly so an unreadable live subtree fails closed to
    # "indeterminate" rather than raising past this resolver.
    try:
        all_handoffs, scan_errors = _collect_all_handoffs_for_gate_index(worktree_root)
    except OSError as exc:
        return _blocked_by_indeterminate(
            "enumeration incomplete — cannot rule out a live blocked_by dependent "
            f"under an unscannable subtree: {getattr(exc, 'filename', worktree_root)}: {exc}",
            identifiers=identifiers,
            scan_errors=[f"{getattr(exc, 'filename', worktree_root)}: {exc}"],
        )

    if scan_errors:
        return _blocked_by_indeterminate(
            "enumeration incomplete — cannot rule out a live blocked_by dependent "
            "under an unscannable subtree: " + "; ".join(scan_errors),
            identifiers=identifiers,
            scan_errors=scan_errors,
        )

    dependents: Set[str] = set()
    malformed_scan_errors: List[str] = []
    for h in all_handoffs:
        h_path = h.get("_path")
        if not h_path:
            continue
        h_abs = str(Path(h_path).resolve())
        if h_abs == candidate_abs or h_abs in exclude_abs:
            continue

        blocked_by = h.get("blocked_by")
        if isinstance(blocked_by, str):
            blocked_by = [blocked_by]
        if blocked_by is not None and not isinstance(blocked_by, (list, tuple)):
            # Review: code-reviewer (P2, Finding 5) — a present-but-malformed
            # `blocked_by` (e.g. a dict/int from bad YAML) is "we could not
            # fully look", not "this handoff does not reference the
            # candidate" — silently `continue`-ing past it fails OPEN,
            # against this resolver's own stated tri-state intent. Accumulate
            # it and force state="indeterminate" below instead.
            malformed_scan_errors.append(
                f"{h_abs}: blocked_by has unexpected type "
                f"{type(blocked_by).__name__!r} (expected str/list/tuple)"
            )
            continue
        if blocked_by is None:
            continue

        matched = False
        for entry in blocked_by:
            if not isinstance(entry, str) or not entry:
                continue
            # Review: code-reviewer (nit, Finding 6) — an `id_index.get(entry)`
            # fallback here was removed as unreachable dead code: `identifiers`
            # (checked below) and `id_index` (built by `_index_by_id` over
            # `all_handoffs`) are both keyed from the exact same
            # `_read_meta`-sourced `stub_id`/`id`/`handoff_id` fields —
            # `_collect_all_handoffs_for_gate_index` populates each entry via
            # `_read_meta(path)` directly (same primitive `identifiers` is
            # built from for the candidate), so any `entry` that would resolve
            # through `id_index` back to the candidate is, by construction,
            # already a member of `identifiers` and matches on the line below.
            if entry in identifiers:
                matched = True
                break
        if not matched:
            continue

        if _is_terminal_or_archived_child(h_abs):
            continue
        dependents.add(h_abs)

    if malformed_scan_errors:
        return _blocked_by_indeterminate(
            "enumeration incomplete — cannot rule out a live blocked_by "
            "dependent: " + "; ".join(malformed_scan_errors),
            identifiers=identifiers,
            scan_errors=malformed_scan_errors,
        )

    if dependents:
        return {
            "state": "dependents",
            "dependents": sorted(dependents),
            "identifiers": identifiers,
            "scan_errors": [],
            "error": None,
        }
    return {
        "state": "none",
        "dependents": [],
        "identifiers": identifiers,
        "scan_errors": [],
        "error": None,
    }


# ---------------------------------------------------------------------------
# handoff.blocked_by_dependents — op registration wrapper (C1, PIN-1)
# ---------------------------------------------------------------------------


@register_op("handoff.blocked_by_dependents")
def _handoff_blocked_by_dependents(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.blocked_by_dependents" handler.

    Thin op-registration wrapper around `blocked_by_dependents` (PIN-1) — see
    that function's docstring for the resolver's full design rationale, and
    the module docstring's "`blocked_by_dependents`" section for why it is
    homed here rather than archival.py. Registration accepted by
    coordinator-claude-em (cross-repo/inbox/2026-08-02-coordinator-claude-em-baton-lifecycle-
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

    # C1b-ii: repo_root is the router-supplied git common dir — same
    # main_worktree_root derivation as _handoff_has_live_children above.
    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)
    else:
        return _blocked_by_indeterminate(
            "no repo_root resolved — _origin_worktree missing from request"
        )

    # Containment: candidate MUST resolve under state/handoffs/ or
    # archive/handoffs/ — same op-family path-containment posture as
    # _handoff_has_live_children (docs/problems/2026-07-08-op-family-path-
    # containment-investigation.md § 4). `exclude` is never resolved to a
    # read, so it is NOT guarded here (same as the sibling op).
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
