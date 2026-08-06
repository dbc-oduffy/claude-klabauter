"""
coordinator_core.ops.ceremony.records_query — in-process records-query helper for ceremony
renderers.

Purpose: frontmatter enumerate + full-grammar ``where``/``since``-filter over every
record type ``coordinator_core.ops.records_query`` supports — the read-side foundation C8b
(``render-handoff-tracker.js`` port) and C8c (``refresh-queries.js`` roadmap-callout port)
build their table assembly on.  Unlike ``coordinator_core.ops.records_query`` (the JSON-RPC
``"records.query"`` op), this is a plain synchronous Python function called in-process by the
renderer ops — no ``register_op``, no async handler, no wire envelope.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C8a
Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4d-g1
  (query-records.js grammar EXTEND, ``since=`` + full type-set widening)

Negative-spec:
  - Does NOT depend on, and is NOT depended on by, C6's memo-sweep
    (``fleet.archive_actioned_memos``) — the two are disjoint helpers over the same
    ``cross-repo/inbox/`` directory for different purposes (sweep mutates/archives;
    this helper only reads).
  - Does NOT register a JSON-RPC op — this is an in-process library call for C8b/C8c to
    import directly, not a wire surface reachable via ``cc_invoke``.
  - Does NOT reimplement file-collection, ``.yaml``-vs-``.md`` parsing, the
    ``where``/``since`` grammar, consumed-marker normalization, roadmap-status
    normalization, ``liveness`` injection, the synthetic
    ``handoff-ledger``/``research-claim`` record assembly, or the DR-115
    legacy-prose-queue invisibility signal — reuses
    ``coordinator_core.ops.records_query``'s ``_TYPE_TO_GLOB`` / ``_collect_files`` /
    ``_load_record`` / ``_apply_plan_filename_filter`` / ``_matches_where`` /
    ``_parse_where`` / ``_parse_since`` / ``_collect_handoff_ledger_records`` /
    ``_collect_research_claim_records`` / ``_legacy_prose_signal`` verbatim so the same
    faithful-strangle logic (ported from ``query-records.js``) does not drift into a
    second, silently-diverging copy — this module now supports every type
    ``_TYPE_TO_GLOB`` does, by construction. ``legacy_prose_signal()`` (below) is a
    thin public re-export of ``_legacy_prose_signal`` — the JSON-RPC ``records.query``
    op (``coordinator_core.ops.records_query._handler``) and this in-process seam
    therefore compute the SAME signal for the same ``(record_type, worktree_root)``
    pair by construction, not by a parity test alone (see
    ``coordinator_core/ops/ceremony/tests/test_records_query.py``'s
    ``TestLegacyProseSignalParity`` for the drift-guard regardless).
  - ``query_records`` does NOT implement ``--older-than`` or ``--sort`` — out of
    scope for the single-type renderer call sites it serves; add if a caller needs
    them (same ``coordinator_core.ops.records_query`` helpers back them already).
  - ``query_unattached_all`` (below) ports the ``--unattached`` multi-type union
    lens — ``coordinator_core.ops.records_query.UNATTACHED_TYPES`` /
    ``_query_unattached_all`` verbatim, same no-second-copy rationale as the rest
    of this module. ``--fleet`` remains unported — out of scope for the renderer
    call sites this helper serves; the synthetic ``lesson``/``handoff-ledger``/
    ``research-claim`` types (and ``decision``/``review``) ARE now supported, same as
    ``coordinator_core.ops.records_query``.
"""

from __future__ import annotations
import sys

from pathlib import Path
from typing import Optional

from coordinator_core.ops.records_query import (
    _SYNTHETIC_TYPES,
    _RecordsCollectError,
    _TYPE_TO_GLOB,
    _apply_plan_filename_filter,
    _collect_files,
    _collect_handoff_ledger_records,
    _collect_research_claim_records,
    _legacy_prose_signal,
    _load_record,
    _matches_where,
    _parse_older_than,
    _parse_since,
    _parse_where,
    _query_unattached_all,
)


