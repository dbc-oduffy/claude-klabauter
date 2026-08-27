"""
coordinator_core.reconcile.handoff_corpus — the shared live+archived handoff
corpus walker.

Extracted (C2a, pln-reconcile-open-comes-back-under-the-bar) out of
`coordinator_core.ops.handoff_reconcile` ahead of that op's C2b deletion
(DR-344 kill bar). `_collect_all_handoffs_for_gate_index` is documented,
in its own docstring below, as "the one shared walker for the live+archived
handoff corpus; nothing else should re-implement it" — six non-test modules
import from it or its supporting symbols (`ops/ownership_index.py`,
`ops/handoff_gate_aging.py`, `ops/handoff_children.py`,
`ops/handoff_transition.py` (two sites), `reconcile/ac27_differential_
oracle.py`, `session/work_state.py`), none of which are the killed op's own
decision logic (auto-ship/gate-cascade routing, D1 conservation, D2 dry-run
resolution) — this module separates the substrate those six importers
actually depend on from the op's decision logic, which stays behind (and is
retired) in `handoff_reconcile.py` itself.

Negative-spec:
  - Does NOT contain any of `handoff_reconcile.py`'s auto-ship/gate-cascade
    routing, D1 conservation-assertion, or D2 dry-run-resolution logic —
    those are the killed op's own decision logic, not shared substrate, and
    are not extracted here.
  - Does NOT re-implement the corpus walk a second time anywhere in this
    tree — every consumer of the live+archived handoff corpus routes through
    `_collect_all_handoffs_for_gate_index` (or the lower-level
    `_walk_archive_md_files` it shares with `_collect_all_handoff_paths` in
    `handoff_reconcile.py`, which stays behind since its one caller,
    C6 chain-walk liveness, is decision logic being deleted).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from coordinator_core.dag import _read_meta
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.fleet._common import collect_live_handoff_paths

_LOG = logging.getLogger(__name__)

#: deployment_state values that remove a handoff from the open set even when
#: status is still "open" (read-tolerant fallback: "active") — mirrors
#: archive_handoffs.py's terminal-set values, applied here on the open-set
#: side of the two-axis predicate (see `_is_open`'s docstring). Value lives
#: in coordinator_core.lifecycle_constants (SSOT, DR-084 C3).
_CLOSED_DEPLOYMENT_STATES = HANDOFF_TERMINAL_DEPLOYMENT

#: the single deployment_state that keeps a status in {claimed, consumed}
#: handoff OPEN — the archive-complement of archive_handoffs.py's
#: _is_terminal Branch A (status in {claimed, consumed} AND deployment_state
#: != in_flight is terminal/closed), so deployment_state==in_flight is the
#: sole non-terminal carve-out.
_CONSUMED_OPEN_DEPLOYMENT_STATE = "in_flight"

_AWAITING_GATE_STATE = "awaiting_gate"

#: D1 severed-observer gate — the two frontmatter fields that together count
#: as a "recorded disposition" for a previously-surfaced candidate. Both must
#: be non-empty strings (mirrors handoff_stamp.py's
#: _repair_archived_shipped_in_handler mandatory-`reason` precedent) — a bare
#: acknowledgement flag with no reason would recreate the exact
#: operator-remembers gap D1 exists to close. Read/write side for these two
#: fields stays in `handoff_reconcile.py` (D1 decision logic); the constants
#: are shared here because `handoff_transition._record_disposition` imports
#: them so the read/write sides cannot drift apart on spelling.
_DISPOSITION_FIELD = "reconcile_disposition"
_DISPOSITION_REASON_FIELD = "reconcile_disposition_reason"


def _is_open(meta: Dict[str, Any]) -> bool:
    """Widened open predicate — the archive-complement of _is_terminal Branch A.

    Admits two shapes:
      (status in {open, active} AND deployment_state NOT IN _CLOSED_DEPLOYMENT_STATES)
      OR (status in {claimed, consumed} AND deployment_state == "in_flight")

    Reads are dual-tolerant per DR-084 (new vocabulary preferred, old accepted as
    fallback — writers elsewhere in the fleet emit new vocabulary only).

    This is the EXACT complement of coordinator_core/ops/fleet/archive_handoffs.py's
    `_is_terminal` Branch A (`status in {claimed, consumed} AND deployment_state !=
    "in_flight"` is terminal/closed). `claimed`/`consumed` handoffs in any OTHER
    deployment_state (`awaiting_gate`, `ready_to_fire`, unset, ...) stay EXCLUDED
    here; only deployment_state==in_flight is admitted.

    # DoE lvv-04/C3 forward-compat — lockstep-update when consumed->claimed lands:
    # if DoE's lifecycle-vocab roadmap (lvv-04/C3) renames consumed->claimed or adds
    # new non-terminal deployment_state values that can co-occur with the renamed
    # status, this predicate (and archive_handoffs.py's Branch A it complements) must
    # be extended in lockstep — otherwise the two predicates silently drift apart and
    # a handoff could land in neither the open set nor the terminal set (or both).
    """
    status = (meta.get("status") or "").strip().lower()
    deployment_state = (meta.get("deployment_state") or "").strip().lower()
    if status in ("open", "active"):
        return deployment_state not in _CLOSED_DEPLOYMENT_STATES
    if status in ("claimed", "consumed"):
        return deployment_state == _CONSUMED_OPEN_DEPLOYMENT_STATE
    return False


def _collect_open_handoffs(worktree_root: Path) -> List[Dict[str, Any]]:
    """Enumerate state/handoffs/*.md and return parsed frontmatter dicts for the open set."""
    open_handoffs: List[Dict[str, Any]] = []
    for path in collect_live_handoff_paths(worktree_root):
        meta = _read_meta(str(path))
        if not meta:
            continue
        if not _is_open(meta):
            continue
        meta = dict(meta)
        meta["_path"] = str(path)
        meta.setdefault("id", meta.get("id") or path.stem)
        open_handoffs.append(meta)
    return open_handoffs


_WalkT = TypeVar("_WalkT")


def _walk_archive_md_files(
    archive_dir: Path,
    on_file: Callable[[Path], Optional[_WalkT]],
    on_scan_error: Callable[[OSError], None],
) -> "tuple[List[_WalkT], List[str]]":
    """Shared `archive/handoffs/` walker: os.walk(onerror=...) + `.md` filter +
    scan_errors bookkeeping, parameterized by a per-file callback.

    Review: code-reviewer (Finding 3) — factored out of
    `_collect_all_handoffs_for_gate_index` and `_collect_all_handoff_paths`
    (the latter stays in `handoff_reconcile.py`), which were near-duplicate
    `os.walk(archive_dir, onerror=...)` implementations differing only in
    what they do with a successfully-read entry and their warning message
    wording. Both delegate here; `on_scan_error` still lets each caller log
    its own subsystem-specific scan-gap rationale.

    NOTE: uses os.walk(onerror=...), NOT rglob("*.md") — Path.glob()'s selector
    silently swallows PermissionError while walking (verified: unreadable dir ->
    glob() yields an empty iterator, no exception), which made a bare
    `except OSError` here dead code for the exact permission-denied case it was
    meant to guard (mirrors roadmap_dag.py's `_collect_stub_paths` fix).
    """
    results: List[_WalkT] = []
    scan_errors: List[str] = []
    if not archive_dir.is_dir():
        return results, scan_errors
    walk_errors: List[OSError] = []
    for dirpath, _dirnames, filenames in os.walk(archive_dir, onerror=walk_errors.append):
        for fn in filenames:
            if not fn.endswith(".md"):
                continue
            path = Path(dirpath) / fn
            if not path.is_file():
                continue
            item = on_file(path)
            if item is not None:
                results.append(item)
    for exc in walk_errors:
        on_scan_error(exc)
        scan_errors.append(f"{getattr(exc, 'filename', archive_dir)}: {exc}")
    return results, scan_errors


def _collect_all_handoffs_for_gate_index(
    worktree_root: Path,
) -> "tuple[List[Dict[str, Any]], List[str]]":
    """Return (all_handoffs, scan_errors) — parsed frontmatter dicts, each
    carrying its own `_path` (mirroring `_collect_open_handoffs`'s convention),
    for the live+archived union. Consumed by gate_eval's `blocked_by` stub-id
    resolution (durable-id survives the archive move) and by the
    session-ownership index built over the same corpus
    (`ownership_index.build_ownership_index`, which resolves claim-store
    basenames against THIS function's output rather than re-walking the
    corpus itself — this is the one shared walker for the live+archived
    handoff corpus; nothing else should re-implement it).

    Walks `state/handoffs/` (live) + `archive/handoffs/` + `archive/completed/`
    (both archive roots via the shared `_walk_archive_md_files` os.walk
    helper — never `rglob`, which silently swallows PermissionError; see that
    function's own docstring). `scan_errors` is non-empty whenever EITHER
    archive subtree could not be fully scanned — an unreadable subtree here
    means gate_eval's `blocked_by` stub-id lookup (or the ownership index's
    basename resolution) may be missing an archived record, which must not be
    indistinguishable from "that record genuinely does not exist".

    Review: code-reviewer — Finding 6 (nit): this function is called by
    THREE independent consumers per invocation cycle (gate_eval's blocked_by
    resolution, ownership_index.build_ownership_index, and
    ac27_differential_oracle.py), each re-walking + re-copying the full
    live+archived corpus rather than sharing one materialized result within
    a single ceremony run. Not flagged as a performance P-anything (no
    evidence this is hot-path/latency-sensitive here) — worth a look for
    whoever eventually profiles `/workstream-complete`.
    """
    all_handoffs: List[Dict[str, Any]] = []
    for path in collect_live_handoff_paths(worktree_root):
        meta = _read_meta(str(path))
        if meta:
            meta = dict(meta)
            meta["_path"] = str(path)
            all_handoffs.append(meta)
    scan_errors: List[str] = []

    for archive_subdir in ("handoffs", "completed"):
        archive_dir = worktree_root / "archive" / archive_subdir

        def _on_scan_error(exc: OSError, archive_dir: Path = archive_dir) -> None:
            _LOG.warning(
                "handoff_corpus: cannot scan archived handoff subtree %s for "
                "the C3 gate index — %s; an awaiting_gate handoff whose blocked_by "
                "names an archived blocker under this subtree may fail to resolve "
                "(indistinguishable from 'that blocker id does not exist' without "
                "this signal)",
                getattr(exc, "filename", archive_dir), exc,
            )

        def _on_file(p: Path) -> "Optional[Dict[str, Any]]":
            meta = _read_meta(str(p))
            if not meta:
                return None
            meta = dict(meta)
            meta["_path"] = str(p)
            return meta

        archived_handoffs, archive_scan_errors = _walk_archive_md_files(
            archive_dir, _on_file, _on_scan_error,
        )
        all_handoffs.extend(archived_handoffs)
        scan_errors.extend(archive_scan_errors)

    return all_handoffs, scan_errors
