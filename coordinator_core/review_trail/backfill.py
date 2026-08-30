"""
coordinator_core.review_trail.backfill

Purpose: the ONE resolution path (write-time and one-shot backfill alike)
that applies `coverage.py`'s five preserved credit rules to review-trail
record(s) and folds the resulting credited SHA set into the reviewed_set
store (`coordinator_core.review_trail.reviewed_set`).

Two callers, one code path (brief: "it is the same operation as healing a
crash-interrupted write, so ship one code path, not two"):
  * `coordinator_core.ops.review_trail_write` calls `resolve_and_fold` with
    exactly ONE (record_id, record) pair, immediately after that record's
    file is durably created (write-time resolution) — never before.
  * `run_backfill` below calls the same `resolve_and_fold` with the full
    batch of not-yet-folded on-disk records under
    `{repo_root}/state/review-trail/*.json` — the one-shot pass over the
    4759 already-written records, and the same self-healing an interrupted
    write-time call needs on its next attempt.

Record identity is the trail record's own file path, relative to
`repo_root`, POSIX-separated (`state/review-trail/<file>.json`), matching
`reviewed_set.py`'s own docstring suggestion ("a trail file path"). A
legacy JSONL file (more than one JSON object per file — `_parse_trail_file`
in `coverage.py` already tolerates this shape) gets one record id per line,
suffixed `#<index>`.

Preserve exactly, by symbol (C1's "Preserve exactly" list, extended to
write time): this module does NOT re-derive any of the five credit rules —
it imports them from `coordinator_core.coverage`:
    1. `_verdict_counts`                  — verdict filter.
    2. `_record_range_has_stored_head`    — HEAD-anchored exclusion.
    3. `_classify_bookkeeping_shas`       — the planning-artifact classifier
                                             `_credit_from_kind_partition`
                                             filters a `scope_kind="plan"`
                                             record's resolved range
                                             against; `scope_kind=
                                             "integration"` is excluded
                                             outright, never resolved.
    4. `_narrow_foreign_session_scope` (+ `_FOREIGN_STRIPPED_SCOPES`) —
       strips provably-foreign commits from a record's resolved range
       before it is folded. An INPUT to the folded set, not a downstream
       consumer.
    5. The never-path-scoped asymmetric scope rule — a no-op here: nothing
       in this module ever filters by path, so there is nothing to
       preserve except NOT introducing a path filter.

Endpoint normalization (this chunk's own "endpoint rule", matching
`reviewed_set.fold_in`'s contract exactly, since the common-case "diff"/
legacy record path below delegates straight to `fold_in`): a record whose
sha_range endpoints do not BOTH resolve to a full 40-hex SHA present in the
`git rev-list --all --parents` reach-set resolves to UNRESOLVED, never the
empty set — retried on the next call (write-time retry is the next
`run_backfill`; backfill retries on its own next invocation).

NOT a gate-path cost: nothing here is invoked implicitly by a coverage
gate. `run_backfill` runs once, on deliberate operator invocation; no gate
caller may trigger it. A gate meeting an unresolved record simply reads it
as uncredited (via `reviewed_set.read_reviewed_set`) and moves on — the
conservative direction, same as every other exclusion in this module.

Negative-spec:
    - Does NOT write `state/review-trail/*.json` — only the reviewed_set
      store under `.git/coordinator-review-trail/` (via `reviewed_set`'s
      own append primitives). Never mutates a trail record.
    - Does NOT treat an unresolvable endpoint, or a git failure resolving
      the reach-set / a range / a per-token endpoint, as the empty set —
      see "Endpoint normalization" above.
    - Does NOT fire from any gate call site — see "NOT a gate-path cost".
    - Does NOT re-fold a record id already present in
      `reviewed_set.read_folded_record_ids` — `run_backfill` filters
      before calling `resolve_and_fold`; `resolve_and_fold` itself folds
      whatever it is given, unconditionally (matching `fold_in`'s own
      contract), so a caller that wants idempotency must filter first.

Spec backlink: docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C1b
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from coordinator_core import coverage
from coordinator_core.review_trail import reviewed_set as _store

#: Bounded fan-out for the "special" (plan-kind / foreign-scoped) per-range
#: `git rev-list` resolution below — mirrors `coverage.py`'s own
#: `_REVLIST_MAX_WORKERS` bound (distinct ranges are independent read-only
#: shell-outs; unbounded fan-out is the "365-spawn fan-out... wearing a
#: migration's clothes" shape this chunk's brief explicitly refuses).
_REVLIST_MAX_WORKERS = 16


class RecordDisposition:
    """String constants for `resolve_and_fold`'s per-record verdict."""

    FOLDED = "folded"
    #: Rule-excluded: verdict=pending, scope_kind=integration, a stored
    #: literal-HEAD range, or an unrecognized/missing range shape. Permanent
    #: — the record's own fields never change (additive-create trail), so
    #: this classification is stable and the id is marked folded (with zero
    #: contribution) so it is never re-attempted.
    EXCLUDED = "excluded"
    #: Retryable: an endpoint or range could not be resolved this call
    #: (abbreviated SHA, malformed ^N, git failure, ...). NOT marked folded
    #: — the next `resolve_and_fold` call (write-time retry is out of
    #: scope; backfill's next run is the retry path) re-attempts it.
    UNRESOLVED = "unresolved"