# ---------------------------------------------------------------------------
# Query entrypoint
# ---------------------------------------------------------------------------


def query_records(
    record_type: str,
    worktree_root: Path,
    *,
    where: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 0,
) -> list[dict]:
    """Enumerate + since/where-filter ``record_type`` records under ``worktree_root``.

    Params:
        record_type: any key of ``coordinator_core.ops.records_query._TYPE_TO_GLOB``
                     (``handoff``, ``handoff-archived``, ``plan``, ``cross-repo-memo``,
                     ``bug``, ``debt``, ``improvement``, ``tracker``, ``roadmap``,
                     ``health-status``, ``decision-guide``, ``completion``,
                     ``decision``, ``review``, ``lesson``, ``handoff-ledger``,
                     ``research-claim``).
        worktree_root: repo worktree root (already resolved by the caller).
        where: optional full-grammar expression (``=``, ``!=``, ``<``, ``>``, ``<=``,
               ``>=``, ``field in (a,b,c)``, bare-field presence) — see
               ``coordinator_core.ops.records_query._parse_where``. An unparseable
               clause raises ``SystemExit`` (fail-loud, same contract).
        since: optional ``Nd``/``Nw``/``Nm``/``YYYY-MM-DD`` — filters ``created >=
               cutoff`` (records lacking ``created`` are excluded), applied BEFORE
               ``where`` (query-records.js:1469-1493 ordering). Invalid input raises
               ``SystemExit`` (propagated from ``_parse_since``).
        limit: max records after since/where filtering. ``<= 0`` means unlimited —
               mirrors ``query-records.js``'s ``opts.limit && opts.limit > 0`` guard
               (query-records.js:1509-1511).

    Returns:
        A list of ``{"path": <repo-relative str>, "frontmatter": <dict>}`` dicts.  Files
        with no parseable frontmatter block are silently skipped (mirrors
        ``query-records.js``'s default ``includeUnparseable=false`` behavior — renderers
        that need fail-loud parse-error surfacing do their own frontmatter=None handling
        on top of this helper, per C8b's active-filter spec).

    Raises:
        ValueError: unknown ``record_type``.
        SystemExit: unsupported ``where`` operator or invalid ``since`` value.
    """
    if record_type not in _TYPE_TO_GLOB:
        valid = ", ".join(sorted(_TYPE_TO_GLOB))
        raise ValueError(f"records_query: unknown type: {record_type!r}. Valid: {valid}.")

    clauses: list[dict] = []
    if where:
        clauses = _parse_where(where)  # may sys.exit(1) on unsupported operator

    since_cutoff = _parse_since(since)  # may sys.exit(1) on invalid value

    try:
        if record_type in _SYNTHETIC_TYPES:
            if record_type == "handoff-ledger":
                results: list[dict] = _collect_handoff_ledger_records(worktree_root)
            else:
                results = _collect_research_claim_records(worktree_root)
        else:
            candidates = _collect_files(worktree_root, record_type)

            if record_type == "plan":
                candidates = _apply_plan_filename_filter(candidates)

            results = []
            for fpath in candidates:
                rec = _load_record(fpath, worktree_root, record_type)
                if rec is not None:
                    results.append(rec)
    except _RecordsCollectError as exc:
        # This helper carries no incomplete/error signal in its bare-list return
        # shape (unlike the records.query op) — fail-open with a stderr note, same
        # posture as this module's pre-existing OSError handling.
        print(
            f"skip: query_records: _collect_files({record_type!r}) failed: {exc}",
            file=sys.stderr,
        )
        return []

    if since_cutoff:
        results = [
            r for r in results
            if r["frontmatter"].get("created")
            and str(r["frontmatter"]["created"]) >= since_cutoff
        ]

    if clauses:
        results = [r for r in results if _matches_where(r["frontmatter"], clauses)]

    if limit > 0:
        results = results[:limit]

    return results


