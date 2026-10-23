"""
coordinator_core.ops.ceremony.commit_v2 -- JSON-RPC "ceremony.commit_v2" op.

Purpose: a fresh dispatchable identity over the 694 measured, zero-spawn lines
in `coordinator_core/git/commit.py` (`commit_paths`) and
`coordinator_core/git/index_write.py` (`splice_index`, called internally by
`commit_paths`). `ceremony.commit` is DEAD (`coordinator_core/op_budget_suspension.py`) and
ITS NAME STAYS DEAD: this op is a fresh identity, ONCE, so every guard, budget
row and test fixture keyed to the old name is unambiguous about which op it
describes. Do not resurrect "ceremony.commit" for this handler or any other.

Handler shape: a thin envelope over `commit.commit_paths`. It does NOT import
`run_commit_pipeline`, and does NOT import anything from `commit_pipeline.py` or `git_native.py` --
those two modules are the 11,015-line surface this op exists to make
un-necessary, and importing from them here would make the v2 a second path
into the same pipeline rather than a replacement for it
(docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md, C3 body).

Keying scope: common_dir; the worktree derives from `main_worktree_root(repo_root)`.
`params.repo_root` is the optional D3 consistency assertion only
(`check_repo_root`), never the worktree-resolution source.

The handler supplies `blob_fallback` (`commit.hash_worktree_blobs_via_spawn`, not
`git_native.py`'s `_hash_worktree_blobs`, barred below) so an `eol=crlf`-pinned
path carrying CR bytes lands via ONE batched `git hash-object -w --stdin-paths`
spawn per commit rather than propagating as a structured `FilterUnsupported`. A refusal `commit_paths` cannot
resolve even with the fallback (the fallback itself fails, or returns no sha
for a path) still propagates as a structured error, unmodified in substance
-- this handler does not catch, retry, or widen it.

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
    was written. No signing-shaped carve-out: `commit_paths` never raises over a signing failure; a
    broken signing setup lands the commit unsigned and reports `CommitOutcome.sign_warning`.
  - Does NOT use `params.repo_root` as the worktree-resolution source (D3:
    socket-authoritative common_dir only).
"""

from __future__ import annotations

import contextlib
import re
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Sequence, cast

from coordinator_core.git.commit import (
    CommitOutcome,
    BlobDriftRefused,
    CommitRefused,
    NothingToCommit,
    FilterUnsupported,
    commit_paths,
    hash_worktree_blobs_via_spawn,
)
from coordinator_core.git.commit_trailers import (
    _UUID_RE,
    apply_missing_trailers,
    message_missing_subject,
)
from coordinator_core.git.commit import partition_declared_deletions
from coordinator_core.git.run import run_git
from coordinator_core.git import checkpoint_guard
from coordinator_core.git.git_state import head_sha, read_index_paths
from coordinator_core.git.git_index import IndexParseError
from coordinator_core.git.index_write import IndexStaleAfterCommit
from coordinator_core.git.eol_declared import (
    find_declared_eol_drift,
    repair_declared_eol_drift,
)
from functools import partial
from coordinator_core.git.commit_walk import commit_meta
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.main_commit_policy import main_commit_refusal
from coordinator_core.git.git_objects import _read_object
from coordinator_core.git.git_state import read_tree_spine
from coordinator_core.authoring_leaks import leak_gate
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.commit_gates import (
    attribution_gate,
    carry_gate,
    residue_gate,
    split_process_paths,
    claude_md_budget_gate,
    declared_deletion_gate,
    machine_path_leak_gate,
    op_scope_coverage_gate,
    registration_quad_gate,
)
from coordinator_core.ops.ceremony.commit_admission import (
    publish_allowlist_drift_refusal,
    skill_weight_check_worktree,
)
from coordinator_core.ops.ceremony.commit_source import post_commit_reader
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.session import claim_index as session_claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness as session_liveness
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.git.path_canon import canonical_repo_relative_path_refusal
from coordinator_core import dispatch_abandon
from coordinator_core.ops.ceremony.commit_v2_phases import PhaseLedger
from coordinator_core.telemetry import spawn_counter


def detect_class_transition(old_source, new_source):
    """Lazy seam over `guard_class_relay.detect_class_transition`; loads `write_guards.engine`
    only when a guard module is in the landing."""
    from coordinator_core.write_guards.guard_class_relay import detect_class_transition as _impl

    return _impl(old_source, new_source)


def stage_class_transition_memo(entry, repo_root=None):
    """Lazy seam over `guard_class_relay.stage_class_transition_memo`."""
    from coordinator_core.write_guards.guard_class_relay import stage_class_transition_memo as _impl

    return _impl(entry, repo_root=repo_root)


#: Prefix filter for the guard-class-relay step: only paths under this directory can carry a
#: `write_guards` CLASS constant; no path under it means the step does no work.
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

    `pre_commit_sources` is `_pre_commit_guard_sources`'s result, captured BEFORE
    `commit_paths` ran. The "new" side reads the worktree: it holds what was just committed.

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
                # Emission failure is NAMED, never swallowed -- but scoped to THIS transition's
                # try/except so one bad emission does not stop the loop staging the rest.
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
    except Exception as exc:  # noqa: BLE001 -- never raise (negative spec)
        skips.append(f"skip: guard_class_relay step: {exc!r}")

    return {"transitions": transitions, "skips": skips}


def _release_committed_claims_step(worktree_root: Path, released: list[str]) -> None:
    """Runs AFTER `commit_paths` has already landed the commit -- like
    `_guard_class_relay_step`, this step cannot refuse, delay, or fail it
    (NEGATIVE SPEC).

    `sid` comes from `session_core.resolve_session_id`, which can resolve to the SPAWNING
    session inside the resident warm server rather than the true caller.
    """
    if not released:
        return
    session_scope.release_committed_claims_or_retain(
        worktree_root,
        released,
        session_core.resolve_session_id(str(worktree_root)),
        "commit_v2",
    )


#: Body line naming a live peer's write-claimed path this commit carried, so history carries
#: the fact for the peer. A body line, not a trailer: a new trailer key is a contract bump.
ABSORBED_PEER_CLAIM_PREFIX = "Absorbed-Peer-Claim"


def _disclose_peer_holds(message: str, peer_holds: list) -> str:
    """Insert one disclosure line per live peer hold after the subject
    paragraph, so any trailer block stays last."""
    if not peer_holds:
        return message
    lines = "\n".join(f"{ABSORBED_PEER_CLAIM_PREFIX} {sid} {path}" for path, sid in peer_holds)
    head, sep, rest = message.partition("\n\n")
    if not sep:
        return f"{message.rstrip(chr(10))}\n\n{lines}\n"
    return f"{head}\n\n{lines}\n\n{rest}"


def _disclose_gone_deletions(message: str, gone: list) -> str:
    """A `Removes:` line per path `gone_tracked_as_deleted` reclassified.

    The message is composed before anyone knows which declared paths were deleted, and the
    undeclared-staged-deletion check reads it."""
    if not gone:
        return message
    lines = "\n".join(f"Removes: {path}" for path in gone)
    head, sep, rest = message.partition("\n\n")
    if not sep:
        return f"{message.rstrip(chr(10))}\n\n{lines}\n"
    return f"{head}\n\n{lines}\n\n{rest}"


_TRAILER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: \S")


def _stopped_run_trailers(message: str, run_ids: list) -> str:
    """Append one `Stopped-Run-Residue: <run_id>` trailer per carried stopped run, joining the
    message's trailer block when it ends in one."""
    if not run_ids:
        return message
    lines = [f"Stopped-Run-Residue: {rid}" for rid in run_ids]
    body = message.rstrip("\n")
    head, sep, last = body.rpartition("\n\n")
    if sep and all(_TRAILER_LINE.match(ln) for ln in last.split("\n")):
        return f"{head}\n\n{last}\n" + "\n".join(lines) + "\n"
    return f"{body}\n\n" + "\n".join(lines) + "\n"