def _record_kind(record: dict) -> Optional[str]:
    """The record's creditable `kind`, or `None` if it is excluded outright
    (unrecognized/`"integration"` scope_kind, or a missing/non-diff-shaped
    range on a legacy record) — mirrors `coverage.py::build_reviewed_set`'s
    own Phase 1 classification exactly, since the folded set must credit
    the identical corpus the retiring per-call builder did (C2's
    equivalence proof)."""
    scope_kind = record.get("scope_kind")
    sha_range = record.get("sha_range") or ""
    if scope_kind is not None:
        if scope_kind == "integration":
            return None
        if not sha_range or scope_kind not in coverage._RECOGNIZED_SCOPE_KINDS:
            return None
        return scope_kind
    if not sha_range or ".." not in sha_range:
        return None
    return "diff"


def _resolve_special(
    repo_root: str,
    special: List[Tuple[str, dict, str]],
    dispositions: Dict[str, str],
) -> None:
    """Resolve+fold the subset of records needing rule 3 (plan-kind
    partitioning) and/or rule 4 (foreign-session narrowing) — anything
    `resolve_and_fold` cannot hand straight to `reviewed_set.fold_in`,
    which folds a whole range unconditionally. Mutates `dispositions` in
    place for every id in `special`.

    Batches exactly like `fold_in` itself (one reach-set build, one
    batched endpoint resolve, one dedup'd `git rev-list` per DISTINCT
    range) plus ONE `_classify_bookkeeping_shas` pass for the whole
    plan-kind bucket — never one spawn set per record (the spawn-budget
    concern this chunk's brief names explicitly)."""
    reach_set = _store._build_reach_set(repo_root)
    if reach_set is None:
        for record_id, _record, _kind in special:
            dispositions[record_id] = RecordDisposition.UNRESOLVED
        return

    parsed: Dict[str, Tuple[str, str]] = {}
    tokens: Set[str] = set()
    for record_id, record, _kind in special:
        split = _store._split_range(record["sha_range"])
        if split is None:
            dispositions[record_id] = RecordDisposition.UNRESOLVED
            continue
        parsed[record_id] = split
        tokens.add(split[0])
        tokens.add(split[1])

    endpoint_shas = _store._resolve_endpoints_batch(sorted(tokens), repo_root)

    # (record_id, record, kind, sha_range) for everything whose endpoints
    # resolved and land in the reach-set.
    resolvable: List[Tuple[str, dict, str, str]] = []
    for record_id, record, kind in special:
        if record_id in dispositions:
            continue
        split = parsed.get(record_id)
        if split is None:
            continue
        left, right = split
        left_sha = endpoint_shas.get(left)
        right_sha = endpoint_shas.get(right)
        if left_sha is None or right_sha is None:
            dispositions[record_id] = RecordDisposition.UNRESOLVED
            continue
        if left_sha not in reach_set or right_sha not in reach_set:
            dispositions[record_id] = RecordDisposition.UNRESOLVED
            continue
        resolvable.append((record_id, record, kind, record["sha_range"]))

    # Dedup identical ranges — one `git rev-list` spawn per DISTINCT range.
    distinct_ranges: Dict[str, List[Tuple[str, dict, str]]] = {}
    for record_id, record, kind, sha_range in resolvable:
        distinct_ranges.setdefault(sha_range, []).append((record_id, record, kind))

    def _revlist(sha_range: str) -> Tuple[str, int, str]:
        rc, out, _err = _store._run(["rev-list", sha_range], repo_root)
        return sha_range, rc, out

    ranges = list(distinct_ranges.keys())
    if not ranges:
        return
    max_workers = min(len(ranges), _REVLIST_MAX_WORKERS)
    if max_workers <= 1:
        range_results = [_revlist(r) for r in ranges]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            range_results = list(pool.map(_revlist, ranges))

    range_shas: Dict[str, FrozenSet[str]] = {}
    for sha_range, rc, out in range_results:
        if rc != 0:
            for record_id, _record, _kind in distinct_ranges[sha_range]:
                dispositions[record_id] = RecordDisposition.UNRESOLVED
            continue
        range_shas[sha_range] = frozenset(
            s.strip() for s in out.splitlines() if s.strip()
        )

    # Rule 3 — one _classify_bookkeeping_shas pass for the WHOLE plan-kind
    # bucket across every still-live "special" record, not per record.
    plan_pool: Set[str] = set()
    for sha_range, entries in distinct_ranges.items():
        if sha_range not in range_shas:
            continue
        for _record_id, _record, kind in entries:
            if kind == "plan":
                plan_pool.update(range_shas[sha_range])
    planning_set: FrozenSet[str] = frozenset()
    if plan_pool:
        _exhaust, planning, _note = coverage._classify_bookkeeping_shas(
            list(plan_pool), repo_root, {},
        )
        planning_set = planning

    # Rule 4 — shared narrowing cache across records (many share a range or
    # session_id).
    session_cache: Dict[Tuple[str, Optional[str]], FrozenSet[str]] = {}

    new_shas: Set[str] = set()
    folded_ids: List[str] = []

    for sha_range, entries in distinct_ranges.items():
        if sha_range not in range_shas:
            continue
        base_shas = range_shas[sha_range]
        for record_id, record, kind in entries:
            credited = base_shas
            if kind == "plan":
                credited = credited & planning_set
            scope = record.get("scope")
            session_id = record.get("session_id")
            if scope in coverage._FOREIGN_STRIPPED_SCOPES:
                try:
                    foreign = coverage._narrow_foreign_session_scope(
                        sha_range, session_id, repo_root, session_cache,
                    )
                except coverage._ForeignSessionLookupError:
                    dispositions[record_id] = RecordDisposition.UNRESOLVED
                    continue
                credited = credited - foreign
            new_shas.update(credited)
            folded_ids.append(record_id)
            dispositions[record_id] = RecordDisposition.FOLDED

    if folded_ids or new_shas:
        existing = _store.read_reviewed_set(repo_root)
        to_append = frozenset(new_shas - existing)
        # Write ordering (finding 1, mirrored from `fold_in`): SHAs first
        # and flushed, THEN the folded ids.
        _store._append_shas(repo_root, to_append)
        _store._append_folded_ids(repo_root, folded_ids)


