"""
coordinator_core.ops.handoff_columns_query — JSON-RPC "handoff.columns" operation.

Purpose: COMPUTE_ONLY read op answering "what are the four cockpit columns
(status, deployment_state, predecessor, shipped_in) for this repo's handoffs,
live plus optionally archived?" — the pull-first surface DR-287's 2026-08-11
addendum names as the replacement for cockpit's retiring emission read.

C3 of docs/plans/2026-08-11-pull-surface-four-columns-and-the-archive.md.
DEPENDS ON C1 (coordinator_core/ops/emit/sections/handoff_columns.py, the
per-record AND batch column computation) and C2 (coordinator_core/ops/
records_query.py's opt-in ``include_archived`` collector). This module is a
thin joining layer: it invents no collection grammar and no column
computation of its own — it calls C2's collector for the record set, C1's
batch entry point for the four columns, and reuses C2's ``where``/``since``
grammar untouched.

THE MOST IMPORTANT CONSTRAINT (see handoff_columns.py's
``compute_handoff_columns_batch`` docstring for the full rationale): a naive
per-record loop calling ``compute_handoff_columns`` would spawn one ``git
log`` subprocess per record — 400+ spawns for this repo's own corpus (133
live + 284 archived handoffs). This handler collects every record's raw
``shipped_in`` SHA up front and joins them via ONE
``compute_handoff_columns_batch`` call, which itself makes exactly ONE
``_resolve_shipped_in_dates`` (and therefore ONE ``git log``) subprocess
spawn regardless of corpus size — O(1), not O(records).

Self-registration: importing this module calls
``register_op("handoff.columns", _handler)`` as a side-effect. Listed in
``coordinator_core/ops/__init__.py``'s eager-import table so
``import coordinator_core.ops`` (the default, non-lazy path) picks it up.

Row shape: one row per record, carrying the repo-relative ``path`` plus
EXACTLY ``status``, ``deployment_state``, ``predecessor``, ``shipped_in`` —
five keys, nothing else. The full-frontmatter bulk records.query's own
``format=json`` returns is precisely what cockpit asked to stop receiving
(~91% of the payload unused by their consumer) — this op is the narrow
replacement, not a second copy of the wide one.

Wire bridging: the CLI-facing param is ``archive`` (C4's choice, mirroring
``--archive`` as a bare flag) — this handler maps it onto C2's
``include_archived`` keyword internally. Neither side is renamed to match
the other; this module is the seam that bridges them.

Worktree resolution mirrors records_query.py's own handler: repo_root
provided -> ``main_worktree_root(repo_root)``; repo_root absent -> a
well-formed empty payload, no raise (never a 500 for an unknown worktree).

Negative-spec: does NOT reimplement ``_TYPE_TO_GLOB``, ``_ARCHIVE_GLOB_FOR_TYPE``,
the ``--where``/``--since`` grammar, or the four-column computation — all
four are imported/called, never mirrored. Does NOT emit any frontmatter key
beyond the four columns plus ``path``.

Spec backlink: docs/plans/2026-08-11-pull-surface-four-columns-and-the-archive.md § C3
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.emit.sections.handoff_columns import compute_handoff_columns_batch
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.records_query import (
    _apply_since_where_filters,
    _collect_type_records,
    _parse_since,
    _parse_where,
)

_LOG = logging.getLogger(__name__)

# Only the four columns' four keys plus the path identifier — see module
# docstring's "Row shape" section. Kept as a named tuple of keys (rather than
# inlined at each call site) so the row-shape contract is grep-able in one
# place and the row-shape test can assert against it directly if desired.
_COLUMN_KEYS: tuple[str, ...] = ("status", "deployment_state", "predecessor", "shipped_in")


def _empty_payload() -> dict:
    return {"records": []}


@register_op("handoff.columns")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.columns" handler.

    Params (all optional):
        where:   str — reuses ``records_query``'s full ``--where`` grammar
                 (``=``, ``!=``, ``<``, ``>``, ``<=``, ``>=``, ``field in
                 (a,b,c)``, bare-field presence). Invalid syntax raises
                 ``SystemExit(1)``, same as ``records.query``.
        since:   str — ``Nd``/``Nw``/``Nm``/``YYYY-MM-DD``, filters
                 ``created >= cutoff`` (records lacking ``created`` excluded).
                 Same parser as ``records.query``.
        archive: bool — archive coverage, DEFAULT ON (True) for this op. Pass
                 ``False`` explicitly to opt OUT and see live handoffs only.
                 Maps onto ``records_query._collect_type_records``'s
                 ``include_archived`` keyword — the wire-facing name is
                 ``archive`` (C4's choice), the internal one stays
                 ``include_archived`` (C2's choice); this handler is the
                 bridge, neither side is renamed.

                 Review: default-ON is deliberate and specific to this op —
                 it has exactly one intended consumer (cockpit's fleet
                 board), whose whole purpose is showing shipped work, so
                 live-only is the useless default here. This does NOT extend
                 to ``records.query``, whose ``include_archived`` stays
                 default-off — that op has many existing callers whose
                 result sets must not move (AC2).

    Returns:
        ``{"records": [{"path": ..., "status": ..., "deployment_state": ...,
        "predecessor": ..., "shipped_in": {"sha": ..., "date": ...} | None},
        ...]}`` — one row per matching handoff record, five keys each,
        nothing else.

    Absent ``repo_root`` -> well-formed empty payload, no raise (mirrors
    ``records.query``'s own absent-repo_root behaviour).
    """
    where_str: str = params.get("where") or ""
    since_str: Optional[str] = params.get("since")
    include_archived: bool = bool(params.get("archive", True))

    if repo_root is None:
        _LOG.warning(
            "handoff.columns: repo_root absent — returning empty payload",
        )
        return _empty_payload()

    worktree_root = main_worktree_root(repo_root)

    clauses = _parse_where(where_str) if where_str else []  # may sys.exit(1) on bad syntax
    since_cutoff = _parse_since(since_str)  # may sys.exit(1) on bad syntax

    records = _collect_type_records(
        worktree_root, "handoff", include_archived=include_archived,
    )
    records = _apply_since_where_filters(
        records, since_cutoff=since_cutoff, clauses=clauses,
    )

    # ONE batch call for the whole matching set — O(1) git-log spawns, not
    # O(records). See module docstring and compute_handoff_columns_batch's
    # own docstring for the full rationale.
    columns = compute_handoff_columns_batch(
        [rec["frontmatter"] for rec in records], worktree_root,
    )

    rows = [
        {"path": rec["path"], **cols}
        for rec, cols in zip(records, columns)
    ]
    return {"records": rows}
