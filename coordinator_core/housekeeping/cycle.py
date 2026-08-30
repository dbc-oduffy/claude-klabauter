"""
coordinator_core.housekeeping.cycle — Step E (one move + ONE commit) and the
assembled `run(...)` entry point the falsifier targets as
`module:coordinator_core.housekeeping.cycle:run`.

Cite (BINDING): docs/research/2026-08-29-housekeeping-v2-target-shape.md § 2
pseudocode step E and the whole-cycle `housekeeping(repo, cap)` wrapper;
docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.md § C6c;
DR-384-the-housekeeping-cycle-archives-through.md (ratified 2026-08-29 —
this module composes `ops/fleet/_common.py :: archive_and_commit`, an
existing DR-211-D1-sanctioned primitive, and thereby EXTENDS DR-211 rather
than falling under it — same reading DR-270 gave `queue.close`).

Step E lands the terminal set (C6b's `compute_terminal_set` output) through
the EXISTING archival seam — `ops/fleet/_common.py :: archive_and_commit` —
authoring NO new commit route. That function os.replaces each `Move`
in-process and lands the whole batch as ONE commit via
`ops.ceremony.git_native._commit_via_head_spine` under a locked `cas_ref`
compare-and-swap; see its own docstring for the full mechanism (DR-211
D3/D4 compliance, the HEAD-race CAS, dst-exists / untracked-at-head
refusals, 100755 mode preservation). This module does not re-derive any of
that.

Three routes considered and rejected before landing on `archive_and_commit`
(so nobody re-derives them):
  - a private `GIT_INDEX_FILE` dance (read-tree/write-tree/commit-tree/
    update-ref) — RETIRED 2026-08-26; `_common.py`'s own docstring says the
    dance "is GONE", and a variant seeded by copying `.git/index` is not
    merely retired but INCORRECT (a whole-repo tree copy silently reverts
    any peer commit landed since the copy; an update-ref CAS does not catch
    it because the parent is right and the tree is wrong).
  - porcelain `git add -- <paths>` + `git commit -- <paths>` — correct and
    DR-211 D3-compliant, but measured 375ms / 2 spawns. Strictly worse than
    the seam.
  - removing the commit from the cycle entirely — forbidden by DR-211 D5.

Failure-surface disposition (D5 forbids a half state — complete-or-restore,
never leave-and-log):

  - `os.replace` fails partway through a batch (some moved, some not):
    handled INSIDE `archive_and_commit` — every acted move is reversed via
    `Path.rename` back to src and reclassified into `failed[]` before that
    function returns. This module never sees a partially-applied batch; it
    only ever sees a clean `(acted, failed)` pair.
  - the seam's `cas_ref` refuses because a peer moved HEAD: also handled
    INSIDE `archive_and_commit` — every acted item (os.replace already
    landed on disk) is reversed and reclassified to `failed[]` before
    return, same as the write-fails-partway case above.
  - a move is refused `dst-exists` or `untracked-at-head`: the move was
    never applied (os.replace was never called for it), so there is
    nothing to restore — the item lands directly in `failed[]`.
  This module's own contribution to that discipline is simply NOT to
  second-guess `archive_and_commit`'s `(acted, failed)` split: `acted`
  items are reported archived, `failed` items are reported failed, and
  nothing here retries, patches, or partially applies either list.

After a successful batch, the archive candidate index is warmed by
`archive_index.revalidate` (C4's cheap, spawn-free `os.scandir`+stat leg) —
the moved records now exist on disk at their `dst` paths, and revalidate
picks up exactly that changed subset. Mirrors the pseudocode's
`archive_idx.add(terminal)` without a new `ArchiveIndex.add` method:
`revalidate` is the sanctioned, already-tested way this index absorbs a
disk change, spawn-free by its own budget (5ms leg, C4).

`run(...)` assembles the whole cycle — steps A (`corpus.read_live_corpus`),
B (`archive_index.build_index`), C (`gate_clear.evaluate_gate_clear` /
`apply_gate_clear`, resolved via `resolve.make_resolver`), D
(`terminal.compute_terminal_set`), and E (this module's own
`archive_terminal_batch`) — in one synchronous call, matching the
falsifier's `fn(repo_root: str, cap: int) -> dict` contract
(docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.falsifier.py
:: `measure_via_module`).

Negative-spec: this module does not walk `state/handoffs/` or
`archive/handoffs/` itself (C3/C4's job), does not decide gate-clear
semantics (C6's job) or the terminal-set membership rules (C6b's job), and
never opens a second commit route beyond `archive_and_commit` — a variant
that stages via `git add`/a private index, or that commits per-item instead
of once per batch, is the regression this module's own test suite exists
to catch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from coordinator_core.claim_state import handoff_claim_dir
from coordinator_core.housekeeping import archive_index as archive_index_mod
from coordinator_core.housekeeping.archive_index import ArchiveIndex
from coordinator_core.housekeeping.corpus import read_live_corpus
from coordinator_core.housekeeping.gate_clear import (
    CONFLICT,
    apply_gate_clear,
    evaluate_gate_clear,
    record_after_clear,
)
from coordinator_core.housekeeping.resolve import make_resolver
from coordinator_core.housekeeping.terminal import (
    TERMINAL_DEPLOYMENT_STATES,
    TerminalEntry,
    compute_terminal_set,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.liveness import cs_claim_holder_live, resolve_live_session_ids
from coordinator_core.ops.fleet import archive_actioned_memos
from coordinator_core.ops.fleet._common import Move, archive_and_commit, handoff_archive_dest
from coordinator_core.ops.fleet.archive_terminal_handoffs import _dirty_handoff_relpaths

PathLike = Union[str, Path]

#: The `deployment_state` value C6's gate-clear machinery acts on — mirrors
#: `gate_clear._AWAITING_GATE` (not re-imported: that name is module-private
#: there, and this module's own scope is which records to OFFER to
#: `evaluate_gate_clear`, not gate-clear's own internal vocabulary).
_AWAITING_GATE = "awaiting_gate"


def _record_claimant(record: Dict[str, Any]) -> str:
    """The session id a record names as holding it, or `""`.

    `claimed_by` wins over the retired `consumed_by`, mirroring
    `coordinator_core.coverage :: _parse_handoff_consumed_by`'s own
    dual-tolerant precedence rather than inventing a second rule. Reading
    only `consumed_by` reads a name no live record carries -- 0 of 298 at
    the time this was written, against 30 carrying `claimed_by` -- which is
    a rail that cannot fire and a unit test that passes because its fixture
    was authored from the same wrong field.
    """
    for key in ("claimed_by", "consumed_by"):
        value = record.get(key)
        if value:
            return str(value).strip()
    return ""


def _transition_target_rel(worktree_root: Path, transition_params: Any) -> Set[str]:
    """The repo-relative POSIX path of a targeted transition's own handoff, as
    a set for `run(exclude=...)`. Empty when no transition ran, or when the
    named path does not sit under this worktree -- an unresolvable name
    excludes nothing rather than silently excluding everything.
    """
    if not isinstance(transition_params, dict):
        return set()
    named = transition_params.get("handoff_path")
    if not named:
        return set()
    # `handoff_path` arrives REPO-RELATIVE from the real callers
    # (`baton_assemble/apply.py` builds `repo_root / predecessor_path` from
    # the same string). Resolving it bare would resolve against the process
    # CWD, so on any caller whose CWD is not the worktree root the exclusion
    # would silently match nothing -- a fail-OPEN on the one rail whose whole
    # job is to stop the sweep touching another leg's handoff.
    candidate = Path(named)
    root = worktree_root.resolve()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return {candidate.resolve().relative_to(root).as_posix()}
    except (ValueError, OSError):
        return set()


def _claim_holder_live_predicate(common_dir: Path):
    """Build the `claim_holder_live(path, record) -> bool` predicate
    `compute_terminal_set` (C6b) expects, closing over `common_dir` so each
    call only needs the per-record `path`.

    Delegates entirely to `coordinator_core.liveness.cs_claim_holder_live`
    (session-registry-backed, DR5 single-liveness-key) via the shared
    `handoff_claim_dir` convention (`coordinator_core.claim_state`) —
    mirrors `archive_terminal_handoffs.py`'s own Check 4. Fails CLOSED to
    "not live" (never retains) on an unexpected liveness-check error: a
    stuck claim dir the liveness check cannot resolve should not silently
    wedge every terminal record behind it forever, and this module's own
    caller (C7's brightline test) never exercises a genuinely broken
    liveness backend — the alternative (fail OPEN to "live", i.e. retain)
    would convert an unrelated liveness-check bug into a routine archival
    stall, on a check that is already best-effort by its own contract.
    """

    def predicate(path: Path, record: Dict[str, Any]) -> bool:
        claim_dir = handoff_claim_dir(common_dir, path)
        try:
            return cs_claim_holder_live(str(claim_dir))
        except OSError:
            return False

    return predicate


def archive_terminal_batch(
    worktree_root: Path,
    moves: List[Move],
    subject: str,
) -> Tuple[List[dict], List[dict]]:
    """Step E: one `os.replace` per Move, landed as ONE commit via the
    existing `archive_and_commit` seam (module docstring — no new commit
    route, no second-guessing of its `(acted, failed)` split).

    Widened (2026-08-30, the actioned-memo class gets an occasion, C2) from
    `List[TerminalEntry]` to a prebuilt `List[Move]` — `housekeeping.terminal`
    type memos are not, and `plan_sweep` (the memo family's own planner)
    already returns ready-built Moves. `run()` builds the handoff family's
    Moves itself (mirroring what this function used to do internally) and
    concatenates them with the memo family's Moves before calling here once,
    so both families land in the SAME commit.

    Returns `(acted, failed)` — `archive_and_commit`'s own return shape,
    passed through unchanged. An empty `moves` short-circuits to `([], [])`
    without calling into the seam at all (nothing to move, nothing to
    commit — a zero-length batch is not a degenerate call to make, it is
    simply not a call).
    """
    if not moves:
        return [], []

    return asyncio.run(archive_and_commit(worktree_root, moves, subject))


def run(
    repo_root: PathLike,
    cap: int,
    *,
    close: bool = True,
    exclude: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """The assembled cycle — steps A through E in one synchronous call.

    Matches the falsifier's `fn(repo_root: str, cap: int) -> dict` contract
    (`docs/plans/2026-08-29-the-housekeeping-cycle-stops-committing.
    falsifier.py :: measure_via_module`), reached as
    `module:coordinator_core.housekeeping.cycle:run`.

    Returns a JSON-serializable result dict: `closed` (int, gates cleared
    this cycle), `conflicts` (list of str paths whose gate-clear lost a
    race — CONFLICT, never a silent overwrite), `archived` (list of
    HANDOFF candidate ids `archive_and_commit` reports `acted` on), `failed`
    (list of `{id, reason}` dicts for any HANDOFF terminal entry
    `archive_and_commit` could not land), `live_read_count` (C3's own
    read-count, asserted read-once by C7), and `scan_gaps` (C3's own
    directory-listing gaps, preserved rather than folded into an empty
    result).

    `memos_archived` / `memos_failed` / `memos_skipped` (2026-08-30, the
    actioned-memo class gets an occasion, C2) — the MEMO family's own
    sibling keys, folded into this same cycle and the SAME commit as the
    handoff family, but kept out of `archived`/`failed` so those two stay
    BYTE-COMPATIBLE for existing consumers (`baton_assemble/apply.py`, the
    ceremony spines) that read handoffs only. `memos_skipped` is
    `archive_actioned_memos.plan_sweep`'s own returned skip semantics
    (scan-time rail refusals plus plan-time dest-conflict/deferred-cap),
    never swallowed.

    `exclude` — repo-relative POSIX paths this sweep must not touch, because
    something else in the same call already owns them. `_handler` passes the
    targeted transition's own handoff. Without it the population sweep can
    COMPLETE a move the transition failed to make: the transition's
    git-mv-failure branch is deliberately non-fatal and returns exit_code 0,
    so the call does not stop, and the sweep then archives the same record
    through its own seam. `cs_chain_archive_handoff` verifies a move by
    source-gone AND destination-present, which cannot tell whose move it was,
    so a failed archival reads as a successful one.
    """
    root = Path(repo_root)
    common_dir = git_common_dir(root)
    worktree_root = main_worktree_root(common_dir)
    live_dir = worktree_root / "state" / "handoffs"
    archive_dir = worktree_root / "archive" / "handoffs"

    # -- A. ONE read of the live corpus. -------------------------------
    live_result = read_live_corpus(live_dir)
    records = live_result.records

    # -- B. Candidate index over the archive. --------------------------
    # From cache where one is usable, rebuilt where it is not. A full build
    # costs 171.9ms at 1,470 records and is linear in the archive, which this
    # plan's Anti-scope forbids per cycle; a revalidated cache costs ~2ms.
    # `rebuilt` is reported out for the brightline gate to assert on -- it is
    # never branched on for correctness, since a cache miss and a cache hit
    # must produce the same verdicts.
    # A repo that has never archived a handoff has no `archive/handoffs/` at
    # all -- an EMPTY archive, not a scan failure. `build_index`'s default
    # `onerror` re-raises (deliberately: a vanished SUBdirectory mid-walk is a
    # gap worth failing on), so calling through it on an absent root turns the
    # first ship/chain/supersede in a freshly onboarded repo into an uncaught
    # FileNotFoundError out of `cs_ship_handoff` -- a traceback where the
    # contract is an error dict, and a handoff that never gets stamped.
    # Persistence is skipped on the same branch: an empty index cached against
    # a directory that does not exist yet only costs the next cycle a rebuild.
    cache_path = archive_index_mod.cache_path_for(common_dir)
    archive_dir_exists = archive_dir.is_dir()
    archive_idx: ArchiveIndex
    if archive_dir_exists:
        archive_idx, index_rebuilt = archive_index_mod.open_index(
            archive_dir, cache_path
        )
    else:
        archive_idx, index_rebuilt = ArchiveIndex(archive_dir=archive_dir), False

    # -- C. Close finished handoffs, under the lock, one file at a time. --
    resolver = make_resolver(records, archive_idx)
    gated = [
        (path, record)
        for path, record in records.items()
        if record.get("deployment_state") == _AWAITING_GATE
    ]

    closed = 0
    conflicts: List[str] = []
    # `close=False` is for a caller that has already closed records itself and
    # wants the sweep alone -- the gated list is still computed above so the
    # result shape does not change shape with the flag.
    # A failed close pass must not eat the sweep -- the two are different
    # kinds of failure, and the archival job is still worth doing when gate
    # evaluation dies. It must not VANISH either: without `close_error` a
    # caller cannot tell "nothing needed closing" from "the close pass died",
    # and both render as closed=0. That pair of properties was asserted by
    # `ops/tests/test_handoff_housekeeping.py`'s fusion-contract tests, which
    # were deleted with their module before this module carried them.
    close_error: Optional[str] = None
    try:
        for path, record in (gated if close else []):
            verdict = evaluate_gate_clear(record, resolver)
            if not verdict.clears:
                continue
            result = apply_gate_clear(path, worktree_root)
            if result.status == CONFLICT:
                conflicts.append(str(path))
                continue
            records[path] = record_after_clear(record)
            closed += 1
    except Exception as exc:  # noqa: BLE001 -- the sweep survives a close failure
        close_error = f"{type(exc).__name__}: {exc}"

    # -- D. Terminal set, computed from step A + this cycle's own mutations. --
    # The worktree-dirty rail is asked ONCE, over the terminal candidates only
    # -- never the whole corpus. `_dirty_handoff_relpaths` answers from a
    # scoped in-process index walk and falls back to a single scoped `git
    # status --porcelain` only when that arm declines, so the normal path adds
    # no spawn to the cycle's budget. It fails CLOSED: a git failure retains
    # every candidate rather than sweeping them.
    claim_holder_live = _claim_holder_live_predicate(common_dir)
    excluded = exclude or frozenset()
    candidate_rels = sorted({
        path.relative_to(worktree_root).as_posix()
        for path, record in records.items()
        if record.get("deployment_state") in TERMINAL_DEPLOYMENT_STATES
    })

    # -- Memo family (C2, the actioned-memo class gets an occasion). --
    # `archive_actioned_memos.plan_sweep` owns its own scan/classify/cap-slot
    # machinery entirely -- this module never re-derives it. The ONE thing
    # folding the memo family into this cycle needs from HERE is its own
    # candidate relpaths, unioned into the SINGLE dirty-check call below, so
    # the memo family never triggers a second `git status` spawn.
    try:
        memo_candidate_paths = archive_actioned_memos.collect_inbox_memo_paths(worktree_root)
    except OSError:
        memo_candidate_paths = []
    memo_candidate_rels = sorted({
        p.relative_to(worktree_root).as_posix() for p in memo_candidate_paths
    })

    dirty_rels = _dirty_handoff_relpaths(
        worktree_root,
        sorted(set(candidate_rels) | set(memo_candidate_rels)),
        fallback_pathspecs=("state/handoffs", "cross-repo/inbox"),
    )
    # Read off the record step A already parsed -- the rail costs no I/O
    # here, where the predecessor sweep paid a per-candidate file read.
    # `claimed_by` is the live field; `consumed_by` is its retired spelling,
    # tolerated at lower precedence exactly as `coverage.py ::
    # _parse_handoff_consumed_by` does. `resolve_live_session_ids` is asked
    # once, and only when some terminal candidate actually names a session.
    live_sids = (
        resolve_live_session_ids()
        if any(
            _record_claimant(record)
            for record in records.values()
            if record.get("deployment_state") in TERMINAL_DEPLOYMENT_STATES
        )
        else frozenset()
    )

    def _retained(path: Path, record: Dict[str, Any]) -> bool:
        """Every ground on which a terminal record is NOT this sweep's to
        file, in one predicate. A live claim holder is one such ground, not
        a category of its own -- it had a second parameter of identical
        shape on `compute_terminal_set` until 2026-08-30."""
        rel = path.relative_to(worktree_root).as_posix()
        if rel in excluded or rel in dirty_rels:
            return True
        if claim_holder_live(path, record):
            return True
        claimant = _record_claimant(record)
        return bool(claimant) and claimant in live_sids

    terminal_entries = compute_terminal_set(records, cap, retained=_retained)

    # -- E. One move + ONE commit, across BOTH families. -----------------
    # Handoff Moves, built exactly as `archive_terminal_batch` used to build
    # them internally before it was widened to accept a prebuilt list (C2).
    handoff_moves = [
        Move(
            src=entry.path,
            dst=handoff_archive_dest(worktree_root, entry.path),
            candidate_id=str(entry.path.relative_to(worktree_root)),
            force=False,
            restage_src=False,
        )
        for entry in terminal_entries
    ]

    # Memo Moves, from the memo op's OWN planner -- `cap` passed straight
    # through, unmodified, exactly as it is already passed to
    # `compute_terminal_set` for the handoff family above (no shared-cap-
    # over-the-union re-derivation here). `known_dirty_relpaths=dirty_rels`
    # answers the memo family's Rail 1 from the single union dirty-check
    # already computed above, spawning nothing extra.
    memos_skipped: List[dict] = []
    memo_moves, memo_plan_skipped = archive_actioned_memos.plan_sweep(
        worktree_root, common_dir, cap,
        known_dirty_relpaths=dirty_rels,
        scan_skipped=memos_skipped,
        # Hand the walk we already did above straight through -- without this
        # `_scan_terminal_memos` re-walks cross-repo/inbox and re-`resolve()`s
        # every entry, a second full directory pass per cycle for a list this
        # caller is already holding.
        inbox_paths=memo_candidate_paths,
    )
    # `plan_sweep` returns its OWN plan-time skips (dest-conflict,
    # deferred-cap) separately from the scan-time `scan_skipped` out-param --
    # both are `plan_sweep`'s own returned skip semantics (RESULT SHAPE,
    # C2), so `memos_skipped` reports the union rather than only the scan
    # half.
    memos_skipped.extend(memo_plan_skipped)

    subject_parts = []
    if terminal_entries:
        subject_parts.append(f"{len(terminal_entries)} terminal handoff(s)")
    if memo_moves:
        subject_parts.append(f"{len(memo_moves)} actioned memo(s)")
    subject = "housekeeping: archive " + " and ".join(subject_parts) if subject_parts else \
        "housekeeping: archive 0 terminal handoff(s)"

    acted, failed = archive_terminal_batch(
        worktree_root, handoff_moves + memo_moves, subject,
    )

    handoff_move_ids = {m.candidate_id for m in handoff_moves}
    memo_move_ids = {m.candidate_id for m in memo_moves}
    memos_archived = [item["id"] for item in acted if item["id"] in memo_move_ids]
    memos_failed = [item for item in failed if item["id"] in memo_move_ids]
    archived = [item["id"] for item in acted if item["id"] in handoff_move_ids]
    failed = [item for item in failed if item["id"] in handoff_move_ids]

    index_changed = False
    if acted:
        index_changed = bool(archive_index_mod.revalidate(archive_idx))

    # Persist for the next cycle, ONLY when the on-disk cache would differ.
    # Best-effort by construction: a cache that cannot be written costs the
    # next cycle a rebuild, nothing else.
    #
    # The write is gated because it is NOT free and it was previously
    # unconditional: `save_index` serialises the whole index to JSON, measured
    # at 94ms / 16232 `_iterencode` calls over a 1,470-record archive by
    # cProfile on process_time (2026-08-30). A cycle that archived nothing
    # rewrote byte-identical content every run, which at backlog scale was the
    # difference between the two-family cycle sitting inside
    # CYCLE_PROCESS_TIME_BUDGET_MS and breaching it. Two cases genuinely need
    # the write: a rebuild (there was no usable cache, or it was stale), and a
    # revalidate that actually patched the index. Neither holds on a quiet
    # cycle, which is the common case on a cadence job.
    index_cache_written = (
        archive_index_mod.save_index(archive_idx, cache_path)
        if archive_dir_exists and (index_rebuilt or index_changed)
        else False
    )

    return {
        "closed": closed,
        "close_error": close_error,
        "conflicts": conflicts,
        "archived": archived,
        "failed": failed,
        "memos_archived": memos_archived,
        "memos_failed": memos_failed,
        "memos_skipped": memos_skipped,
        "live_read_count": live_result.read_count,
        "scan_gaps": live_result.scan_gaps,
        "index_rebuilt": index_rebuilt,
        "index_cache_written": index_cache_written,
    }


# ---------------------------------------------------------------------------
# The op boundary — `housekeeping.cycle`
# ---------------------------------------------------------------------------
#
# DR-384 admits `housekeeping.cycle` to DR-211 § D1's sanctioned-writer list.
# That decision was ratified against an op that did not exist: `run()` above is
# a module function, and nothing in this package called `register_op`. The
# consequence is not cosmetic -- `handoff.housekeeping`'s key is carried in
# `authz/classification.py` (as MUTATING) and referenced by the bash guards, and
# a caller reaches it through the registry, not by import. A replacement that
# never registers cannot be repointed onto, only imported around.
#
# `run()` also does not accept what two of its three real callers pass:
#   - `close=False`, for a caller that has already closed records itself and
#     wants the sweep alone.
#   - `transition`, a targeted transition on ONE named handoff, which
#     `baton_assemble/apply.py`'s d6 needs because it must stamp
#     `continued`/`continued_into` on a predecessor whose successor was minted
#     seconds ago by the same run -- a fact no population scan can derive.
#
# So the op boundary lives here, with the same parameter contract, and delegates
# the transition leg to `handoff_archive_transition` exactly as before (that
# module is NOT part of this plan's deletion set and stays where it is).

OP_KEY = "housekeeping.cycle"


def _setup_error(message: str) -> Dict[str, Any]:
    return {"exit_code": 1, "error": message}


def _handler(params: Dict[str, Any], repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """`housekeeping.cycle` — clear finished gates, file terminal handoffs.

    Params:
        cap (int, REQUIRED)   positive move cap; absent or non-positive is a
                              setup error, never an unbounded default.
        close (bool)          run the gate-clear pass. Default True. False
                              sweeps and archives only.
        transition (dict)     OPTIONAL targeted transition on ONE named
                              handoff, passed verbatim to
                              `handoff_archive_transition._handler`. Its
                              result comes back under `transition`, unmodified.

    A FAILED TRANSITION STOPS THE CALL; a failed close pass does not. They are
    different kinds of failure: the close pass is a population sweep whose
    failure leaves the archival job still worth doing, while a transition is
    one named handoff, and sweeping past it would report a green run over a
    succession that never landed.
    """
    if repo_root is None:
        return _setup_error("repo_root handler arg is None")

    common_dir = repo_root if isinstance(repo_root, Path) else Path(repo_root)

    cap = params.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        return _setup_error(
            f"cap is required and must be a positive int, got {cap!r} -- no "
            f"unbounded default (mirrors fleet.archive_completed_handoffs's "
            f"own cap-axis decision)"
        )

    transition_result = None
    transition_params = params.get("transition")
    if transition_params:
        import asyncio

        from coordinator_core.ops.handoff_archive_transition import (
            _handler as _transition_handler,
        )

        try:
            transition_result = asyncio.run(
                _transition_handler(dict(transition_params), common_dir)
            )
        except Exception as exc:  # noqa: BLE001 -- a raising leg is a failed
            # transition, not a traceback out of an op whose contract is an
            # error dict. The old library call was reached through a caller
            # that caught; reaching it through the op boundary is not licence
            # to drop that.
            return {
                "exit_code": 1,
                "error": f"transition raised: {type(exc).__name__}: {exc}",
                "transition": None,
            }
        if transition_result.get("exit_code") != 0:
            # Stop, do not sweep on: see the fail-posture note above.
            return {
                "exit_code": transition_result.get("exit_code", 1),
                "error": transition_result.get("error", "transition failed"),
                "transition": transition_result,
            }

    worktree_root = main_worktree_root(common_dir)
    result = run(
        worktree_root,
        cap,
        close=bool(params.get("close", True)),
        exclude=_transition_target_rel(worktree_root, transition_params),
    )
    result["exit_code"] = 0
    if transition_result is not None:
        result["transition"] = transition_result
    return result


register_op(OP_KEY, _handler)
