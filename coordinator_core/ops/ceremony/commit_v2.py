"""
coordinator_core.ops.ceremony.commit_v2 -- JSON-RPC "ceremony.commit_v2" op.

Purpose: a fresh dispatchable identity over the 694 measured, zero-spawn lines
in `coordinator_core/git/commit.py` (`commit_paths`) and
`coordinator_core/git/index_write.py` (`splice_index`, called internally by
`commit_paths`). `ceremony.commit` is DEAD (killed at p50 421.9ms process
time against a 200ms bar -- `coordinator_core/op_budget_suspension.py`) and
ITS NAME STAYS DEAD: this op is a fresh identity, ONCE, so every guard, budget
row and test fixture keyed to the old name is unambiguous about which op it
describes. Do not resurrect "ceremony.commit" for this handler or any other.

Handler shape: a thin envelope, not a wrapper. It calls `commit.commit_paths`
directly and returns its outcome. It does NOT import `run_commit_pipeline`,
and does NOT import anything from `commit_pipeline.py` or `git_native.py` --
those two modules are the 11,015-line surface this op exists to make
un-necessary, and importing from them here would make the v2 a second path
into the same pipeline rather than a replacement for it
(docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md, C3 body).

Keying scope: common_dir (`coordinator_core/op_scopes.py`) -- commits within
the CALLER's own working tree/index, mirroring `commit.exec_bit_change` and
`commit.anchors`'s precedent: the handler receives repo_root = git common dir
and derives the caller's worktree via `main_worktree_root(repo_root)`.
`params.repo_root` is the optional D3 consistency assertion only
(`check_repo_root`), never the worktree-resolution source.

Scope of THIS row (C4): the handler supplies `blob_fallback` --
`commit.hash_worktree_blobs_via_spawn`, restated small in `git/commit.py`
rather than imported from `git_native.py`'s `_hash_worktree_blobs` (barred by
the negative-spec below) -- so an `eol=crlf`-pinned path carrying CR bytes,
refused by `commit_paths`' own in-process check, lands via ONE batched
`git hash-object -w --stdin-paths` spawn per commit rather than propagating
as a structured `FilterUnsupported` error. A refusal `commit_paths` cannot
resolve even with the fallback (the fallback itself fails, or returns no sha
for a path) still propagates as a structured error, unmodified in substance
-- this handler does not catch, retry, or widen it.

Spec backlinks:
    docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md § C3
    coordinator_core/git/commit.py :: commit_paths
    coordinator_core/git/index_write.py :: splice_index (called internally)

Negative-spec (hard-won, restated for this row):
  - Does NOT import `coordinator_core.ops.ceremony.commit_pipeline` or
    `coordinator_core.ops.ceremony.git_native` in any form -- not
    `run_commit_pipeline`, not a helper, not a type. If the handler needs
    something those modules have, it is restated in `git/`, small, or it does
    not come (C3 body, verbatim).
  - Does NOT use the name "ceremony.commit" anywhere -- registry key,
    docstring, error message, or test fixture. That identity is dead and
    stays dead.
  - Does NOT catch `CommitRefused`/`FilterUnsupported` and retry, guess, or
    widen scope -- a refusal from `commit_paths` is returned as a structured
    error, unmodified in substance, so the caller sees exactly why nothing
    was written.
  - Does NOT use `params.repo_root` as the worktree-resolution source (D3:
    socket-authoritative common_dir only).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, cast

from coordinator_core.git.commit import (
    CommitOutcome,
    CommitRefused,
    NothingToCommit,
    FilterUnsupported,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import apply_missing_trailers
from coordinator_core.git.index_write import IndexStaleAfterCommit
from coordinator_core.git.eol_declared import (
    find_declared_eol_drift,
    repair_declared_eol_drift,
)
from functools import partial
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import _read_object
from coordinator_core.git.git_state import read_tree_spine
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.commit_gates import (
    carry_gate,
    declared_deletion_gate,
    op_scope_coverage_gate,
)
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.write_guards.guard_class_relay import (
    detect_class_transition,
    stage_class_transition_memo,
)
import logging

_LOG = logging.getLogger(__name__)

#: Prefix filter for the guard-class-relay step below (C2 of
#: docs/plans/2026-08-29-a-guard-class-flip-announces-itself.md). ONLY paths
#: under this directory can carry a `write_guards` CLASS constant -- this
#: string compare is the zero-cost gate the whole step's budget rests on
#: (0.156 us / 0 spawns measured, docs/research/spike-verdicts/2026-08-29-
#: guard-class-relay-commit-seam.md Q4): no path under it means the step
#: below returns having done no work -- no git call, no object read, no AST
#: parse, no import beyond what module load already paid.
_GUARD_MODULE_DIR = "coordinator_core/write_guards/"


def _guard_module_paths(paths: list, deleted_paths: list) -> list:
    """Guard-module `.py` paths present in either `paths` or `deleted_paths`,
    de-duplicated, order-preserving. A path under `_GUARD_MODULE_DIR` that is
    not `.py` (e.g. a stray non-source file) is not a guard module and is
    excluded -- `detect_class_transition` only has an opinion about source.
    """
    seen: dict = {}
    for p in list(paths) + list(deleted_paths):
        if p.startswith(_GUARD_MODULE_DIR) and p.endswith(".py"):
            seen.setdefault(p, None)
    return list(seen)


def _blob_source(
    spine: Optional[dict], common_dir: Path, path: str
) -> Optional[str]:
    """The decoded text of `path`'s blob per `spine` (a `read_tree_spine`
    result), or `None` when the path is absent from the spine (added/
    deleted wholesale), the blob is unreadable, or it is not valid UTF-8 --
    all three read as "no source to compare", matching
    `detect_class_transition`'s own "missing source is not a transition"
    posture. Never raises.
    """
    if spine is None:
        return None
    head_dir, _, head_name = path.rpartition("/")
    entry = spine.get(head_dir, {}).get(head_name)
    if entry is None:
        return None
    result = _read_object(common_dir, entry[1])
    if result is None:
        return None
    _otype, payload = result
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _pre_commit_guard_sources(worktree_root: Path, guard_paths: list) -> dict:
    """`{path: source_or_None}` for `guard_paths` as they stand at HEAD
    BEFORE `commit_paths` runs -- called from `_handler` prior to the
    `commit_paths` call, while HEAD still points at the pre-commit tree.
    Once `commit_paths` lands the commit, HEAD moves and `read_tree_spine`
    (HEAD-relative, no explicit-root parameter) can no longer see this
    state -- capturing it here is the only in-process route, and it is the
    SAME `read_tree_spine` call `commit_paths` itself makes at
    `commit.py:448` over (a superset of) these same paths, so the tree
    objects along this spine are already warm in `git_objects._OBJECT_CACHE`
    by the time `commit_paths` re-walks them a moment later (docs/research/
    spike-verdicts/2026-08-29-guard-class-relay-commit-seam.md Q4).

    Returns `{}` when `guard_paths` is empty (no read at all) or when
    `read_tree_spine` itself returns `None` (unreadable/corrupt HEAD tree --
    every path then reads as "no pre-commit source", never raises).
    """
    if not guard_paths:
        return {}
    common_dir = resolve_git_common_dir(worktree_root)
    spine = read_tree_spine(worktree_root, guard_paths)
    return {path: _blob_source(spine, common_dir, path) for path in guard_paths}


def _guard_class_relay_step(
    worktree_root: Path,
    guard_paths: list,
    pre_commit_sources: dict,
    committed_sha: str,
    repo_root: Optional[Path] = None,
) -> dict:
    """Runs AFTER `commit_paths` has already landed the commit -- this step
    cannot refuse, delay, or fail it (NEGATIVE SPEC, C2 body). Detects a
    `write_guards` module CLASS transition (hard-deny <-> advisory) across
    the commit just made, via C1's `detect_class_transition`
    (`write_guards/guard_class_relay.py`), and stages a memo for each
    detected transition via C3's `stage_class_transition_memo` (same
    module) -- an in-process `memo.draft`/`memo.compose` op call, never a
    subprocess.

    `pre_commit_sources` is `_pre_commit_guard_sources`'s result, captured
    BEFORE `commit_paths` ran (see that function's docstring for why it
    cannot be captured here). The "new" side needs no git read at all: the
    worktree at THIS path, right now, holds exactly what was just
    committed (the ordinary case -- `commit_paths` writes from worktree
    bytes) or the staged-preferred bytes, close enough for a CLASS-literal
    comparison; a path deleted by this commit simply has no file to read.

    Returns `{"transitions": [...], "skips": [...]}`, both possibly empty.
    A `transitions` entry is `{"module", "old_class", "new_class", "sha",
    "memo_staged", "memo_topic"}` -- the last two report
    `stage_class_transition_memo`'s outcome for that entry (memo_staged is
    True on a fresh stage OR an idempotent no-op re-stage; memo_topic is the
    composed topic slug). A per-transition emission failure never drops the
    detected transition itself -- it is recorded as a NAMED `skips` entry
    alongside it (C3 negative spec: "never raise" must not become "silently
    do nothing"). Any exception anywhere in this step (detection itself, not
    a single emission) is caught and degrades to one `skips` string -- this
    step never raises and never touches `committed`/`sha` in the caller's
    own result.
    """
    if not guard_paths:
        return {"transitions": [], "skips": []}

    transitions: list = []
    skips: list = []
    try:
        for path in guard_paths:
            old_source = pre_commit_sources.get(path)
            file_path = worktree_root / path
            try:
                new_source: Optional[str] = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                new_source = None
            transition = detect_class_transition(old_source, new_source)
            if transition is not None:
                old_class, new_class = transition
                entry = {
                    "module": path,
                    "old_class": old_class,
                    "new_class": new_class,
                    "sha": committed_sha,
                }
                transitions.append(entry)
                # Emission failure is NAMED, never swallowed (C3 negative
                # spec) -- but scoped to THIS transition's own try/except so
                # one bad emission does not stop the loop from detecting/
                # staging the rest.
                try:
                    emission = stage_class_transition_memo(
                        entry, repo_root=repo_root
                    )
                except Exception as exc:  # noqa: BLE001 -- never raise
                    emission = {
                        "staged": False, "topic": None,
                        "reason": f"stage_class_transition_memo raised: {exc!r}",
                    }
                entry["memo_staged"] = emission.get("staged")
                entry["memo_topic"] = emission.get("topic")
                if not emission.get("staged"):
                    skips.append(
                        f"skip: guard_class_relay memo emission for "
                        f"{path!r}: {emission.get('reason')}"
                    )
    except Exception as exc:  # noqa: BLE001 -- never raise, C2 negative spec
        skips.append(f"skip: guard_class_relay step: {exc!r}")

    return {"transitions": transitions, "skips": skips}


def _release_committed_claims_step(worktree_root: Path, released: list[str]) -> None:
    """Runs AFTER `commit_paths` has already landed the commit -- like
    `_guard_class_relay_step`, this step cannot refuse, delay, or fail it
    (NEGATIVE SPEC).

    `sid` comes from `session_core.resolve_session_id`, which can resolve to
    the SPAWNING session's identity inside the resident warm server (env
    tiers 1-3) rather than the true caller's -- the same exposure
    `detached_render_commit.py` and `post_commit_tail.py` already carry
    through the same function; not a new class introduced here, and the
    transport fix is routed separately.
    """
    if not released:
        return
    try:
        sid = session_core.resolve_session_id(str(worktree_root))
        if sid:
            session_scope.release_committed_claims(
                sid, released, cwd=str(worktree_root)
            )
    except Exception:
        _LOG.debug(
            "commit_v2: release_committed_claims failed post-commit; "
            "claim(s) retained", exc_info=True,
        )


def _pre_commit_gates(
    worktree_root: Path, paths: list, deleted_paths: list
) -> Optional[str]:
    """Run the gates `run_commit_pipeline` ran before landing, returning a
    refusal string or None. TWO of its four, and the omissions are the point.

    `run_commit_pipeline` ran four; C3 repointed every caller onto this op,
    which ran none, and the resulting capability drop was filed as a P1
    (`the-commit-v2-route-runs-none-of-the-fou`). The spike that P1 asked for
    measured all four in this exact call shape:
    docs/research/spike-verdicts/2026-08-30-what-the-four-commit-gates-actually-cost.md.
    All four together cost 71.6ms at one path and 99.0ms at 35, with ZERO
    process spawns, so the budget objection the P1 anticipated never applied --
    the brightline is about process creation, and these create none.

    WHY `deletion_block_gate` IS NOT HERE, and it is not an oversight or a
    budget call. It is the cheapest of the four to justify dropping and the
    most expensive mistake to include. Its Assertion-3 refuses a commit whose
    IN-SCOPE staged deletions are not accounted for by a "Step 2.67" block in
    the commit body -- a `workstream-complete` CEREMONY convention, not a
    general commit convention. `run_commit_pipeline` was the ceremony
    committer and could assume it; `ceremony.commit_v2` is the general
    committer every session and the dispatchable `git-commit-agent` route
    through, and cannot. Measured against this repo's own history before
    wiring: 346 of the last 400 commits delete a path and NONE carries a Step
    2.67 block, so reinstating it here would have refused roughly 86% of this
    repo's commits across every concurrent session. Verified it genuinely
    fires rather than no-oping in this shape -- a staged deletion in scope
    with an ordinary message returns passed=False -- so this is a real
    exclusion, not a gate that would have sat inert.

    That check still HAS a live home on the ceremony path it was written for:
    `commit_gates.main()`, reached by
    `coordinator/bin/check-workstream-complete-deletion-blocks`. What it lost
    in the repoint was an in-commit caller on the ceremony route, and putting
    it on the general route is not the way to give it one back.

    Deletion accountability itself is NOT skipped, though -- it is covered by
    a DIFFERENT oracle, `declared_deletion_gate` (docs/plans/2026-08-30-
    deletion-accountability-without-the-cere.md), called explicitly below
    rather than through the loop. `commit_v2` receives `params.deleted_paths`
    as a structured declaration of what this commit removes -- a parameter
    the ceremony committer never had, which is exactly why the ceremony gate
    had to parse the commit body instead. The new gate compares that
    declaration against staged reality directly: every in-scope staged
    deletion must appear in `deleted_paths`, no prose required. So the ~86%
    figure above stays true of `deletion_block_gate` specifically -- its
    prose-body oracle is still the wrong shape for this route -- but the gap
    it left, an undeclared deletion landing unnoticed, is now closed.

    WHY `dirty_tree_gate` IS NOT HERE EITHER, and this one is redundancy
    rather than blast radius. DR-227's vacuity argument already proves that
    for a SCOPED caller the only case-(c) member reachable at all is an
    unstaged deletion -- an in-scope path with an index entry whose worktree
    file has vacated. Through THIS op that path does not survive to the gate:
    `commit_paths` reads every non-deleted member's bytes to build the tree
    and refuses before anything lands. The axis is already covered, louder
    and earlier, by the committer itself.

    QUOTE THE MESSAGE FROM THE RAISE SITE, NOT FROM HERE. This paragraph
    used to say the refusal reads `cannot read <path>`, which stopped being
    true at `62fe8736d1`: a named path that git still tracks and the
    worktree no longer has now refuses with "<path> is gone from the
    worktree but still tracked -- pass it in `deleted_paths` to commit the
    deletion", and only a path git does not track falls through to the
    errno-shaped `cannot read`. The stale quote cost a real reader real
    time -- doe-claude-em read it on 2026-08-31 while verifying a cross-repo
    memo, concluded the refusal gave the caller no route, and filed an ask
    for a fix that had landed in the very tree they were reading. A
    docstring that quotes a message verbatim is a copy that goes stale
    silently; `coordinator_core/git/commit.py::commit_paths` is the
    authority.

    It is also the one that could not be made cheap. Its scoped branch calls
    `read_index` unscoped -- 35.16ms of the ~40ms it costs -- and the obvious
    swap to `parse_index_identity` (1.95-3.91ms, and what `commit_paths`
    already uses) does NOT preserve behaviour: `read_index` raises
    `IndexParseError` on an unmerged index and `parse_index_identity` does
    not, so the swap would silently turn a mid-merge-conflict refusal into a
    pass -- the exact regression this gate's own F1 code-review finding
    exists to prevent. Reinstating it as written would also put this op over
    `PROCESS_TIME_TARGET_MS` (50.0), the standing budget its own gate asserts.

    What remains -- `carry_gate` and `op_scope_coverage_gate` -- is free
    (0.00ms and 2.60ms worst-case, zero spawns), needs no message convention,
    and catches two things nothing else on this route catches: a staged
    handoff whose `carried_items` declare undeclared state, and a registry-map
    change registering an op with no scope coverage.
    """
    gate_paths = list(paths) + list(deleted_paths)
    if not gate_paths:
        return None

    # `declared_deletion_gate` needs the declaration argument the other two
    # do not, so it does not fit the uniform `(name, gate)` loop below --
    # called explicitly, BEFORE `commit_paths` (a refusal after the commit
    # lands is not a refusal), passing `deleted_paths` as the declaration.
    deletion_outcome = declared_deletion_gate(worktree_root, gate_paths, deleted_paths)
    if not deletion_outcome.passed:
        return "declared_deletion_gate: " + "; ".join(deletion_outcome.diagnostics[:5])

    # Named explicitly rather than read off `gate.__name__`: the refusal text
    # is what the caller acts on, and a decorated, wrapped, or patched gate
    # would otherwise report itself as `<lambda>` at exactly the moment
    # somebody needs to know which gate refused.
    for name, gate in (
        ("carry_gate", carry_gate),
        ("op_scope_coverage_gate", op_scope_coverage_gate),
    ):
        outcome = gate(worktree_root, gate_paths)
        if not outcome.passed:
            return f"{name}: " + "; ".join(outcome.diagnostics[:5])

    return None


def _error(message: str, **extra: object) -> dict:
    """Build the structured-error result envelope for this op.

    Purpose: uniform fail-loud shape -- contract fields present with
    "committed" false and "sha" null, plus "error" naming what happened.
    """
    result: dict = {"committed": False, "sha": None, "error": message}
    result.update(extra)
    return result


@register_op("ceremony.commit_v2")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.commit_v2" handler -- mutating, sync.

    Sync (not async): `commit_paths` does synchronous filesystem I/O only
    (zero git spawns on the common shapes); ipc.py offloads sync handlers via
    asyncio.to_thread (commit_exec_bit / commit_anchors pattern).

    Params:
        repo_root (str, optional)   -- D3 consistency assertion only
                                       (check_repo_root); NEVER the
                                       worktree-resolution source.
        paths     (list[str], required) -- repo-relative paths to commit.
                                       At least one of `paths`/`deleted_paths`
                                       must be non-empty (an empty pathspec is
                                       refused by `commit_paths` itself --
                                       it commits the WHOLE INDEX otherwise).
        deleted_paths (list[str], optional) -- repo-relative paths to record
                                       as removed in this commit.
        message   (str, required)   -- the commit message.
        prefer_staged (list[str], optional) -- paths whose STAGED bytes are
                                       committed in preference to differing
                                       worktree bytes (commit_paths invariant
                                       1 -- a deliberate partial stage,
                                       declared, never inferred).
        prefer_deliberate_stage (bool, optional, DEFAULT FALSE) -- the
                                       blanket form of `prefer_staged`
                                       (DR-379): preserve the staged bytes of
                                       every path in this call that has them,
                                       without naming the paths up front. The
                                       shared-branch case, where the diverging
                                       content is a peer's and cannot be
                                       enumerated in advance. Default stays
                                       False -- see the backlog row
                                       `commit-v2-prefers-worktree-over-index`
                                       for why it is not flipped.
    Returns:
        {"committed": True, "sha": str, "staged_preferred": [str, ...],
         "worktree_over_staged": [str, ...], "warnings": [str, ...],
         "guard_class_relay": {"transitions": [...], "skips": [...]}} on
        success -- `warnings` is non-empty exactly when
        `worktree_over_staged` is, and says the same thing in the register
        an operator reads. `guard_class_relay` is the C2 step (docs/plans/
        2026-08-29-a-guard-class-flip-announces-itself.md): a detected
        `write_guards` module CLASS transition in this commit, per path
        under `coordinator_core/write_guards/`. It never gates or delays
        the commit above -- a step failure degrades to a `skips` entry.
        Or
        {"committed": False, "sha": None, "error": str} on any
        structured refusal (an empty pathspec, a directory in `paths`, an
        unresolvable CAS ref, a lost CAS race, or a path needing a checkin
        conversion neither this module nor its `blob_fallback` can
        reproduce).

    Keying scope: common_dir -- repo_root arg is the .git common dir; the
    caller's worktree is main_worktree_root(repo_root).
    """
    if repo_root is None:
        return _error(
            "ceremony.commit_v2 requires a common_dir-keyed dispatch; "
            "repo_root (git common dir) was not supplied"
        )

    d3_mismatch = check_repo_root(params.get("repo_root"), repo_root)
    if d3_mismatch is not None:
        return _error(d3_mismatch)

    raw_paths = params.get("paths") or []
    if not isinstance(raw_paths, list) or not all(isinstance(p, str) for p in raw_paths):
        return _error("params.paths must be a list of strings")

    raw_deleted = params.get("deleted_paths") or []
    if not isinstance(raw_deleted, list) or not all(isinstance(p, str) for p in raw_deleted):
        return _error("params.deleted_paths must be a list of strings")

    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error("params.message is required and must be a non-empty string")

    raw_prefer_staged = params.get("prefer_staged") or []
    if not isinstance(raw_prefer_staged, list) or not all(
        isinstance(p, str) for p in raw_prefer_staged
    ):
        return _error("params.prefer_staged must be a list of strings")

    raw_prefer_deliberate_stage = params.get("prefer_deliberate_stage", False)
    if not isinstance(raw_prefer_deliberate_stage, bool):
        return _error("params.prefer_deliberate_stage must be a boolean")

    worktree_root = main_worktree_root(repo_root)

    # Filter FIRST, before anything else touching the guard-class-relay step
    # -- no path under `_GUARD_MODULE_DIR` means zero work below: no git
    # call, no object read, no AST parse (0.156 us / 0 spawns measured,
    # spike-verdicts/2026-08-29-guard-class-relay-commit-seam.md Q4).
    guard_paths = _guard_module_paths(raw_paths, raw_deleted)
    pre_commit_guard_sources = (
        _pre_commit_guard_sources(worktree_root, guard_paths) if guard_paths else {}
    )

    # Gates run BEFORE the commit lands -- they are refusals, and a refusal
    # after the fact is not one. Contrast `_guard_class_relay_step` below,
    # which runs after and is forbidden from failing the commit.
    gate_refusal = _pre_commit_gates(worktree_root, raw_paths, raw_deleted)
    if gate_refusal is not None:
        return _error(gate_refusal)

    # Declared-vs-actual EOL, for the executables THIS COMMIT touches -- the
    # write-scoped v2 the eol family's deletion left owed (kill-ledger K-064's
    # returns-when spec; `docs/reference/eol-drift-detection.md`). Filter-first
    # like `guard_paths` above: a commit carrying no `.cmd`/`.ps1`/`.sh`/`.bat`
    # spawns nothing and reads nothing.
    #
    # BEFORE the commit, not after, and NOT a refusal. Before, so the tree that
    # lands already carries correct bytes. Not a refusal, because the repair is
    # provably content-neutral -- check-in normalization maps the drifted and
    # repaired files to the same blob -- so there is nothing for an operator to
    # adjudicate and nothing a refusal would protect.
    # A repair-side exception is a worse defect than the drift it looks for
    # (NEGATIVE SPEC, `eol_declared` module docstring) -- neither function is
    # documented to raise, but nothing upstream of this line guarantees it,
    # so this is wrapped rather than trusted. Review finding 1, 2026-08-30.
    # `prefer_staged` paths are excluded: that parameter exists precisely
    # because the caller wants the INDEX content committed while deliberately
    # leaving the working tree diverged (see `worktree_over_staged` below).
    # Repairing those bytes anyway overrides a deliberate operator choice,
    # even though it would not change what lands. Review finding 5, 2026-08-30.
    try:
        prefer_staged_set = set(raw_prefer_staged)
        eol_candidates = [p for p in raw_paths if p not in prefer_staged_set]
        eol_drifts = find_declared_eol_drift(worktree_root, eol_candidates)
        eol_repaired = (
            repair_declared_eol_drift(worktree_root, eol_drifts) if eol_drifts else []
        )
    except Exception:
        eol_drifts = []
        eol_repaired = []

    # Attach Session-Id / Deliverable-Id (whichever are resolvable and not
    # already present) via the shared applier -- this route lands via
    # `commit_paths`' `commit-tree` plumbing, which fires NO git hooks
    # (`prepare-commit-msg` included), so this call is this route's ONLY
    # attach point (docs/dispatch-briefs/.../C3.md; commit_trailers.py
    # module docstring). Never blocks: `apply_missing_trailers` degrades to
    # `message` unchanged on any resolution failure.
    message = apply_missing_trailers(
        message, worktree_root, list(raw_paths) + list(raw_deleted)
    )

    try:
        outcome = commit_paths(
            worktree_root,
            raw_paths,
            message,
            deleted_paths=raw_deleted,
            prefer_staged=raw_prefer_staged,
            prefer_deliberate_stage=raw_prefer_deliberate_stage,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree_root),
        )
    except NothingToCommit as exc:
        # Distinguished from the other refusals in the SAME envelope, not a
        # new one: `committed: False` plus the message is what makes it
        # legible to a human reading one line, and the flag is what lets a
        # caller that legitimately expects a possible no-op (a follow-up
        # commit after `memo.send` already committed its own receipt) tell
        # "already done" from "failed" without parsing prose.
        return _error(str(exc), nothing_to_commit=True)
    except IndexStaleAfterCommit as exc:
        # THE COMMIT LANDED. Reporting this as an error is the single most
        # expensive mistake available on this op: `_error` says `committed:
        # False`, the caller retries, and the same work is committed twice.
        # `commit_paths` splices the index AFTER the ref swap by design, so a
        # peer holding `.git/index.lock` for the width of that splice lands
        # here with real work in history -- routine at the ~50-session load
        # norm, not exotic.
        #
        # Recovered as a SUCCESS carrying the outcome the splice would have
        # returned, with the stale index reported as a warning rather than a
        # failure, because that is all the residue actually is: peers' `git
        # status` misreports these paths until any subsequent index write
        # refreshes it.
        # `cast`, not a runtime check: `IndexStaleAfterCommit.outcome` is typed
        # `object` there only to keep `index_write` free of an import cycle
        # back to `commit.py`. The sole raiser passes a real `CommitOutcome`.
        outcome = cast(CommitOutcome, exc.outcome)
        if exc.outcome is None:
            # No raiser on the commit path omits it; if one ever does, say so
            # rather than inventing a sha -- but never downgrade to `_error`,
            # because the commit landed either way.
            return _error(
                f"{exc} (the outcome was not carried out of the raise site, so "
                "no sha can be reported -- resolve it with `git log -1`)"
            )
        index_stale_warning = str(exc)
    except (CommitRefused, FilterUnsupported) as exc:
        return _error(str(exc))
    else:
        index_stale_warning = None

    # A FIELD IS NOT A SIGNAL. The other route through this same disagreement
    # (`commit_scoped`'s private-index branch, via `commit_pipeline`) has
    # carried a loud message on SUCCESS since the 2026-08-10 bug-backlog row,
    # on the reasoning that a commit which sets one side aside is a legitimate
    # success and the operator still needs to see why. This route returned the
    # equivalent fact as a dict key nobody is obliged to read, so the same
    # disagreement was loud on one path and silent on the other.
    warnings = []
    if index_stale_warning is not None:
        # First, because it is the one warning here that changes what the
        # reader should DO: everything else describes which bytes landed; this
        # one says the commit landed and the index did not, so `git status`
        # will misreport these paths until any subsequent index write.
        warnings.append(index_stale_warning)
    if outcome.worktree_over_staged:
        # Bounded like the other truncation sites in this diff area
        # (commit.py's `refused[:5]` / `sorted(unknown)[:5]`) -- an unbounded
        # join of every passed-over path violates the register's bounded,
        # terse intent (docs/wiki/guard-messaging.md § Register) on a commit
        # touching a dozen-plus partially-staged paths.
        paths = ", ".join(outcome.worktree_over_staged[:5])
        if len(outcome.worktree_over_staged) > 5:
            paths += ", ..."
        warnings.append(
            f"committed worktree content for {len(outcome.worktree_over_staged)} "
            f"path(s) whose index held different content: {paths}. "
            "Pass prefer_staged to name those paths, or "
            "prefer_deliberate_stage=true to preserve every staged blob in "
            "this call -- the latter is the shared-branch answer, where the "
            "differing content is a peer's and you cannot name it up front."
        )

    if outcome.no_delta:
        # The k-of-N face of the zero-delta bug, and it belongs in `warnings`
        # for the same reason the divergence above does: the caller named
        # these paths and they are not in the commit, which is indistinguish-
        # able from delivery on the success line alone. Bounded at five like
        # every other join in this envelope.
        # "already at HEAD" is FALSE for a path HEAD never had. Those are
        # split out below: a declared deletion of a file HEAD does not carry
        # is not a benign no-op, it is a declaration the caller could not
        # have meant, and it is what an untracked path looks like after the
        # pathspec split misclassifies it from the wrong cwd. Collapsing the
        # two into the reassuring sentence is how a skipped new file reads as
        # "nothing was owed" (state/audits/2026-08-31-committer-p0-*).
        absent = set(getattr(outcome, "declared_absent_from_head", ()))
        matched = [p for p in outcome.no_delta if p not in absent]
        declared = len(raw_paths) + len(raw_deleted)

        if matched:
            paths = ", ".join(matched[:5])
            if len(matched) > 5:
                paths += ", ..."
            warnings.append(
                f"{len(matched)} of {declared} declared path(s) contributed "
                f"nothing -- already at HEAD: {paths}. If you expected a "
                "change there, it landed elsewhere or never landed."
            )

        if absent:
            shown = sorted(absent)
            paths = ", ".join(shown[:5])
            if len(shown) > 5:
                paths += ", ..."
            warnings.append(
                f"{len(shown)} of {declared} declared path(s) were SKIPPED, "
                f"not committed: declared deleted, but HEAD has no such path: "
                f"{paths}. Nothing was deleted and nothing was added. If you "
                "meant to commit a new file, it is still uncommitted -- check "
                "that you invoked from the repo root."
            )

    # Reported, not because reporting is what fixes it -- the repair above did
    # that -- but because a launcher's bytes changing under an operator is a
    # fact they are owed. Bounded at five like the join above, same reason.
    if eol_drifts:
        shown = "; ".join(d.describe() for d in eol_drifts[:5])
        if len(eol_drifts) > 5:
            shown += "; ..."
        unfixed = len(eol_drifts) - len(eol_repaired)
        warnings.append(
            f"repaired declared-vs-actual line endings on {len(eol_repaired)} of "
            f"{len(eol_drifts)} executable path(s): {shown}."
            + (f" {unfixed} could not be written." if unfixed else "")
        )

    # Step runs AFTER the commit has landed -- it cannot refuse, delay, or
    # fail it (NEGATIVE SPEC). `guard_class_relay` is additive; existing
    # consumers reading `committed`/`sha`/`warnings` see no change.
    guard_class_relay = _guard_class_relay_step(
        worktree_root, guard_paths, pre_commit_guard_sources, outcome.sha,
        repo_root=repo_root,
    )

    # Same post-commit region, same negative spec: cannot refuse, delay, or
    # fail the already-landed commit. Release set is the paths THIS commit
    # actually covered -- raw_paths + raw_deleted, including outcome.no_delta
    # members (the caller named them and their bytes are at HEAD).
    _release_committed_claims_step(
        worktree_root, list(raw_paths) + list(raw_deleted)
    )

    return {
        "committed": True,
        "sha": outcome.sha,
        "staged_preferred": list(outcome.staged_preferred),
        "worktree_over_staged": list(outcome.worktree_over_staged),
        "no_delta": list(outcome.no_delta),
        "warnings": warnings,
        "guard_class_relay": guard_class_relay,
    }