def _peer_claim_warnings(
    worktree_root: Path, paths: list, peer_holds: Optional[list] = None,
    own_sid: Optional[str] = None, abandoned_holds: Optional[list] = None,
    held_out: Optional[dict] = None,
) -> list:
    """One batched ``claim_index.lookup(paths)`` plus a liveness check on each non-self claimant.
    Reports a live peer's hold; never refuses, never spawns, never raises: a lookup failure or
    ``UNANSWERABLE`` entry degrades to a "claim state indeterminate" warning. Each live peer also
    gets one warning naming (capped at 5) its untracked files outside ``paths``.
    A claimant ``session_abandoned`` reads idle joins ``abandoned_holds`` (when given), not
    ``peer_holds``. ``own_sid`` (opt-in) names the caller's session.
    """
    if not paths:
        return []

    if own_sid is None:
        try:
            own_sid = session_core.resolve_session_id(str(worktree_root))
        except Exception:
            own_sid = None

    try:
        lookup_result = session_claim_index.lookup(list(paths), cwd=str(worktree_root))
    except Exception as exc:  # noqa: BLE001 -- never refuse the commit
        return [f"claim state indeterminate ({exc!r})"]

    if held_out is not None:
        held_out.update(lookup_result.held_by or {})
    warnings: list = []
    live_peers: list = []
    for path in paths:
        claimants = lookup_result.get(path, [])
        if session_claim_index.UNANSWERABLE in claimants:
            warnings.append(f"claim state indeterminate for {path!r}")
            continue
        kinds = (lookup_result.recorded_kind or {}).get(path, {})
        for sid in claimants:
            if sid == own_sid or not touch_record.kind_blocks_a_peer_commit(kinds.get(sid)):
                continue
            try:
                live = session_liveness.session_live(sid, str(worktree_root))
            except Exception:  # noqa: BLE001 -- see docstring
                warnings.append(f"claim state indeterminate for {path!r}")
                continue
            if live:
                try:
                    abandoned = session_liveness.session_abandoned(sid, str(worktree_root))
                except Exception:  # noqa: BLE001 -- see docstring
                    abandoned = False
                if abandoned:
                    if abandoned_holds is not None:
                        abandoned_holds.append((path, sid))
                    warnings.append(
                        f"{path!r} is held by session {sid!r}, live but idle here past the "
                        "abandonment window -- claim released, committed"
                    )
                    continue
                if peer_holds is not None:
                    peer_holds.append((path, sid))
                if sid not in live_peers:
                    live_peers.append(sid)
                warnings.append(
                    f"{path!r} is held by live peer session {sid!r} -- "
                    "committed anyway"
                )
    if live_peers:
        try:
            from coordinator_core.ops.ceremony.commit_companion import untracked_companions

            companions = untracked_companions(
                worktree_root, lookup_result.held_by or {}, live_peers, list(paths)
            )
            for sid, names in companions.items():
                shown = ", ".join(names[:5])
                if len(names) > 5:
                    shown += f" (+{len(names) - 5} more)"
                warnings.append(
                    f"live peer session {sid} holds untracked file(s) this "
                    f"commit leaves behind: {shown}"
                )
        except Exception as exc:  # noqa: BLE001 -- never refuse the commit
            warnings.append(f"companion state indeterminate ({exc})")
    return warnings


def _own_left_behind(
    worktree_root: Path, held_by: dict, own_sid: Optional[str], pathspec: list,
) -> tuple:
    """``(free, contested)`` own dirty paths outside ``pathspec``. A per-path liveness or
    ``UNANSWERABLE`` doubt is contested; a failure of the whole candidate scan or lookup returns
    ``([], [])`` (nothing carried, nothing reported). A sid holding no committed path is absent
    from ``held_by`` and carries nothing."""
    held = [p for p in held_by.get(own_sid, ()) if p not in pathspec] if own_sid else []
    if not held:
        return [], []
    try:
        from coordinator_core.ops.ceremony.commit_companion import own_left_behind

        candidates = own_left_behind(worktree_root, held_by, own_sid, pathspec)
    except Exception:  # noqa: BLE001 -- never refuse the commit
        return [], []
    if not candidates:
        return [], []
    contested: set = set()
    try:
        peers = session_claim_index.lookup(list(candidates), cwd=str(worktree_root))
    except Exception:  # noqa: BLE001
        return [], []
    for path in candidates:
        claimants = peers.get(path, [])
        if session_claim_index.UNANSWERABLE in claimants:
            contested.add(path)
            continue
        kinds = (peers.recorded_kind or {}).get(path, {})
        for sid in claimants:
            if sid == own_sid or not touch_record.kind_blocks_a_peer_commit(kinds.get(sid)):
                continue
            try:
                if not session_liveness.session_live(sid, str(worktree_root)):
                    continue
            except Exception:  # noqa: BLE001
                contested.add(path)
                break
            try:
                if session_liveness.session_abandoned(sid, str(worktree_root)):
                    continue
            except Exception:  # noqa: BLE001
                pass
            contested.add(path)
            break
    return [p for p in candidates if p not in contested], [p for p in candidates if p in contested]


def _pre_commit_gates(
    worktree_root: Path,
    paths: list,
    deleted_paths: list,
    read_source: Callable[[str], Optional[bytes]],
    prefer_staged: Sequence[str] = (),
    deliberate_stage: bool = False,
) -> Optional[str]:
    """Run the gates `run_commit_pipeline` ran before landing, returning a
    refusal string or None. TWO of its four, and the omissions are the point.

    Runs now, in order: `declared_deletion_gate`, `carry_gate`, `residue_gate`,
    `op_scope_coverage_gate`, `leak_gate`, `registration_quad_gate`,
    `claude_md_budget_gate`, `machine_path_leak_gate`, `attribution_gate`.
    `read_source` is the post-commit reader (`commit_source.post_commit_reader`): the last
    three and `op_scope_coverage_gate` judge the tree this commit lands, never the
    worktree or the index.

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
    time -- coordinator-content-repo-em read it on 2026-08-31 while verifying a cross-repo
    memo, concluded the refusal gave the caller no route, and filed an ask
    for a fix that had landed in the very tree they were reading. A
    docstring that quotes a message verbatim is a copy that goes stale
    silently; `coordinator_core/git/commit.py::commit_paths` is the
    authority.

    It is also the one that could not be made cheap. Its scoped branch calls
    `read_index` unscoped -- 35.16ms of the ~40ms it costs -- and the obvious
    swap to `parse_index_identity` (1.95-3.91ms, and what `commit_paths`
    already uses) does NOT preserve behaviour: `read_index` raises
    `IndexParseError` on an unmerged index and `parse_index_identity` raises
    it too unless handed `unmerged_out`; a swap that passed `unmerged_out`
    would silently turn a mid-merge-conflict refusal into a
    pass -- the exact regression this gate's own F1 code-review finding
    exists to prevent. Reinstating it as written would also put this op over
    `PROCESS_TIME_TARGET_MS` (50.0), the standing budget its own gate asserts.

    `carry_gate` and `op_scope_coverage_gate` are free (zero spawns) and catch two things
    nothing else on this route catches: a staged handoff whose `carried_items` declare
    undeclared state, and a registry-map change registering an op with no scope coverage.

    `leak_gate` (`coordinator_core/authoring_leaks`) runs every detector in
    `authoring_leaks.WIRED_DETECTORS` over one shared `HeadView`, with zero spawns; a
    detector that fails to import or raises refuses the commit.
    """
    gate_paths = list(paths) + list(deleted_paths)
    if not gate_paths:
        return None

    # `declared_deletion_gate` takes the declaration the other gates do not, so it runs outside the
    # `(name, gate)` loop, explicitly and BEFORE `commit_paths`: a refusal after the commit is none.
    deletion_outcome = declared_deletion_gate(worktree_root, gate_paths, deleted_paths)
    if not deletion_outcome.passed:
        return "declared_deletion_gate: " + "; ".join(deletion_outcome.diagnostics[:5])

    # Named explicitly rather than read off `gate.__name__`: a wrapped gate would report `<lambda>`.
    for name, gate in (
        ("carry_gate", carry_gate),
        ("residue_gate", residue_gate),
        ("op_scope_coverage_gate", partial(op_scope_coverage_gate, read_source=read_source)),
        ("leak_gate", leak_gate),
        ("registration_quad_gate", partial(registration_quad_gate, read_source=read_source)),
        ("claude_md_budget_gate", partial(claude_md_budget_gate, read_source=read_source)),
        ("machine_path_leak_gate", partial(machine_path_leak_gate, read_source=read_source)),
        ("attribution_gate", partial(
            attribution_gate, vacated=deleted_paths,
            prefer_staged=prefer_staged, deliberate_stage=deliberate_stage,
        )),
    ):
        outcome = gate(worktree_root, gate_paths)
        if not outcome.passed:
            shown = (
                outcome.diagnostics if name == "attribution_gate"
                else outcome.diagnostics[:5]
            )
            return f"{name}: " + "; ".join(shown)

    return None