def resolve_and_fold(repo_root: str, records: List[Tuple[str, dict]]) -> Dict[str, str]:
    """Apply the five credit rules to `records` and fold every creditable
    result into the reviewed_set store. Returns `{record_id: disposition}`
    (see `RecordDisposition`).

    `records` is folded unconditionally — a caller wanting idempotency
    (i.e. `run_backfill`) must filter out ids already in
    `reviewed_set.read_folded_record_ids` BEFORE calling this. The
    write-time caller (`ops.review_trail_write`) always passes a brand-new
    record id, so it never needs to filter.

    The common case (a "diff"-kind record with no foreign-session
    narrowing owed) is delegated straight to `reviewed_set.fold_in` — the
    already-shipped, already-spawn-budgeted endpoint-normalization +
    range-fold path (C1). Only records actually needing rule 3 (plan-kind
    partitioning) or rule 4 (foreign-session narrowing) take the slower
    `_resolve_special` path.
    """
    dispositions: Dict[str, str] = {}
    excluded_permanent: List[str] = []
    plain: List[Tuple[str, str]] = []
    special: List[Tuple[str, dict, str]] = []

    for record_id, record in records:
        sha_range = record.get("sha_range") or ""
        if not coverage._verdict_counts(record):
            excluded_permanent.append(record_id)
            continue
        if sha_range and coverage._record_range_has_stored_head(sha_range):
            excluded_permanent.append(record_id)
            continue
        kind = _record_kind(record)
        if kind is None:
            excluded_permanent.append(record_id)
            continue
        scope = record.get("scope")
        if kind == "plan" or scope in coverage._FOREIGN_STRIPPED_SCOPES:
            special.append((record_id, record, kind))
        else:
            plain.append((record_id, sha_range))

    if excluded_permanent:
        _store._append_folded_ids(repo_root, excluded_permanent)
        for record_id in excluded_permanent:
            dispositions[record_id] = RecordDisposition.EXCLUDED

    if plain:
        result = _store.fold_in(repo_root, plain)
        for record_id in result.folded_record_ids:
            dispositions[record_id] = RecordDisposition.FOLDED
        for record_id in result.unresolved_record_ids:
            dispositions[record_id] = RecordDisposition.UNRESOLVED

    if special:
        _resolve_special(repo_root, special, dispositions)

    return dispositions


# ---------------------------------------------------------------------------
# One-shot backfill over the already-on-disk corpus.
# ---------------------------------------------------------------------------


@dataclass
class BackfillResult:
    """Outcome of one `run_backfill` call."""

    folded: List[str] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)
    #: Files present on disk that could not be parsed at all (neither JSON
    #: nor JSONL) — reported, never silently dropped.
    parse_failures: List[str] = field(default_factory=list)


