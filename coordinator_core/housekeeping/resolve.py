"""
coordinator_core.housekeeping.resolve — Step 2's hot path: "what state is
blocker id X in?", rebuilt from a 3,300 ms full-corpus double-scan down to a
dict lookup plus 1-2 fresh single-file reads.

Cite (BINDING): docs/research/2026-08-29-housekeeping-v2-target-shape.md § 2
"the resolver (the hot path)" pseudocode, and
docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md § C5. The
function this module replaces is
`coordinator_core.ops.handoff_transition._resolve_blocker_deployment_state`,
which `rglob`s BOTH `state/handoffs/` and `archive/handoffs/` on every call —
measured 3,300 ms, twice, inside a write lock. This module never re-scans
either root: it consumes C3's already-in-memory live corpus and C4's
already-built archive candidate index.

Three steps, in order:

  1. live corpus (C3's `LiveCorpusResult.records`, already in memory) —
     zero I/O. A `handoff_id` match here is trusted directly; the live
     corpus was itself a fresh read at this cycle's start (C3's own single
     read), and re-reading it a second time inside the SAME cycle buys
     nothing.
  2. archive candidate index (C4's `ArchiveIndex.lookup`) — zero I/O, a
     dict lookup narrowing ~1,470 files to this id's candidates.
  3. TRUTH from disk, fresh, right now, but ONLY for the archive candidates
     found in step 2 — typically 1-2 files, never the whole tree. This is
     contract 1's act-time re-read: it closes the shared-worktree race with
     ~50 peers (a stale index entry costs one wasted file read, never a
     wrong verdict) and guards a stale index (`matches_id` below drops a
     candidate the fresh read shows no longer carries this id).

Contract 2 (collapse before deciding): a `handoff_id` can name an entire
continuation CHAIN, not one record — a baton picked up N times leaves N
records (the live head, plus every archived predecessor) that all carry the
same id. Naive "first/last match wins" gets this wrong in either direction:
one prior resolver returned whichever record its walker appended last
(archived entries sort after live ones, so a superseded record beat the live
head); another saw multiple matches and refused outright (permanently
wedging a real, resolvable id). Both wrong. `collapse_to_chain_heads`
(`coordinator_core.reconcile.gate_eval`) is the SHARED primitive that fixes
this — the same one `handoff_transition.py::_resolve_blocker_deployment_state`
already uses — applied here to the union of the live match and the
act-time-re-read archive candidates, BEFORE any ambiguity decision.

Contract 3 (ambiguous, never a guess): after collapsing, more than one
DISTINCT surviving record is a genuine data defect (a real `handoff_id`
collision, not a chain), and this module fails loud with
`AMBIGUOUS_BLOCKER_SENTINEL` (`"<ambiguous-duplicate-id>"` — the existing
spelling, unchanged) rather than picking one.

Negative-spec: this module does not itself walk `state/handoffs/` or
`archive/handoffs/` — C3 and C4 own those scans. It does not decide what any
`deployment_state` value MEANS for gate-clearing (C6's job) — it only
resolves a blocker id to its terminal-relevant fields, or to `UNRESOLVED`/
`AMBIGUOUS`.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional

from coordinator_core.housekeeping.archive_index import ArchiveIndex
from coordinator_core.housekeeping.head_scan import scan_keys
from coordinator_core.reconcile.gate_eval import collapse_to_chain_heads

#: Sentinel returned as `deployment_state` when, after collapsing to chain
#: heads, more than one DISTINCT record still resolves the same id — a real
#: `handoff_id` collision, never a legitimate chain. Deliberately not a real
#: `deployment_state` value, so a caller's clearing logic falls through to
#: its catch-all non-clearing branch exactly like any other unresolvable
#: state. Existing spelling (contract 3): unchanged from
#: `coordinator_core.ops.handoff_transition._AMBIGUOUS_BLOCKER_SENTINEL`.
AMBIGUOUS_BLOCKER_SENTINEL = "<ambiguous-duplicate-id>"

#: Leg budget for one `resolve()` call, asserted independently (plan chunk
#: C5 body): "5 ms per gate clear" — a dict lookup plus 1-2 file head-scans,
#: not a corpus walk.
LEG_BUDGET_MS = 5.0

#: Frontmatter keys the act-time archive re-read needs. `handoff_id` guards
#: a stale index entry (contract 1); `deployment_state`/`closed_reason`/
#: `continued_into` are the terminal-relevant fields a caller needs to
#: decide whether a blocker clears; `predecessor`/`additional_predecessors`
#: feed `collapse_to_chain_heads`'s signal (3) — see that function's own
#: docstring in `coordinator_core.reconcile.gate_eval`.
_RESOLVE_READ_KEYS = (
    "handoff_id",
    # The id a blocker actually names (`blocked_by: [sat-06]`). Both legs
    # match on this, not on `handoff_id` -- 0 of the live corpus's
    # blocked_by values resolve to a handoff_id, every one to a stub_id.
    "stub_id",
    "deployment_state",
    "closed_reason",
    "continued_into",
    "predecessor",
    "additional_predecessors",
)

Reader = Callable[[Any, Iterable[str]], Dict[str, Any]]


class BlockerState(NamedTuple):
    """A blocker id's resolved terminal-relevant fields, as of one
    act-time archive re-read (or the in-memory live corpus, unre-read).

    `deployment_state` is `None` when no record resolves the id at all
    (`resolved=False` — a dangling reference, the id is the defect), or
    `AMBIGUOUS_BLOCKER_SENTINEL` when more than one DISTINCT record does
    (contract 3, `resolved=True` — a record WAS matched, just not uniquely).
    """

    deployment_state: Optional[str]
    closed_reason: Optional[str]
    continued_into: Optional[str]
    resolved: bool = True


#: Returned when no record — live or archived — resolves the requested id.
UNRESOLVED_BLOCKER_STATE = BlockerState(
    deployment_state=None, closed_reason=None, continued_into=None, resolved=False
)

#: Returned when, after collapsing to chain heads, more than one distinct
#: record still resolves the same id (contract 3).
AMBIGUOUS_BLOCKER_STATE = BlockerState(
    deployment_state=AMBIGUOUS_BLOCKER_SENTINEL,
    closed_reason=None,
    continued_into=None,
    resolved=True,
)


def _path_str(path: Any) -> str:
    return str(path)


def _live_matches(
    live_records: Dict[Any, Dict[str, Any]], blocker_id: str
) -> List[Dict[str, Any]]:
    """Every live-corpus record whose `stub_id` equals `blocker_id` —
    zero I/O, read straight out of C3's already-in-memory dict. Normally
    zero or one match; a chain's live HEAD is at most one record (a
    superseded predecessor left resident in `state/handoffs/` would itself
    be a data defect this function has no way to detect without re-reading,
    which contract 2 does not ask it to do for the live leg)."""
    matches: List[Dict[str, Any]] = []
    for path, fields in live_records.items():
        if fields.get("stub_id") != blocker_id:
            continue
        rec = dict(fields)
        rec["_path"] = _path_str(path)
        matches.append(rec)
    return matches


def _archive_matches(
    archive_index: ArchiveIndex,
    blocker_id: str,
    reader: Reader,
) -> List[Dict[str, Any]]:
    """The act-time re-read leg (contract 1): narrow via the archive index
    (zero I/O), then re-read EACH candidate fresh from disk right now — never
    trusting the index's own `stub_id` mapping as truth. A candidate whose
    fresh read no longer carries `blocker_id` (stale index entry — the file
    changed since the index last saw it) is dropped rather than returned."""
    matches: List[Dict[str, Any]] = []
    for path in archive_index.lookup(blocker_id):
        fields = reader(path, _RESOLVE_READ_KEYS)
        if fields.get("stub_id") != blocker_id:
            continue  # stale index entry — guarded, never trusted (contract 1)
        rec = dict(fields)
        rec["_path"] = _path_str(path)
        matches.append(rec)
    return matches


def resolve_blocker_id(
    blocker_id: str,
    live_records: Dict[Any, Dict[str, Any]],
    archive_index: ArchiveIndex,
    *,
    reader: Reader = scan_keys,
) -> BlockerState:
    """Resolve one blocker id's terminal-relevant state — the hot-path
    function this module exists to rebuild. See module docstring for the
    three-step shape and the two contracts (collapse-then-decide,
    ambiguous-never-a-guess) this implements.

    `live_records` is C3's `LiveCorpusResult.records` (or any dict of the
    same path -> fields shape). `archive_index` is C4's `ArchiveIndex`.
    `reader` defaults to the declining head-scan (`scan_keys`) — a test may
    substitute a stub to assert the act-time-re-read call count without
    real file I/O.
    """
    candidates = _live_matches(live_records, blocker_id) + _archive_matches(
        archive_index, blocker_id, reader
    )
    if not candidates:
        return UNRESOLVED_BLOCKER_STATE

    heads = collapse_to_chain_heads(candidates)
    if len(heads) > 1:
        return AMBIGUOUS_BLOCKER_STATE

    head = heads[0]
    return BlockerState(
        deployment_state=head.get("deployment_state"),
        closed_reason=head.get("closed_reason"),
        continued_into=head.get("continued_into"),
        resolved=True,
    )


def make_resolver(
    live_records: Dict[Any, Dict[str, Any]],
    archive_index: ArchiveIndex,
    *,
    reader: Reader = scan_keys,
) -> Callable[[str], BlockerState]:
    """Bind `live_records`/`archive_index` once per cycle and return a
    closure over `resolve_blocker_id` — the shape C6's gate evaluation calls
    once per `awaiting_gate` record's blocker, mirroring the target-shape
    doc's `make_resolver(live, archive_idx)` pseudocode."""

    def resolve(blocker_id: str) -> BlockerState:
        return resolve_blocker_id(blocker_id, live_records, archive_index, reader=reader)

    return resolve