def _expand_untracked_dirs(worktree_root: Path, untracked: list) -> list:
    dirs = [
        p for p in untracked
        if p.endswith(("/", "\\")) or (worktree_root / p).is_dir()
    ]
    if not dirs:
        return untracked
    # Per-use spawn: runs only for a declared untracked directory (never on the terminal_commit
    # path); one batched spawn, since the HEAD tree under a directory has no in-process reader here.
    result = run_git(
        ["-C", str(worktree_root), "--no-optional-locks", "ls-tree", "-r", "-z",
         "--name-only", "HEAD", "--", *[d.replace("\\", "/") for d in dirs]]
    )
    if result.timed_out or result.returncode != 0:
        return untracked
    expanded = [p for p in result.stdout.split("\0") if p]
    dir_set = set(dirs)
    return [p for p in untracked if p not in dir_set] + expanded


def _drop_missing(worktree_root: Path, paths: list) -> tuple:
    absent = [p for p in paths if not (worktree_root / p).exists()]
    if not absent:
        return list(paths), []
    partition = partition_declared_deletions(worktree_root, absent)
    if partition is None:
        return list(paths), []
    gone = set(partition[1])
    return [p for p in paths if p not in gone], [p for p in paths if p in gone]


def _split_index_deleted(worktree_root: Path, paths: list) -> tuple:
    """(kept, untracked): on-disk HEAD-tracked paths absent from the index move to untracked, so a `git rm --cached` lands; zero spawns; any read failure leaves `paths` as given."""
    present = [p for p in paths if (worktree_root / p).is_file()]
    if not present:
        return list(paths), []
    try:
        partition = partition_declared_deletions(worktree_root, present)
        if partition is None or not partition[0]:
            return list(paths), []
        in_index = read_index_paths(worktree_root)
    except Exception:  # noqa: BLE001
        return list(paths), []
    gone = {p for p in partition[0] if p.replace("\\", "/") not in in_index}
    return [p for p in paths if p not in gone], [p for p in paths if p in gone]


def _split_gone_tracked(worktree_root: Path, paths: list) -> tuple:
    """(kept, deleted): `paths` entries absent from disk but tracked at HEAD move to
    `deleted`. Never raises; an unresolvable HEAD spine leaves `paths` untouched."""
    absent = [p for p in paths if not (worktree_root / p).exists()]
    if not absent:
        return list(paths), []
    partition = partition_declared_deletions(worktree_root, absent)
    if partition is None:
        return list(paths), []
    gone = set(partition[0])
    return [p for p in paths if p not in gone], [p for p in paths if p in gone]


def _ignored_untracked(worktree_root: Path, paths: list) -> list:
    present = [p for p in paths if (worktree_root / p).is_file()]
    partition = partition_declared_deletions(worktree_root, present)
    if partition is None or not partition[1]:
        return []
    candidates = partition[1]
    # Per-use spawn: ignore rules (.gitignore stack, info/exclude, core.excludesFile) have no
    # in-process evaluator; one batched spawn for all candidates, skipped when the caller supplies
    # `ignored_untracked` (terminal_commit does, from its status snapshot).
    result = run_git(
        ["-C", str(worktree_root), "--no-optional-locks", "check-ignore", "-z", "--stdin"],
        input=b"\0".join(p.replace("\\", "/").encode("utf-8") for p in candidates),
        binary=True,
    )
    if result.timed_out or result.returncode not in (0, 1):
        return []
    ignored = {s.decode("utf-8", "surrogateescape") for s in result.stdout_bytes.split(b"\0") if s}
    return [p for p in candidates if p.replace("\\", "/") in ignored]


def _error(message: str, **extra: object) -> dict:
    """Build the structured-error result envelope for this op.

    Purpose: uniform fail-loud shape -- contract fields present with
    "committed" false and "sha" null, plus "error" naming what happened.
    """
    result: dict = {"committed": False, "sha": None, "error": message}
    result.update(extra)
    return result


_RETRY_LANDED_WINDOW_COMMITS = 20
_RETRY_LANDED_WINDOW_SECS = 900  # a retry follows its timeout within seconds; bounds adopting an old commit


def _message_identity(text: str) -> list:
    return [
        ln.rstrip()
        for ln in text.splitlines()
        if ln.strip() and not ln.startswith(ABSORBED_PEER_CLAIM_PREFIX)
    ]


def _landed_by_predecessor(worktree_root: Path, message: str) -> Optional[str]:
    """Sha of a recent commit whose message equals `message`, else None; never raises."""
    want = _message_identity(message)
    try:
        # Per-use spawn: runs only after NothingToCommit (a retried landing), where one bounded
        # `git log` message read is the sole way to find the predecessor commit.
        result = run_git(
            ["log", f"-n{_RETRY_LANDED_WINDOW_COMMITS}", "--format=%x1e%H%x1f%ct%x1f%B", "HEAD"],
            cwd=str(worktree_root),
        )
    except Exception:  # noqa: BLE001 -- the plain refusal is the fallback
        return None
    if result.returncode != 0:
        return None
    now = time.time()
    for record in result.stdout.split("\x1e"):
        parts = record.split("\x1f", 2)
        if len(parts) != 3:
            continue
        sha, committed_at, body = parts
        try:
            age = now - int(committed_at)
        except ValueError:
            continue
        if age > _RETRY_LANDED_WINDOW_SECS:
            return None
        if _message_identity(body) == want:
            return sha.strip()
    return None


_PROCESS_SUBJECT_SUFFIX = " (process files)"


class _Landing(NamedTuple):
    """One `commit_paths` attempt. `error` is set on a refusal; `predecessor` names a recent
    commit carrying this exact message when the attempt found nothing left to commit."""

    outcome: Optional[CommitOutcome]
    stale_warning: Optional[str]
    stale_paths: list
    error: Optional[dict]
    predecessor: Optional[str]


def _process_message(message: str) -> str:
    """`message` with `_PROCESS_SUBJECT_SUFFIX` on its subject line; trailers untouched."""
    subject, sep, rest = message.partition("\n")
    return subject.rstrip() + _PROCESS_SUBJECT_SUFFIX + sep + rest