def _iter_trail_records(
    repo_root: str,
) -> Tuple[List[Tuple[str, dict]], List[str]]:
    """Every `(record_id, record)` pair under `{repo_root}/state/review-trail/`
    AND `{repo_root}/archive/review-trail/` (`*.json`, sorted within each
    directory — every `state/` record precedes every `archive/` record;
    nothing depends on a single global sort across the two). `record_id`
    is the file's path relative to `repo_root`, POSIX-separated — matching
    what the write-time caller (`ops.review_trail_write`) uses for the
    SAME file, so a record folded at write time is never re-folded here.
    This equivalence assumes `repo_root` here and `caller_worktree` there
    are the SAME path for a given file; neither module enforces it — see
    `ops.review_trail_write`'s record_id comment.
    A multi-record (JSONL) file gets one id per line, suffixed `#<index>`.

    Both directories are scanned, RECURSIVELY, because the `state/` ->
    `archive/` archival move is a relocation, not a retraction: an archived
    record still credits its SHAs, and `coverage._collect_trail_paths` — the
    retiring implementation this store must reproduce byte for byte — scans
    the same two directories with the same `rglob`. Records are filed under
    dated subdirectories (`archive/review-trail/2026-08-03/*.json`), so a
    non-recursive `glob` sees none of the 3351 archived ones and misses 92
    nested live ones besides. Both defects were caught by C2's whole-corpus
    differential, which read 3550 SHAs against this store's 1138.

    Returns `(records, parse_failures)` — `parse_failures` is the relative
    path of every file `coverage._parse_trail_file` could not parse at all
    (neither JSON nor JSONL), reported rather than silently dropped so
    `BackfillResult.parse_failures` (and the `_main` CLI's printed count)
    reflect real skips instead of always reading zero.
    """
    root = Path(repo_root)
    out: List[Tuple[str, dict]] = []
    parse_failures: List[str] = []
    for trail_dir in (root / "state" / "review-trail", root / "archive" / "review-trail"):
        if not trail_dir.is_dir():
            continue
        for path in sorted(trail_dir.rglob("*.json")):
            rel = path.relative_to(root).as_posix()
            try:
                records = coverage._parse_trail_file(str(path))
            except coverage._TrailParseError:
                parse_failures.append(rel)
                continue
            if len(records) == 1:
                out.append((rel, records[0]))
            else:
                for idx, rec in enumerate(records):
                    out.append((f"{rel}#{idx}", rec))
    return out, parse_failures


def run_backfill(repo_root: str) -> BackfillResult:
    """One-shot, idempotent, resumable pass resolving every not-yet-folded
    on-disk review-trail record into the reviewed_set store.

    Idempotent: a record id already in `reviewed_set.read_folded_record_ids`
    is skipped entirely — a second `run_backfill` call over an unchanged
    corpus folds nothing new. Resumable: an `UNRESOLVED` record from a
    prior interrupted/partial run is retried (its id was never marked
    folded), and a crash mid-call leaves exactly the same self-healing
    state `resolve_and_fold`'s write-ordering already guarantees per
    record batch.

    This is the SAME code path as write-time resolution
    (`resolve_and_fold`) — no separate migration-shaped fan-out. Not a
    gate-path cost: nothing calls this implicitly.
    """
    already_folded = _store.read_folded_record_ids(repo_root)
    all_records, parse_failures = _iter_trail_records(repo_root)
    pending = [
        (record_id, record)
        for record_id, record in all_records
        if record_id not in already_folded
    ]

    result = BackfillResult()
    result.parse_failures = parse_failures
    if not pending:
        return result

    dispositions = resolve_and_fold(repo_root, pending)
    for record_id, disposition in dispositions.items():
        if disposition == RecordDisposition.FOLDED:
            result.folded.append(record_id)
        elif disposition == RecordDisposition.EXCLUDED:
            result.excluded.append(record_id)
        else:
            result.unresolved.append(record_id)
    return result


def _main(argv: Optional[List[str]] = None) -> int:
    """Minimal operator CLI: `python -m coordinator_core.review_trail.backfill <repo_root>`.
    Prints a one-line summary and exits 0 always — this is a report, never
    a gate; an unresolved record is reported, not treated as a failure."""
    import sys

    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m coordinator_core.review_trail.backfill <repo_root>", file=sys.stderr)
        return 2
    repo_root = args[0]
    result = run_backfill(repo_root)
    print(
        json.dumps(
            {
                "folded": len(result.folded),
                "excluded": len(result.excluded),
                "unresolved": len(result.unresolved),
                "parse_failures": len(result.parse_failures),
            }
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI shim
    import sys

    raise SystemExit(_main())
