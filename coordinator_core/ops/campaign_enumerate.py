"""
coordinator_core.ops.campaign_enumerate — "records.by_origin_plan" op.

Purpose: COMPUTE_ONLY read op that enumerates work-state artifacts (handoffs,
queue entries, decisions, reviews, ...) carrying a given ``origin_plan_id``
frontmatter value, across every non-synthetic record type
``records_query._TYPE_TO_GLOB`` knows about, in a single call.

Spec backlink: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-doe-thread.md (C21,
Item 22).

Re-verify performed before writing this module (per the spec row's own first
instruction): ``records.query --where origin_plan_id=<P> --format json`` already
answers this question for ONE record type per call (its generic ``_resolve_field``
equality match works on any frontmatter key, ``origin_plan_id`` included, with no
code change), but ``type`` is mandatory on that op's single-type path and its only
multi-type union lens (``--unattached``) filters on a DIFFERENT predicate
(``initiative`` null/absent over a fixed 6-type set), not an arbitrary field over
the full type set. There is therefore no existing single call that unions every
record type by an arbitrary field and projects ``{path, kind, status}`` — the gap
named in the spec row's re-verify clause — so this module is built.

Placement: a new op module registered through ``_registry_map.py`` (the fleet's
standing pattern for a records-adjacent op that is not the byte-frozen
``records_query.py`` itself — see ``records.history``/``record_history.py`` for
the precedent this follows), rather than a new addition inside
``records_query.py``. No second scanner: every record is read through
``records_query._collect_type_records``, the SAME frontmatter reader
``records.query`` and ``records.history`` already share — this module adds only
the cross-type union, the ``origin_plan_id`` filter, and the ``{path, kind,
status}`` projection on top of it.

Negative-spec:
    - Does NOT re-parse frontmatter or re-walk any glob itself — every candidate
      file is collected via ``records_query._collect_type_records``.
    - Does NOT touch ``records_query.py`` or ``authz/classification.py`` —
      those files are outside this dispatch's footprint. ``op_scopes.py`` IS
      touched (registers ``"records.by_origin_plan": "common_dir"`` in
      ``_OP_KEY_SCOPE``, mirroring ``records.query``'s own scope) — see this
      op's dispatch report for the classification registration gap and who
      owns closing it.
    - Excludes ``records_query._SYNTHETIC_TYPES`` (``handoff-ledger``,
      ``research-claim``) — N-records-per-file types with no single on-disk
      ``path``/``kind``/``status`` triple to project, same exclusion
      ``record_history.supported_record_types()`` already applies for the
      same reason.
    - Does NOT add caching. The spec row's own instruction is to measure
      process time over the live corpus and stop-and-report over 500ms rather
      than reach for caching — see this op's dispatch report for the measurement.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops import records_query
from coordinator_core.ops.fleet._common import main_worktree_root

# Record types this op enumerates over — every wired type except the
# synthetic (N-records-per-file) ones, which have no single path/kind/status
# triple to project. Mirrors record_history.supported_record_types()'s own
# exclusion of records_query._SYNTHETIC_TYPES, computed the same way (a set
# difference against the live _TYPE_TO_GLOB map) so a newly-wired type is
# picked up here automatically rather than needing a second hand-maintained
# list to stay in sync.
_ENUMERABLE_TYPES: frozenset[str] = (
    frozenset(records_query._TYPE_TO_GLOB) - records_query._SYNTHETIC_TYPES
)


def enumerable_record_types() -> frozenset[str]:
    """The record types this op unions over — see ``_ENUMERABLE_TYPES``."""
    return _ENUMERABLE_TYPES


def _artifacts_for_origin_plan(
    worktree_root: Path, origin_plan_id: str,
) -> list[dict]:
    """Union every enumerable record type, filtered to ``origin_plan_id``.

    Returns a list of ``{"path": str, "kind": str, "status": str | None}``
    dicts, one per matching record, in ``_ENUMERABLE_TYPES`` iteration order
    (insertion order of ``records_query._TYPE_TO_GLOB``, itself alphabetical-
    within-directory via ``_collect_type_records``'s own sort — no additional
    sort is applied here; a caller wanting a different order sorts client-
    side, same as ``records.query`` callers do for its own results).

    A directory-scan failure for one type (``_RecordsCollectError``) is
    warned-and-skipped rather than aborting the whole union — same
    try/continue shape ``records_query._query_unattached_all`` already uses
    for exactly this failure mode, so one broken type never turns a partial
    answer into a hard error for every other type.
    """
    artifacts: list[dict] = []
    for record_type in sorted(_ENUMERABLE_TYPES):
        try:
            type_records = records_query._collect_type_records(worktree_root, record_type)
        except records_query._RecordsCollectError as exc:
            sys.stderr.write(
                f'records.by_origin_plan: skipping type "{record_type}" — {exc}\n'
            )
            continue
        for rec in type_records:
            fm = rec.get('frontmatter', {})
            if fm.get('origin_plan_id') != origin_plan_id:
                continue
            artifacts.append({
                'path': rec['path'],
                'kind': record_type,
                'status': fm.get('status'),
            })
    return artifacts


@register_op("records.by_origin_plan")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """COMPUTE_ONLY: enumerate work-state artifacts by ``origin_plan_id``.

    Params:
        origin_plan_id: str (required) — equality-matched against each
                        record's ``origin_plan_id`` frontmatter field.

    Returns:
        ``{"origin_plan_id": <str>, "artifacts": [{"path", "kind", "status"}, ...]}``

    Worktree resolution mirrors ``records.query``/``records.history``:
        repo_root provided -> main_worktree_root(repo_root) -> worktree_root
        repo_root absent   -> well-formed empty artifacts list, no raise
                              (an unknown worktree is not a 500 — same
                              contract records.query documents for itself).

    Raises ``ValueError`` when ``origin_plan_id`` is missing/empty — an
    unfiltered "every artifact in the repo" union is never a useful silent
    default for a by-ID lookup, unlike records.query's own type-required
    guard this mirrors in spirit.
    """
    origin_plan_id = params.get('origin_plan_id')
    if not origin_plan_id:
        raise ValueError(
            "records.by_origin_plan requires a non-empty 'origin_plan_id'"
        )

    if repo_root is None:
        return {'origin_plan_id': origin_plan_id, 'artifacts': []}

    worktree_root = main_worktree_root(repo_root)
    artifacts = _artifacts_for_origin_plan(worktree_root, origin_plan_id)
    return {'origin_plan_id': origin_plan_id, 'artifacts': artifacts}