# ---------------------------------------------------------------------------
# DR-115 legacy-prose-queue invisibility signal
# ---------------------------------------------------------------------------


def legacy_prose_signal(record_type: str, worktree_root: Path) -> Optional[dict]:
    """Return ``{"count", "path"}`` when ``record_type`` has unindexed legacy
    prose-queue entries on disk under ``worktree_root``, else ``None``.

    Thin re-export of ``coordinator_core.ops.records_query._legacy_prose_signal``
    (DR-115 — example-doctrine-repo ``docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-
    staleness.md``) — the single shared implementation, not a second reader. See
    that function's own docstring for the exact ``None``-vs-populated rules
    (no known legacy path for this type, path absent, zero pipe-row entry
    lines, or a genuine non-empty legacy queue independent of the per-entry
    YAML directory's own count).

    Deliberately NOT folded into ``query_records()``'s own return shape:
    ``query_records()`` returns a bare ``list[dict]`` consumed by ``len()``/
    list-comprehension callers throughout this seam's importers
    (``coordinator_core/roadmap/audit.py``, ``coordinator_core/goals/
    reassess_krs.py``, ``coordinator_core/reconcile/gate_eval.py``,
    ``coordinator_core/ops/emit/sections/backlogs.py``) — wrapping that return
    value in a dict to carry a flat signal key would be a breaking contract
    change for every existing caller, for a signal only two of the many
    queryable types (``improvement``, ``bug``) ever carry. Call this function
    separately, alongside ``query_records()``, for the same
    ``(record_type, worktree_root)`` pair when a caller needs to report queue
    depth.
    """
    return _legacy_prose_signal(worktree_root, record_type)


# ---------------------------------------------------------------------------
# --unattached union lens
# ---------------------------------------------------------------------------


def query_unattached_all(
    worktree_root: Path,
    *,
    where: Optional[str] = None,
    since: Optional[str] = None,
    older_than: Optional[str] = None,
    sort: Optional[str] = None,
    limit: int = 0,
) -> list[dict]:
    """Union ``--unattached`` records across ``coordinator_core.ops.records_query
    .UNATTACHED_TYPES`` (``bug``, ``debt``, ``improvement``, ``roadmap``,
    ``handoff``, ``plan``) — the null-initiative-FK multi-type lens.

    In-process wrapper over ``coordinator_core.ops.records_query
    ._query_unattached_all`` — same param-parsing convention as ``query_records``
    above (``where``/``since`` parsed via the shared ``_parse_where``/``_parse_since``
    helpers, raising ``SystemExit`` on unparseable input, same contract).

    Params:
        worktree_root: repo worktree root (already resolved by the caller).
        where/since/older_than: same grammar as ``query_records``, applied PER
                     TYPE before the union is assembled.
        sort/limit:  applied ONCE to the assembled union, not per type — a
                     per-type limit would truncate before the union is
                     complete and silently produce a different, wrong result
                     set (this ordering is load-bearing, see
                     ``_query_unattached_all``'s docstring).

    Returns:
        A list of ``{"path": ..., "frontmatter": ..., "_type": <source type>}``
        dicts — the ``_type`` tag lets a caller (e.g. markdown-list rendering)
        pick the right per-type display function for each record.
    """
    clauses: list[dict] = []
    if where:
        clauses = _parse_where(where)  # may sys.exit(1) on unsupported operator

    since_cutoff = _parse_since(since)  # may sys.exit(1) on invalid value
    older_than_cutoff = _parse_older_than(older_than) if older_than else None

    return _query_unattached_all(
        worktree_root,
        clauses=clauses,
        since_cutoff=since_cutoff,
        older_than_cutoff=older_than_cutoff,
        sort_str=sort,
        limit=limit,
    )