def _half_expectations(
    expected: dict, h_paths: list, half_index: int, mixed: bool, all_paths: list
) -> dict:
    """The expectation keys one commit half owns. A key in no half's paths goes to the first,
    where `commit_paths` refuses it as outside the pathspec."""
    if not mixed or not expected:
        return expected
    own = {p.replace("\\", "/") for p in h_paths}
    if half_index == 0:
        known = {p.replace("\\", "/") for p in all_paths}
        return {k: v for k, v in expected.items() if k in own or k not in known}
    return {k: v for k, v in expected.items() if k in own}


def _land(
    worktree_root: Path,
    paths: list,
    message: str,
    *,
    deleted: list,
    untracked: list,
    prefer_staged: list,
    deliberate_stage: bool,
    declared_reverts: list,
    expected_blobs: Optional[dict] = None,
    expected_content_sha256: Optional[dict] = None,
    allow_empty: bool = False,
    finish_merge: bool = False,
) -> _Landing:
    """One `commit_paths` attempt; `allow_empty` is tried only after `NothingToCommit` with no predecessor."""

    def _attempt(empty_ok: bool) -> CommitOutcome:
        return commit_paths(
            worktree_root,
            paths,
            message,
            deleted_paths=deleted,
            untracked_paths=untracked,
            prefer_staged=prefer_staged,
            prefer_deliberate_stage=deliberate_stage,
            allow_empty=empty_ok,
            finish_merge=finish_merge,
            blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=worktree_root),
            detect_rollback=True,
            declared_reverts=declared_reverts,
            expected_blobs=expected_blobs or None,
            expected_content_sha256=expected_content_sha256 or None,
        )

    try:
        try:
            outcome = _attempt(False)
        except NothingToCommit:
            if not allow_empty or _landed_by_predecessor(worktree_root, message) is not None:
                raise
            outcome = _attempt(True)
    except BlobDriftRefused as exc:
        return _Landing(None, None, [], _error(
            str(exc),
            refused="blob-drift",
            blob_drift=[
                {"path": d.path, "basis": d.basis, "expected": d.expected, "actual": d.actual}
                for d in exc.drift
            ],
        ), None)
    except NothingToCommit as exc:
        # `already_landed_sha`: a recent commit with this exact message (Session-Id included).
        landed = _landed_by_predecessor(worktree_root, message)
        if landed is not None:
            return _Landing(None, None, [], _error(
                f"{exc} The same message already landed as {landed}; an earlier call of "
                "this op committed it.",
                nothing_to_commit=True,
                already_landed_sha=landed,
            ), landed)
        return _Landing(None, None, [], _error(str(exc), nothing_to_commit=True), None)
    except IndexStaleAfterCommit as exc:
        # THE COMMIT LANDED. An error here (`committed: False`) makes the caller commit twice;
        # a peer holding `.git/index.lock` lands here routinely. Recovered as a SUCCESS with a warning.
        # `cast`: `IndexStaleAfterCommit.outcome` is typed `object` to avoid an import cycle.
        if exc.outcome is None:
            # No raiser on the commit path omits it; if one ever does, say so
            # rather than inventing a sha -- but never downgrade to `_error`,
            # because the commit landed either way.
            return _Landing(None, None, [], _error(
                f"{exc} (the outcome was not carried out of the raise site, so "
                "no sha can be reported -- resolve it with `git log -1`)"
            ), None)
        return _Landing(cast(CommitOutcome, exc.outcome), str(exc), list(exc.paths), None, None)
    except IndexParseError as exc:
        return _Landing(None, None, [], _error(
            f"{exc}. Nothing committed; resolve any unmerged path, or convert a v4 "
            "index with `git update-index --index-version 2`."
        ), None)
    except (CommitRefused, FilterUnsupported) as exc:
        return _Landing(None, None, [], _error(str(exc)), None)
    return _Landing(outcome, None, [], None, None)


def _record_ledger(worktree_root: Path, paths: list, sha: str, message: str) -> None:
    # Local import for the module-cycle reason the sibling call sites document.
    from coordinator_core.contract.apply_base import record_ledger_entry
    from coordinator_core.git.commit_trailers import extract_closure_facts_from_text

    # Line-anchored read of the raw message that became the commit object, so a demoted
    # `Closes:` still records (C1's contract).
    closes, reverts_sha = extract_closure_facts_from_text(message)
    record_ledger_entry(worktree_root, paths, sha, closes=closes, reverts_sha=reverts_sha)


REPLY_LIST_CAP = 20


def _cap_list(items: list) -> tuple:
    return items[:REPLY_LIST_CAP], len(items)


def _cap_mapping_lists(block: dict) -> dict:
    out: dict = {}
    for key, value in block.items():
        if isinstance(value, list) and len(value) > REPLY_LIST_CAP:
            out[key], out[f"{key}_total"] = _cap_list(value)
        elif isinstance(value, dict) and len(value) > REPLY_LIST_CAP:
            out[key] = dict(list(value.items())[:REPLY_LIST_CAP])
            out[f"{key}_total"] = len(value)
        else:
            out[key] = value
    return out


def cap_reply(result: dict) -> dict:
    """Cap every reply list at REPLY_LIST_CAP with a `<key>_total` sibling, nested `own_left_behind` and `stopped_run_residue` blocks included; `full_reply: true` skips it."""
    if not isinstance(result, dict):
        return result
    out = _cap_mapping_lists(result)
    for key in ("own_left_behind", "stopped_run_residue"):
        block = out.get(key)
        if isinstance(block, dict):
            inner = _cap_mapping_lists(block)
            carried = inner.get("carried")
            if key == "stopped_run_residue" and isinstance(carried, dict):
                total = sum(len(v) for v in carried.values() if isinstance(v, list))
                if total > REPLY_LIST_CAP:
                    inner["carried"] = {
                        rid: ps[:REPLY_LIST_CAP] if isinstance(ps, list) else ps
                        for rid, ps in carried.items()
                    }
                    inner["carried_total"] = total
            out[key] = inner
    return out


@register_op("ceremony.commit_v2")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    ledger = PhaseLedger()
    spawn_start = spawn_counter.spawn_count()
    t0 = time.thread_time()
    try:
        reply = _handler_body(params, repo_root, ledger)
        if isinstance(params, dict) and params.get("full_reply") is True:
            return reply
        return cap_reply(reply)
    finally:
        with contextlib.suppress(Exception):
            total_ms = (time.thread_time() - t0) * 1000.0
            ledger._ms["unbracketed"] = max(0.0, total_ms - sum(ledger._ms.values()))
            declared = params.get("paths") if isinstance(params, dict) else None
            ledger.emit(
                repo_root,
                abandoned=dispatch_abandon.is_abandoned(),
                paths=len(declared) if isinstance(declared, list) else 0,
                spawns=spawn_counter.spawn_count() - spawn_start,
            )


