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
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from coordinator_core.dag import _read_meta
from coordinator_core.frontmatter.primitives import unquote_yaml_scalar
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


# ---------------------------------------------------------------------------
# _build_blocker_index — C2 (plan 2026-08-30-the-gate-brief-reads-a-list-
# where-the-record-wrote-one)
# ---------------------------------------------------------------------------

#: Bytes of a candidate file's HEAD read before falling back to a full read.
#: Measured against the live 1165-record corpus (dispatch brief, C2): 72-82ms
#: to build the index at this size vs. 1005ms for the equivalent full-YAML
#: index, with `missing_ids=0, extra_ids=0, path_mismatch=0` — 8192/16384
#: produce IDENTICAL output, so this is the measured floor, not a guess.
_BLOCKER_INDEX_HEAD_BYTES = 4096

#: `^(stub_id|handoff_id):` id lines, extracted from the (possibly truncated)
#: frontmatter head. Trailing comments and empty/null id values do not occur
#: in the corpus (measured 0 each, dispatch brief) — no further coercion is
#: applied beyond `unquote_yaml_scalar` below.
_BLOCKER_ID_LINE_RE = re.compile(rb"^(?:stub_id|handoff_id):[ \t]*(.*?)[ \t]*$", re.MULTILINE)


def _frontmatter_head_bytes(path: Path) -> Optional[bytes]:
    """Read `path`'s frontmatter HEAD — from the opening `---` fence up to
    (excluding) the closing `---` fence — truncated-read at
    `_BLOCKER_INDEX_HEAD_BYTES` with a full-file fallback when the closing
    fence is not found within that truncated head.

    The truncated read is the measured-fast path (see
    `_BLOCKER_INDEX_HEAD_BYTES`'s docstring); the fallback makes the 4096-byte
    constant SELF-CORRECTING rather than silently wrong for an oversized
    frontmatter block — 4 of 1202 live+archived records (measured directly,
    Review: overengineering-reviewer, Finding 5) do not close their
    frontmatter within 4096 bytes and take this fallback TODAY, not zero.
    That is the argument FOR keeping the fallback, not against it: an id
    embedded past the truncation point must still resolve, not silently
    vanish into `unresolvable` (dispatch brief C2).

    Returns `None` on the initial `open`/`read` failing with `OSError` —
    a locked or permission-denied FILE, distinct from `b""` (a real,
    readable, empty file). The caller threads `None` into `scan_errors`
    rather than reading a locked file as "carries no id", which is the
    same corpus-scan-failure hazard `scan_errors` already tracks at
    directory granularity, reopened here at file granularity (Review:
    code-reviewer — Finding 1, verdict BLOCKED on
    2026-08-30-the-gate-brief-reads-a-list-where-the-record-wrote-one). A
    later `OSError` on the full-file fallback read is NOT re-raised as a
    scan error: the truncated head already read successfully, so the
    fallback failing only means the file grew/vanished between the two
    reads — falling back to the (possibly-fenceless) head already read is
    honest, not a silent absence.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_BLOCKER_INDEX_HEAD_BYTES)
    except OSError:
        return None
    idx = head.find(b"\n---", 4)
    if idx != -1:
        return head[:idx]
    if len(head) < _BLOCKER_INDEX_HEAD_BYTES:
        # The whole file fit in the truncated read and still carries no
        # closing fence — there is nothing more to fetch.
        return head
    try:
        full = path.read_bytes()
    except OSError:
        return head
    idx = full.find(b"\n---", 4)
    if idx != -1:
        return full[:idx]
    return full


def _blocker_ids_in_head(head: bytes) -> List[str]:
    """Every `stub_id`/`handoff_id` value in a frontmatter head, unquoted.

    MUST route the raw regex capture through `unquote_yaml_scalar` before use
    as an index key — measured (dispatch brief C2): 625 of 653
    `^stub_id:`/`^handoff_id:` lines in the live+archived corpus carry a
    quoted value (`stub_id: "sat-06"`), and an unquoted key means 96% of
    lookups resolve as `unresolvable`, the exact dangling-ref symptom C3
    argues cannot happen.
    """
    ids: List[str] = []
    for match in _BLOCKER_ID_LINE_RE.finditer(head):
        raw = match.group(1).decode("utf-8", errors="replace").strip()
        value = unquote_yaml_scalar(raw)
        if value:
            ids.append(value)
    return ids


def _build_blocker_index(repo_root: Path) -> "tuple[Dict[str, List[Path]], List[str]]":
    """Return `({id: [Path, ...]}, scan_errors)` — a second, bounded
    projection of the same live+archived handoff corpus
    `_collect_all_handoffs_for_gate_index` walks, keyed by every
    `stub_id`/`handoff_id` a record's frontmatter carries (a `stub_id` alone
    names a whole continuation chain, so more than one path can share a key —
    the caller, not this index, collapses that via `collapse_to_chain_heads`).

    Built over `collect_live_handoff_paths(repo_root)` for the live half and
    `_walk_archive_md_files` for the archived half — never `rglob`, which
    silently swallows `PermissionError` while walking (see
    `_walk_archive_md_files`'s own docstring). An unreadable subtree — OR a
    single unreadable file inside an otherwise-scannable directory (a
    handoff another session is mid-write/locked on, routine rather than a
    corner case at the stated 50-70 concurrent-session load norm; see
    `_frontmatter_head_bytes`) — is surfaced in `scan_errors` rather than
    read as a silent absence indistinguishable from "the id does not
    exist" — the exact dangling-ref hazard the 2026-08-08 PM ruling
    dissolved (dispatch brief C2, see C3; file-granularity gap closed per
    Review: code-reviewer — Finding 1).

    Root set MIRRORS the act-time resolver's own two roots
    (`handoff_transition.py :: _resolve_blocker_deployment_state`), which is
    why they are not simply `_collect_all_handoffs_for_gate_index`'s two
    roots: `state/handoffs/` is walked NON-RECURSIVELY (its `.archive/`
    sibling holds STALE copies of records that have already moved to
    `archive/handoffs/` and must not be double-indexed against a live-looking
    but superseded path), while `archive/handoffs/` is walked RECURSIVELY
    (month-nested, `archive/handoffs/2026-08/...`). `archive/completed/`
    (which `_collect_all_handoffs_for_gate_index` also walks, for its own
    THREE independent consumers — see that function's docstring) carries
    ZERO records with either id key today (measured, dispatch brief C2) and
    is correctly omitted here — a blocker id can never resolve there, so
    walking it would only cost time, never correctness.

    Per-file cost: one bounded read (`_frontmatter_head_bytes`), not a full
    YAML parse — see that function's docstring for the measured 4096-byte
    floor. Per-id lookup after this index is built is a dict hit.
    """
    index: Dict[str, List[Path]] = {}

    def _index_file(path: Path) -> None:
        head = _frontmatter_head_bytes(path)
        if head is None:
            # A directory-level walk found `path`, but reading it failed
            # (locked/permission-denied) — the exact per-FILE instance of
            # the "unreadable subtree" hazard `scan_errors` already tracks
            # at directory granularity (Review: code-reviewer — Finding 1).
            # A blocker id that only this file names must come back
            # `scan_incomplete`, never a silent `unresolvable`.
            scan_errors.append(f"{path}: unreadable (frontmatter head read failed)")
            return
        for blocker_id in _blocker_ids_in_head(head):
            index.setdefault(blocker_id, []).append(path)

    scan_errors: List[str] = []
    live_dir = repo_root / "state" / "handoffs"
    try:
        live_paths = collect_live_handoff_paths(repo_root)
    except OSError as exc:
        _LOG.warning(
            "handoff_corpus: cannot scan live handoff subtree %s for the "
            "blocker index — %s; a blocked_by id naming a live record under "
            "this subtree may fail to resolve (indistinguishable from 'that "
            "blocker id does not exist' without this signal)",
            live_dir, exc,
        )
        scan_errors.append(f"{live_dir}: {exc}")
        live_paths = []
    for path in live_paths:
        _index_file(path)

    archive_dir = repo_root / "archive" / "handoffs"

    def _on_scan_error(exc: OSError) -> None:
        _LOG.warning(
            "handoff_corpus: cannot scan archived handoff subtree %s for the "
            "blocker index — %s; a blocked_by id naming an archived record "
            "under this subtree may fail to resolve (indistinguishable from "
            "'that blocker id does not exist' without this signal)",
            getattr(exc, "filename", archive_dir), exc,
        )

    def _on_file(path: Path) -> None:
        _index_file(path)
        return None

    _, archive_scan_errors = _walk_archive_md_files(archive_dir, _on_file, _on_scan_error)
    scan_errors.extend(archive_scan_errors)

    return index, scan_errors