def _handler_body(params: dict, repo_root: Optional[Path], ledger: PhaseLedger) -> dict:
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
                                       as removed in this commit. A path still
                                       present in the worktree is refused.
        untracked_paths (list[str], optional) -- paths this commit removes from HEAD and the index
                                       while the worktree keeps the file (`git rm --cached`).
                                       Declared, never inferred: the phantom-deletion refusal cannot
                                       tell this from a stale shared index.
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
        allow_empty (bool, optional, DEFAULT FALSE) -- land ONE marker commit (tree equal to
                                       HEAD's) when every declared path already matches HEAD.
                                       Tried only after nothing-to-commit and no recent commit
                                       carries this message. A mixed set lands at most one marker.
        finish_merge (bool, optional, DEFAULT FALSE) -- with MERGE_HEAD present and every conflict
                                       resolved, land the merge as one two-parent commit over
                                       `paths`, never split into halves; reply has `merge_parent`.
        session_id (str, optional)  -- canonical UUID (8-4-4-4-12 hex,
                                       case-insensitive) threaded into
                                       `apply_missing_trailers` as
                                       `session_id_override`, taking
                                       precedence over the env-ladder
                                       resolution for the Session-Id trailer.
                                       Malformed (present but not UUID-
                                       shaped) refuses the whole call rather
                                       than silently falling back to the env
                                       ladder. Absent -- behaviour unchanged
                                       from before this parameter existed.
                                       Exists because dispatched git-commit-
                                       agent commits were landing with no
                                       Session-Id trailer under the env-
                                       ladder alone, blinding the review-
                                       brightline gate (coordinator-claude#52a).
        declared_reverts (list[str], optional) -- paths deliberately reverted to an older exact blob;
                                       excluded from `commit_paths`'s staged-rollback check
                                       (`detect_rollback=True`), which otherwise raises
                                       `StagedRollbackRefused`, returned as a structured refusal.
        ignored_untracked (list[str], optional) -- caller-read subset of `paths` that is gitignored and
                                       untracked; when present no `check-ignore` runs.
    Returns:
        {"committed": True, "sha": str, "tree": str|None (tree of the landed commit), "staged_preferred": [str, ...],
         "worktree_over_staged": [str, ...], "warnings": [str, ...],
         "guard_class_relay": {"transitions": [...], "skips": [...]}} on
        success -- `warnings` is non-empty exactly when
        `worktree_over_staged` is, and says the same thing in the register
        an operator reads. `guard_class_relay` is the C2 step (docs/plans/
        2026-08-29-a-guard-class-flip-announces-itself.md): a detected
        `write_guards` module CLASS transition in this commit, per path
        under `coordinator_core/write_guards/`. It never gates or delays
        the commit above -- a step failure degrades to a `skips` entry.
        `warnings` also carries one entry per named path a live peer session holds a touch-claim on;
        never a refusal (see `_peer_claim_warnings`), plus one entry per live peer naming the
        untracked files it holds that this commit leaves behind.
        Or
        {"committed": False, "sha": None, "error": str} on any
        structured refusal (an empty pathspec, a directory in `paths`, an unresolvable CAS ref, a lost
        CAS race, or a path needing a checkin conversion neither this module nor its `blob_fallback` can reproduce).

    on_main_reason (str, optional) -- the one explicit door past the refusal of a commit while HEAD is
    `main` and a remote is configured; a non-empty reason admits, never ambient. In-process callers of
    `_handler` (bookkeeping commits) get the same refusal on a remote-backed `main` unless they pass it.

    carry_own_left_behind (bool, default false) adds own dirty claimed paths no live peer holds; reply `own_left_behind`.
    carry_stopped_runs (bool, default true) adds the dirty, uncontested run artifacts of this session's
    runs that ended on evidence; reply `stopped_run_residue`, one `Stopped-Run-Residue:` trailer per run.
    skip_missing (bool, default false) drops `paths` absent from disk and HEAD, as `skipped_missing`.
    gone_tracked_as_deleted (bool, default false) reclassifies `paths` entries gone from the worktree
    but tracked at HEAD as declared deletions. For a caller whose `paths` is itself the declaration
    (a wave's landed rows' writes); a caller with undeclared entries must not set it.
    Gitignored-untracked `paths` refuse; `force_ignored` (list) commits them, `drop_ignored` (bool) drops them.

    expected_blobs ({path: git blob sha}, optional) -- the blob the caller reviewed for each path.
    expected_content_sha256 ({path: sha256 of the file bytes}, optional) -- the bytes the caller
    recorded writing. `commit_paths` compares both before any write. A path that drifted refuses
    the call with {"committed": False, "sha": None, "error": str, "refused": "blob-drift",
    "blob_drift": [{"path", "basis", "expected", "actual"}, ...]}; `actual` is null when the
    committed bytes are not worktree bytes read here. A mixed process/product call gives each
    half only the keys in its own paths. Absent, the reply is unchanged.

    Keying scope: common_dir -- repo_root arg is the .git common dir; the
    caller's worktree is main_worktree_root(repo_root).
    """
    if "repo" in params:
        return _error(
            "params.repo is not a parameter of this op. The target repo is keyed "
            "from the calling worktree (_OP_KEY_SCOPE \"common_dir\"), never from "
            "params; `repo_root` is a consistency assertion only. Dispatch from "
            "the target repo instead. Accepted silently, `repo` committed against "
            "the caller's own tree while naming another."
        )

    if repo_root is None:
        return _error(
            "ceremony.commit_v2 requires a common_dir-keyed dispatch; "
            "repo_root (git common dir) was not supplied"
        )

    # `ops.fleet._common` is a heavy closure; an absent `repo_root` param needs none of it.
    if params.get("repo_root") is not None:
        from coordinator_core.ops.fleet._common import check_repo_root

        d3_mismatch = check_repo_root(params.get("repo_root"), repo_root)
        if d3_mismatch is not None:
            return _error(d3_mismatch)

    raw_paths = params.get("paths") or []
    if not isinstance(raw_paths, list) or not all(isinstance(p, str) for p in raw_paths):
        return _error("params.paths must be a list of strings")

    raw_deleted = params.get("deleted_paths") or []
    if not isinstance(raw_deleted, list) or not all(isinstance(p, str) for p in raw_deleted):
        return _error("params.deleted_paths must be a list of strings")

    raw_untracked = params.get("untracked_paths") or []
    if not isinstance(raw_untracked, list) or not all(
        isinstance(p, str) for p in raw_untracked
    ):
        return _error("params.untracked_paths must be a list of strings")

    message = params.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error("params.message is required and must be a non-empty string")

    # Before trailers are appended, or a trailer-only message would pass.
    if message_missing_subject(message):
        return _error(
            "commit message has no subject line (first line is a trailer). "
            "Put a one-line subject first."
        )

    raw_prefer_staged = params.get("prefer_staged") or []
    if not isinstance(raw_prefer_staged, list) or not all(
        isinstance(p, str) for p in raw_prefer_staged
    ):
        return _error("params.prefer_staged must be a list of strings")

    raw_prefer_deliberate_stage = params.get("prefer_deliberate_stage", False)
    if not isinstance(raw_prefer_deliberate_stage, bool):
        return _error("params.prefer_deliberate_stage must be a boolean")
    raw_allow_empty = params.get("allow_empty", False)
    if not isinstance(raw_allow_empty, bool):
        return _error("params.allow_empty must be a boolean")
    raw_finish_merge = params.get("finish_merge", False)
    if not isinstance(raw_finish_merge, bool):
        return _error("params.finish_merge must be a boolean")

    session_id_override = params.get("session_id")
    if session_id_override is not None:
        if not isinstance(session_id_override, str) or not _UUID_RE.fullmatch(
            session_id_override
        ):
            return _error("params.session_id must be a canonical UUID string")

    raw_declared_reverts = params.get("declared_reverts") or []
    if not isinstance(raw_declared_reverts, list) or not all(
        isinstance(p, str) for p in raw_declared_reverts
    ):
        return _error("params.declared_reverts must be a list of strings")

    expectations: dict = {}
    for name in ("expected_blobs", "expected_content_sha256"):
        raw = params.get(name)
        if raw is None:
            expectations[name] = {}
            continue
        if not isinstance(raw, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
        ):
            return _error(f"params.{name} must be an object of string path to string sha")
        expectations[name] = {k.replace("\\", "/"): v for k, v in raw.items()}

    # A list copied from CRLF output carries a trailing CR no path can end in.
    raw_paths = [p.rstrip("\r") for p in raw_paths]
    raw_deleted = [p.rstrip("\r") for p in raw_deleted]
    raw_untracked = [p.rstrip("\r") for p in raw_untracked]
    raw_prefer_staged = [p.rstrip("\r") for p in raw_prefer_staged]
    raw_declared_reverts = [p.rstrip("\r") for p in raw_declared_reverts]

    raw_skip_missing = params.get("skip_missing", False)
    if not isinstance(raw_skip_missing, bool):
        return _error("params.skip_missing must be a boolean")

    raw_carry = params.get("carry_own_left_behind", False)
    if not isinstance(raw_carry, bool):
        return _error("params.carry_own_left_behind must be a boolean")

    raw_carry_stopped = params.get("carry_stopped_runs", True)
    if not isinstance(raw_carry_stopped, bool):
        return _error("params.carry_stopped_runs must be a boolean")

    raw_gone_as_deleted = params.get("gone_tracked_as_deleted", False)
    if not isinstance(raw_gone_as_deleted, bool):
        return _error("params.gone_tracked_as_deleted must be a boolean")

    worktree_root = main_worktree_root(repo_root)

    on_main_reason = params.get("on_main_reason")
    if on_main_reason is not None and not isinstance(on_main_reason, str):
        return _error("params.on_main_reason must be a string")
    with ledger.phase("main_commit_refusal"):
        main_refusal = main_commit_refusal(worktree_root, override_reason=on_main_reason)
    if main_refusal is not None:
        return _error(main_refusal)

    reclassified: list = []
    if raw_gone_as_deleted and raw_paths:
        raw_paths, reclassified = _split_gone_tracked(worktree_root, raw_paths)
        raw_deleted = list(raw_deleted) + [p for p in reclassified if p not in raw_deleted]

    skipped_missing: list = []
    if raw_skip_missing and raw_paths:
        raw_paths, skipped_missing = _drop_missing(worktree_root, raw_paths)
        if not (raw_paths or raw_deleted or raw_untracked):
            return _error(
                "every declared path is absent from disk and HEAD, nothing to commit.",
                nothing_to_commit=True,
                skipped_missing=skipped_missing,
            )

    with ledger.phase("index_deleted_present"):
        raw_paths, index_untracked = _split_index_deleted(worktree_root, raw_paths)
    raw_untracked = list(raw_untracked) + [p for p in index_untracked if p not in raw_untracked]

    with ledger.phase("expand_untracked_dirs"):
        raw_untracked = _expand_untracked_dirs(worktree_root, raw_untracked)

    force_ignored = params.get("force_ignored") or []
    if not isinstance(force_ignored, list) or not all(isinstance(p, str) for p in force_ignored):
        return _error("params.force_ignored must be a list of strings")
    exempt = {p.replace("\\", "/") for p in force_ignored}
    raw_drop_ignored = params.get("drop_ignored", False)
    if not isinstance(raw_drop_ignored, bool):
        return _error("params.drop_ignored must be a boolean")
    supplied_ignored = params.get("ignored_untracked")
    if supplied_ignored is None:
        with ledger.phase("ignored_untracked"):
            found_ignored = _ignored_untracked(worktree_root, raw_paths)
    elif not isinstance(supplied_ignored, list) or not all(
        isinstance(p, str) for p in supplied_ignored
    ):
        return _error("params.ignored_untracked must be a list of strings")
    else:
        supplied = {p.replace("\\", "/") for p in supplied_ignored}
        found_ignored = [p for p in raw_paths if p.replace("\\", "/") in supplied]
    ignored = [p for p in found_ignored if p not in exempt]
    dropped_ignored: list = []
    if ignored and raw_drop_ignored:
        dropped_ignored = ignored
        raw_paths = [p for p in raw_paths if p not in set(ignored)]
        if not (raw_paths or raw_deleted or raw_untracked):
            shown = ", ".join(ignored[:5]) + (", ..." if len(ignored) > 5 else "")
            return _error(
                f"every path is gitignored and untracked, nothing left to commit: {shown}.",
                dropped_ignored=dropped_ignored,
            )
        ignored = []
    if ignored:
        shown = ", ".join(ignored[:5]) + (", ..." if len(ignored) > 5 else "")
        return _error(
            f"{len(ignored)} path(s) are gitignored and untracked: {shown}. "
            "Drop them from `paths`, or `git add -f` one deliberately."
        )

    # An untracked path leaves HEAD like a deleted one: downstream consumers read the union.
    raw_removed = list(raw_deleted) + list(raw_untracked)

    # Filter FIRST: no path under `_GUARD_MODULE_DIR` means zero work below.
    guard_paths = _guard_module_paths(raw_paths, raw_removed)
    with ledger.phase("pre_commit_guard_sources"):
        pre_commit_guard_sources = (
            _pre_commit_guard_sources(worktree_root, guard_paths) if guard_paths else {}
        )

    # A live peer holding a named path is reported, never refused.
    peer_holds: list = []
    abandoned_holds: list = []
    held_by: dict = {}
    with ledger.phase("peer_claim_warnings"):
        peer_claim_warnings = _peer_claim_warnings(
            worktree_root, list(raw_paths) + raw_removed, peer_holds, own_sid=session_id_override,
            abandoned_holds=abandoned_holds, held_out=held_by,
        )
    own_sid = session_id_override
    if own_sid is None:
        with ledger.phase("resolve_session_id"):
            try:
                own_sid = session_core.resolve_session_id(str(worktree_root))
            except Exception:  # noqa: BLE001
                own_sid = None
    # own_sid now reaches _peer_claim_warnings above, so the session's own holds drop out of peer warnings.
    with ledger.phase("own_left_behind"):
        free, contested = _own_left_behind(
            worktree_root, held_by, own_sid, [*raw_paths, *raw_removed]
        )
    stopped_carried: dict = {}
    stopped_withheld: dict = {}
    stopped_error: Optional[str] = None
    # A checkpoint lands its wave's declared footprint only: a sibling run's residue is not it.
    if raw_carry_stopped and own_sid and not checkpoint_guard._SUBJECT_RE.match(
        message.split("\n", 1)[0].strip()
    ):
        with ledger.phase("stopped_runs_residue"):
            try:
                from coordinator_core.ops.dispatch_emit import stopped_runs

                stopped = stopped_runs.residue(
                    worktree_root, own_sid, [*raw_paths, *raw_removed]
                )
                stopped_carried = {rid: list(ps) for rid, ps in stopped.carried.items()}
                stopped_withheld = dict(stopped.withheld)
            except Exception as exc:  # noqa: BLE001
                stopped_carried, stopped_withheld = {}, {}
                stopped_error = f"stopped-run residue unavailable ({type(exc).__name__}); nothing carried"
    # The session's own `.git/` scratch (review-targets.txt) sits in its touch list but never
    # in a tree: carrying it makes commit_paths refuse the whole commit.
    free = [p for p in free if canonical_repo_relative_path_refusal(p) is None]
    stopped_carried = {
        rid: [p for p in ps if canonical_repo_relative_path_refusal(p) is None]
        for rid, ps in stopped_carried.items()
    }
    stopped_flat = list(dict.fromkeys(p for ps in stopped_carried.values() for p in ps))
    carried: list = []
    own_paths = list(raw_paths)
    if (raw_carry and free) or stopped_flat:
        probe = list(dict.fromkeys([*(free if raw_carry else ()), *stopped_flat]))
        # A supplied set answers only for `paths`; carried candidates sit outside it.
        asked = {p.replace("\\", "/") for p in raw_paths} if supplied_ignored is not None else set()
        skip = {p.replace("\\", "/") for p in supplied_ignored or ()}
        unasked = [p for p in probe if p.replace("\\", "/") not in asked]
        with ledger.phase("ignored_untracked"):
            skip |= {p.replace("\\", "/") for p in _ignored_untracked(worktree_root, unasked)} if unasked else set()
        if raw_carry:
            carried = [p for p in free if p not in skip or p in exempt]
        kept_stopped = {p for p in stopped_flat if p not in skip or p in exempt}
        stopped_carried = {
            rid: [p for p in ps if p in kept_stopped] for rid, ps in stopped_carried.items()
        }
        stopped_carried = {rid: ps for rid, ps in stopped_carried.items() if ps}
        have = set(raw_paths)
        raw_paths = (
            list(raw_paths) + carried
            + [p for p in stopped_flat if p in kept_stopped and p not in have and p not in carried]
        )
    left = [p for p in free if p not in carried] + contested

    # Gates run BEFORE the commit lands -- they are refusals, and a refusal
    # after the fact is not one. Contrast `_guard_class_relay_step` below,
    # which runs after and is forbidden from failing the commit.
    with ledger.phase("pre_commit_gates"):
        read_source = post_commit_reader(
            worktree_root,
            resolve_git_common_dir(worktree_root),
            paths=raw_paths,
            deleted_paths=raw_removed,
            prefer_staged=raw_prefer_staged,
            prefer_deliberate_stage=raw_prefer_deliberate_stage,
        )
        gate_refusal = _pre_commit_gates(
            worktree_root, raw_paths, raw_removed, read_source,
            prefer_staged=raw_prefer_staged,
            deliberate_stage=raw_prefer_deliberate_stage,
        )
    carry_withheld_note: Optional[str] = None
    if gate_refusal is not None:
        # TRAP: the carry is opportunistic and must never cost the run its own landing. A
        # refusal with something carried is re-judged over the caller's own paths; the second
        # pass is paid only on a refusal that was already going to stop the commit.
        if not ((carried or stopped_flat) and (own_paths or raw_removed)):
            return _error(gate_refusal)
        with ledger.phase("pre_commit_gates_own"):
            own_reader = post_commit_reader(
                worktree_root,
                resolve_git_common_dir(worktree_root),
                paths=own_paths,
                deleted_paths=raw_removed,
                prefer_staged=raw_prefer_staged,
                prefer_deliberate_stage=raw_prefer_deliberate_stage,
            )
            own_refusal = _pre_commit_gates(
                worktree_root, own_paths, raw_removed, own_reader,
                prefer_staged=raw_prefer_staged,
                deliberate_stage=raw_prefer_deliberate_stage,
            )
        if own_refusal is not None:
            return _error(own_refusal)
        stopped_withheld.update({p: "gate-refused" for p in stopped_flat})
        left = left + carried
        carried = []
        stopped_carried = {}
        raw_paths = own_paths
        carry_withheld_note = "carried residue withheld: " + gate_refusal.splitlines()[0][:200]

    # Declared-vs-actual EOL repair for the executables this commit touches (`docs/reference/eol-drift-detection.md`).
    # Filter-first: no `.cmd`/`.ps1`/`.sh`/`.bat` means no spawn and no read. Runs BEFORE the commit; not a
    # refusal. Wrapped: a repair-side exception is worse than the drift. `prefer_staged` paths are excluded.
    with ledger.phase("eol_drift"):
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
    # `commit_paths`' hand-rolled object-write plumbing, which fires NO git
    # hooks (`prepare-commit-msg` included), so this call is this route's
    # ONLY attach point (docs/dispatch-briefs/.../C3.md; commit_trailers.py
    # module docstring). Never blocks: `apply_missing_trailers` degrades to
    # `message` unchanged on any resolution failure.
    #
    # `commit_paths` honours `commit.gpgsign`; a signing failure lands the commit unsigned and
    # `outcome.sign_warning` is surfaced into `warnings` below.
    message = _disclose_peer_holds(message, peer_holds + abandoned_holds)
    message = _disclose_gone_deletions(message, reclassified)
    message = _stopped_run_trailers(message, list(stopped_carried))
    with ledger.phase("apply_missing_trailers"):
        message = apply_missing_trailers(
            message,
            worktree_root,
            list(raw_paths) + raw_removed,
            session_id_override=session_id_override,
        )

    with ledger.phase("skill_weight_check"):
        skill_weight_refusal, skill_weight_note = skill_weight_check_worktree(
            worktree_root, raw_paths, raw_deleted
        )
    if skill_weight_refusal is not None:
        return _error(skill_weight_refusal)

    with ledger.phase("publish_allowlist_drift"):
        allowlist_drift = publish_allowlist_drift_refusal(
            worktree_root, {p: ("", "") for p in [*raw_paths, *raw_untracked]}
        )
    if allowlist_drift is not None:
        return _error(allowlist_drift)

    # Process files (see `_PROCESS_PREFIXES`) never ride in a
    # product commit on a non-DoE repo: a mixed call lands product first, process second.
    prod_paths, proc_paths = split_process_paths(worktree_root, raw_paths)
    prod_deleted, proc_deleted = split_process_paths(worktree_root, raw_deleted)
    prod_untracked, proc_untracked = split_process_paths(worktree_root, raw_untracked)
    mixed = bool(prod_paths or prod_deleted or prod_untracked) and bool(
        proc_paths or proc_deleted or proc_untracked
    )
    # A merge is one commit: a finish_merge over a live MERGE_HEAD never splits.
    if mixed and raw_finish_merge and (resolve_git_dir(worktree_root) / "MERGE_HEAD").exists():
        mixed = False
    if mixed:
        proc_set = set(proc_paths) | set(proc_deleted) | set(proc_untracked)
        halves = [
            (prod_paths, prod_deleted, prod_untracked, message),
            (proc_paths, proc_deleted, proc_untracked, _process_message(message)),
        ]
    else:
        proc_set = set()
        halves = [(raw_paths, raw_deleted, raw_untracked, message)]
    if mixed:
        # Each half is checkpoint-checked inside its own landing; judged here over the whole
        # set so a refusal in the second half cannot follow a landed first.
        with ledger.phase("checkpoint_refusal"):
            whole_refusal = checkpoint_guard.checkpoint_refusal(
                worktree_root, resolve_git_common_dir(worktree_root), head_sha(worktree_root),
                message, list(raw_paths),
            )
        if whole_refusal:
            return _error(whole_refusal)

    # (paths + removed, message, landing outcome or None, sha, process half?)
    landed_halves: list = []
    index_stale_warnings: list = []
    index_stale_paths: list = []
    last_error: Optional[dict] = None
    for half_index, (h_paths, h_deleted, h_untracked, h_message) in enumerate(halves):
        is_process = mixed and half_index == 1
        with ledger.phase("land.process" if is_process else "land.product"):
            landing = _land(
                worktree_root,
                h_paths,
                h_message,
                deleted=h_deleted,
                untracked=h_untracked,
                prefer_staged=(
                    [p for p in raw_prefer_staged if (p in proc_set) == is_process]
                    if mixed else raw_prefer_staged
                ),
                deliberate_stage=raw_prefer_deliberate_stage,
                allow_empty=raw_allow_empty and not landed_halves and half_index == len(halves) - 1,
                finish_merge=raw_finish_merge,
                declared_reverts=(
                    [p for p in raw_declared_reverts if (p in proc_set) == is_process]
                    if mixed else raw_declared_reverts
                ),
                **{
                    name: _half_expectations(exp, h_paths, half_index, mixed, [
                        p for h in halves for p in h[0]
                    ])
                    for name, exp in expectations.items()
                },
            )
        h_all = list(h_paths) + list(h_deleted) + list(h_untracked)
        if landing.error is None:
            assert landing.outcome is not None
            landed_halves.append((h_all, h_message, landing.outcome, landing.outcome.sha, is_process))
            if landing.stale_warning is not None:
                index_stale_warnings.append(landing.stale_warning)
                index_stale_paths.extend(landing.stale_paths)
        elif mixed and landing.predecessor is not None:
            # A retry of a mixed call whose half already landed: report it as landed.
            landed_halves.append((h_all, h_message, None, landing.predecessor, is_process))
        elif mixed and landing.error.get("nothing_to_commit"):
            last_error = landing.error
        elif landed_halves:
            unlanded = ", ".join((list(h_paths) + list(h_deleted))[:5])
            for done_paths, done_message, _o, done_sha, _p in landed_halves:
                with ledger.phase("record_ledger"):
                    _record_ledger(worktree_root, done_paths, done_sha, done_message)
                with ledger.phase("release_committed_claims"):
                    _release_committed_claims_step(worktree_root, done_paths)
            return _error(
                f"{landing.error['error']} The product commit {landed_halves[0][3]} landed; "
                f"these process paths did not: {unlanded}. Retry the same call to land them.",
                landed_sha=landed_halves[0][3],
                unlanded_paths=list(h_paths) + list(h_deleted) + list(h_untracked),
                **{k: landing.error[k] for k in ("refused", "blob_drift") if k in landing.error},
            )
        else:
            return landing.error
    if not landed_halves:
        assert last_error is not None
        return last_error

    outcomes = [h[2] for h in landed_halves if h[2] is not None]
    sha = landed_halves[0][3]
    process_sha = next((h[3] for h in landed_halves[1:] if h[4]), None)
    if index_stale_paths:
        # A peer's index lock lasts one write: a single batched retry after the landings usually heals it.
        from coordinator_core.git import commit as _git_commit

        if _git_commit._reset_index_paths(worktree_root, sorted(set(index_stale_paths))):
            index_stale_paths = []
            index_stale_warnings = []
    index_stale_warning = " ".join(index_stale_warnings) or None

    # A FIELD IS NOT A SIGNAL: a commit that sets one side aside still owes the operator a warning.
    warnings = []
    if dropped_ignored:
        shown = ", ".join(dropped_ignored[:5]) + (", ..." if len(dropped_ignored) > 5 else "")
        warnings.append(
            f"dropped {len(dropped_ignored)} gitignored-untracked path(s) from `paths`: {shown}."
        )
    warnings.extend(peer_claim_warnings)
    if carry_withheld_note is not None:
        warnings.append(carry_withheld_note)
    if stopped_error is not None:
        warnings.append(stopped_error)
    warnings.extend(
        f"carried stopped run {rid}'s residue: {len(ps)} file(s)"
        for rid, ps in stopped_carried.items()
    )
    if skill_weight_note is not None:
        warnings.append(skill_weight_note)
    staged_preferred = [p for o in outcomes for p in o.staged_preferred]
    worktree_over_staged = [p for o in outcomes for p in o.worktree_over_staged]
    no_delta = [p for o in outcomes for p in o.no_delta]
    # First-class: the only warning meaning "this commit is not what the branch's protection expects."
    warnings.extend(o.sign_warning for o in outcomes if o.sign_warning is not None)
    if index_stale_warning is not None:
        # First: the commit landed and the index did not, so `git status` misreports these paths.
        warnings.append(index_stale_warning)
    if worktree_over_staged:
        paths = ", ".join(worktree_over_staged[:5])
        if len(worktree_over_staged) > 5:
            paths += ", ..."
        warnings.append(
            f"committed worktree content for {len(worktree_over_staged)} "
            f"path(s) whose index held different content: {paths}. "
            "Pass prefer_staged to name those paths, or "
            "prefer_deliberate_stage=true to preserve every staged blob in "
            "this call -- the latter is the shared-branch answer, where the "
            "differing content is a peer's and you cannot name it up front."
        )

    if no_delta:
        # The k-of-N face of the zero-delta bug, and it belongs in `warnings`
        # for the same reason the divergence above does: the caller named
        # these paths and they are not in the commit, which is indistinguish-
        # able from delivery on the success line alone. Bounded at five like
        # every other join in this envelope.
        # `PhantomDeletionDeclared` is raised before any write, so `no_delta` is only "already at HEAD".
        matched = list(no_delta)
        declared = len(raw_paths) + len(raw_removed)

        if matched:
            paths = ", ".join(matched[:5])
            if len(matched) > 5:
                paths += ", ..."
            warnings.append(
                f"{len(matched)} of {declared} declared path(s) contributed "
                f"nothing -- already at HEAD: {paths}. If you expected a "
                "change there, it landed elsewhere or never landed."
            )

    # Reported because a launcher's bytes changing under an operator is owed disclosure; bounded at five.
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

    # Post-commit: cannot refuse, delay, or fail the landed commit (NEGATIVE SPEC).
    guard_class_relay = _guard_class_relay_step(
        worktree_root, guard_paths, pre_commit_guard_sources, sha,
        repo_root=repo_root,
    )

    # Post-commit, same negative spec. Release set includes outcome.no_delta members.
    with ledger.phase("release_committed_claims"):
        _release_committed_claims_step(
            worktree_root, list(raw_paths) + raw_removed
        )

    for done_paths, done_message, _o, done_sha, _p in landed_halves:
        with ledger.phase("record_ledger"):
            _record_ledger(worktree_root, done_paths, done_sha, done_message)

    # Mark the repo for the next push sweep: mark only, never push.
    # `sys.modules` gate: importing the cadence here would charge a cold commit its import graph.
    cadence = sys.modules.get("coordinator_core.warm.push_cadence")
    if cadence is not None:
        cadence.note_commit(worktree_root)

    checkpoint_stamp: dict = {}
    if "Checkpoint-Plan: " in message:
        # Literal mirrors checkpoint_stamp.CHECKPOINT_NOT_LANDED_TRAILER: that module is imported lazily.
        if "\nCheckpoint-Not-Landed: " in message:
            checkpoint_stamp = {"stamp_deferred": "run-open"}
        else:
            try:
                from coordinator_core.ops.dispatch_emit.checkpoint_stamp import stamp_checkpoint

                checkpoint_stamp = stamp_checkpoint(
                    worktree_root, message, sha, session_id=session_id_override
                )
            except Exception as exc:  # noqa: BLE001 -- the landed checkpoint stands
                checkpoint_stamp = {"stamp_error": repr(exc)}

    landed = commit_meta(resolve_git_common_dir(worktree_root), sha)
    result = {
        "committed": True,
        "sha": sha,
        "tree": landed["tree"] if landed else None,
        "staged_preferred": staged_preferred,
        "worktree_over_staged": worktree_over_staged,
        "no_delta": no_delta,
        "warnings": warnings,
        "guard_class_relay": guard_class_relay,
        "index_stale": index_stale_paths,
        "dropped_ignored": dropped_ignored,
        "skipped_missing": skipped_missing,
    }
    if carried or left:
        result["own_left_behind"] = {"carried": carried, "left": left}
    if stopped_carried or stopped_withheld:
        result["stopped_run_residue"] = {
            "carried": stopped_carried, "withheld": stopped_withheld,
        }
    if checkpoint_stamp:
        result["checkpoint_stamp"] = checkpoint_stamp
    if process_sha is not None:
        result["process_sha"] = process_sha
    merge_parent = next((o.merge_parent for o in outcomes if o.merge_parent), None)
    if merge_parent is not None:
        result["merge_parent"] = merge_parent
    return result
