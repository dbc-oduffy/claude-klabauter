"""
coordinator_core.ops.ceremony.scoped_git_commit — standalone scoped-commit op.

Purpose: registers `ceremony.scoped_git_commit`, a thin wrapper over
`ops/ceremony/commit_pipeline.py::run_commit_pipeline` for callers that just
want "stage these paths, commit with this message, push if there's a
remote" — decoupled from the `wsc_tail` session lifecycle (no receipt, no
`on_committed` sentinel hook, no CALLER-authored deleted/kept-entries
message blocks — see the 2026-08-04 fix below for the one exception).

DEC-3 (docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md):
one wrapper, pointed at all 28 fence-inventory sites tagged `scoped-git-commit`
(24 DELETE-verdict plain `git add && git commit` sites, plus the 4 in-scope
NEW-CLAUDE-KLABAUTER sites this op actually builds for). Deliberately NOT the audit's
leading-`git reset` variant — an unscoped reset un-stages whatever a
concurrent sibling EM session has staged on a shared `work/*` branch, and
`run_commit_pipeline`'s tolerant explicit-stage already covers the same
"caller-supplied paths may already be swept by a peer" case without that
hazard. See DEC-3 for the full rationale and the `scoped-safety-commits.md`
citation.

This op has no real ceremony session backing it, so it mints a private
per-invocation session_id (`scoped-git-commit-<uuid4>`) — never reused
across calls, so it carries no session-registry meaning of its own. It is
still genuinely consumed downstream: `run_commit_pipeline` passes it through
to `_derive_absorbed_peer_claims_trailer` (SC-DR-019) as the `session_id`
used to resolve whether this id names a real session directory (it never
does, being lock-only-shaped), which gates that trailer to its
"undetermined" marker rather than attempting `compute_offer` against an
unregistered id (see `commit_pipeline.py`'s own docstring for that gate).
(2026-08-05, P2 fix: this minted id is passed to `run_commit_pipeline` ONLY
as its `session_id`, never for attribution — the ACTUAL committing
session's resolved identity is threaded separately via `attribution_session_
id`, so `_derive_absorbed_peer_claims_trailer`'s `compute_offer` lookup runs
against a real session, not a lock-only id with no session directory. See
`run_commit_pipeline`'s own docstring for the full split.)
No caller-supplied kept_entries/trailers — those stay `wsc_tail`-specific
concerns this wrapper's callers don't have. `deleted_paths` is NOT
caller-supplied either, but this op DOES now pass one to
`run_commit_pipeline` (2026-08-04 fix, defect A/B — a deletion NAMED in the
caller's own pathspec was silently dropped from the commit set, or the call
refused with `empty-commit-set`, because a deletion could never reach
`commit_paths` at all): it is derived entirely from
`commit_pipeline.explicit_stage`'s own `StageOutcome.deletion_paths` — see
that field's docstring — never authored by this op or its caller. This is
NOT a `wsc_tail`-style caller-declared deletion; it exists solely so
`commit_gates.deletion_block_gate` sees a message that accounts for a
deletion this op's own staging logic already decided belongs in the commit.

Idempotency (AC7, manifest hazard rating: none): a second call with the same
`worktree_root`/`paths`/`message`, after the first landed, returns the benign
`{"committed": False, "sha": None, "pushed": None, "commit_failed": False,
"diagnostics": [], "reason": "empty-commit-set"}` no-op.

CORRECTED 2026-08-03 (live incident, session f2a9e7b3 — three invocations of
`safe-commit-offer` seconds apart; the first landed `7aaf68401`, the second
and third each reported `genuinely failed to commit ... exit_code=1` over
those same already-committed paths, and the operator nearly hand-committed a
duplicate on the strength of that report). The paragraph above previously
claimed this fell out of `run_commit_pipeline`'s empty-`commit_paths`
short-circuit "inherited from `commit_pipeline`, not re-derived here". It did
not, and never had: that short-circuit keys off `compute_commit_paths(
compute_gate_paths(stage.staged_paths, ...))`, and `staged_paths` is every
caller path that EXISTS ON DISK — a committed-and-clean file is still on
disk, so `commit_paths` is non-empty, the pipeline runs the commit step, and
`git commit` exits 1 with its "nothing to commit" no-op, which the pipeline
classifies as `commit_failed=True` like any other non-zero commit exit. The
contract was written and believed but never witnessed by a test.

It IS re-derived here now, by `_commit_paths_are_clean()`: when the commit
STEP itself (never a gate, never staging) returns non-zero and nothing landed,
the caller's own pathspec is re-checked against `git status --porcelain`. If
no path under it has anything left to commit, nothing was lost and the outcome
is the benign no-op above. Anything else — any residual change under the
pathspec, or a git that cannot answer — stays `commit_failed=True`, so a
genuine refusal (a failing `pre-commit` hook, whose paths are still dirty
after the pipeline's rollback) is never laundered into a success.

Spec backlink: pln-coordinator-ops-buildout-from--903224
§ DEC-3, Wave 1 C1d.

Sink-side ownership enforcement: REMOVED (2026-08-08, PM ruling),
REPLACED (C2 of docs/plans/2026-08-08-claim-index-the-commit-gate-never-
had.md), then REMOVED OUTRIGHT (2026-08-13, PM ruling, docs/plans/2026-08-
13-claim-release-deadlock-and-the-doctrine-that-rejects-it.md, C1).

The replacement gate the paragraph above used to describe --
`_check_claim_conflicts()`, composing `claim_index.lookup()` +
`liveness.session_live()` to refuse a commit whenever a live peer's
session had TOUCHED a dirty path in the caller's own pathspec -- is gone.
Path-touch claims are advisory swimlane guidance (they exist so an EM
doesn't accidentally sweep a peer's uncommitted work), not an enforcement
primitive; a mechanism that hard-denies on an advisory signal was the
defect, not a missing escape route from it. See the plan's Problem section
for the two live incidents this caused (a 9-test file left uncommittable
for the rest of a session; a 20-minute block on two read-adjacent files)
and its duration argument: a block may not outlive the operation it
protects, and this one persisted for a session while protecting nothing
git's own index lock (below) does not already protect.

Nothing in this file replaces it as a GATE -- no pause, no prompt, no
control flow gated on anything `claim_index` or `session/scope.py`
answers. C5 (docs/plans/2026-08-13-claim-release-deadlock-and-the-
doctrine-that-rejects-it.md) assessed the substrate's remaining readers
and returned SUBSTRATE SURVIVES (two live readers independent of the
deleted gate), which authorized C1d to build the bounded recency-of-EDIT
warn AC1d called for: `_warn_recent_edits`, which logged (never gated,
paused, or prompted) when a path in the caller's pathspec was EDITED
within 30s by a live peer session -- "someone's hands are on it right now".

That warn was REMOVED 2026-08-19 on latency grounds, and the removal is the
same conclusion its own C1d contract already implied. Its only output was a
`_LOG.warning` line: it never reached this op's response envelope, so no
caller -- CLI, agent, or peer op -- could read it, and its own docstring
required it to swallow every failure rather than affect the commit it
accompanied. It paid a `claim_index.lookup()` index rebuild (~50ms measured)
on every invocation of the hottest op in the engine to produce a fact
nothing consumed; an unread computation is cost, not a cheaper gate.
Nothing about the ownership posture above changes -- the warn never gated,
so its removal removes no enforcement. This is the same counterfactual, on
the same substrate, that `state/kill-ledger.md` K-008 applied to
`_disclose_peer_claims`/`Absorbed-From:` under a PM ruling eight days
earlier; K-008's own "What is NOT touched" paragraph exempted this function
on the grounds that it answers a different question, which remains true and
is beside the point -- an answer no consumer receives is not an answer.
A claim-presence read returns to this commit path only behind a named
consumer whose OUTCOME differs, per that ledger entry's Returns-when.

Sweeping-pathspec rejection SURVIVES, and is independent of ownership:
`.`, `./`, `:/`, `:(top)`, a glob (`*`/`?`/`[`), an empty pathspec element,
the repo root, an ancestor of the repo root, or a `-A`/`-a`/`--all` flag
token is rejected regardless -- a sweeping pathspec is unsafe on a shared
branch whoever owns the files. That check is pure string/path work and
costs nothing.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.git import divergence as git_divergence
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.commit_pipeline import (
    PUSH_MODE_DEFERRED,
    PUSH_MODE_NONE,
    PUSH_MODE_SYNC,
    PUSH_STATUS_DECLINED,
    PUSH_STATUS_FAILED,
    PUSH_STATUS_NO_REMOTE,
    PUSH_STATUS_NOT_ATTEMPTED,
    PUSH_STATUS_PUSHED,
    PUSH_STATUS_UNCONFIRMED,
    PipelineResult,
    run_commit_pipeline,
)
# `classify_surface`/`_is_noise_path` are declared shared primitives on
# `review_brightline_gate` (its own module comment, 2026-08-04 review
# finding) -- reused here for the ledger's per-commit `kind`, mirroring
# `commit_ledger.classify.py`'s identical reuse rather than forking a
# second classifier.
from coordinator_core.ops.review_brightline_gate import _is_noise_path, classify_surface
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

_LOG = logging.getLogger(__name__)

#: Literal pathspec tokens rejected outright, regardless of what they
#: currently happen to resolve to -- git "magic" pathspecs that mean "the
#: whole worktree" (`.`, `./`, `:/`, `:(top)`), and the flag spellings that
#: would make `git add`/`git commit` unscoped if one ever slipped through as
#: a caller-supplied "path" (`-A`/`-a`/`--all`).
_SWEEPING_PATHSPEC_LITERALS = frozenset({".", "./", ":/", ":(top)", "-A", "-a", "--all"})

#: Glob metacharacters that make a pathspec match more than the literal
#: string names -- a caller-supplied glob resolves against whatever is on
#: disk AT COMMIT TIME, the same partial-blanket-add hazard directory
#: pathspecs are rejected for elsewhere in this pipeline (see `git_native.
#: directory_pathspecs`).
_GLOB_CHARS = frozenset("*?[")

#: How many stale-index paths `_reject_stale_index_paths` names before it
#: switches to a count. The 2026-08-20 measurement found 18 on this branch at
#: once; a refusal message that scrolls is one an agent skims past.
_STALE_INDEX_PATHS_SHOWN = 8

def _reject_sweeping_pathspec(paths: List[str], worktree_root: str) -> Optional[str]:
    """Return a human-readable rejection reason for the first sweeping
    pathspec element in `paths`, or `None` if none of them are sweeping.

    This is the structural check that SURVIVED the 2026-08-08 ownership-gate
    excision, and it answers a different question than ownership ever did:
    not "who does this path belong to" but "does this pathspec element name
    more than one path". A sweeping element is unsafe on a shared branch
    whoever owns the files.

    AC3/AC11 (docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-
    that-rejects-it.md, C3): what this protects THAT GIT DOES NOT -- `git
    add .`/`git commit -a` have no concept of "too broad for a shared
    branch"; they stage and commit whatever a wide pathspec resolves to at
    that instant, silently absorbing a concurrent sibling session's
    in-progress edit into this caller's commit. Outlet, no human: the
    caller re-issues the SAME call with an explicit, narrower `paths` list
    it already had (its own edit set) -- this is a single-request reject,
    not a hold; there is nothing to wait out.

    Rejects: an empty/non-string element; one of `_SWEEPING_PATHSPEC_
    LITERALS`; any element containing a glob metacharacter
    (`_GLOB_CHARS`); and any element that resolves (relative to
    `worktree_root`) to the repo root itself or to an ancestor of it --
    covers `.`/`./` structurally as well as `..`, `../..`, and any other
    escaping form. Resolution failures (e.g. an unresolvable `worktree_root`)
    are NOT swallowed into "not sweeping" -- an unresolvable root fails the
    resolution check below and is treated as sweeping (fail-closed; the
    ownership gate downstream will independently reject an unreadable scope
    too, but this check does not rely on that).
    """
    try:
        root_resolved = Path(worktree_root).resolve()
    except OSError:
        return "worktree_root %r could not be resolved" % (worktree_root,)

    for p in paths:
        if not isinstance(p, str) or not p.strip():
            return "empty pathspec element"
        if p in _SWEEPING_PATHSPEC_LITERALS:
            return "sweeping pathspec %r" % (p,)
        if any(ch in p for ch in _GLOB_CHARS):
            return "glob pathspec %r" % (p,)
        try:
            candidate_resolved = (root_resolved / p).resolve()
        except OSError:
            return "pathspec %r could not be resolved" % (p,)
        if candidate_resolved == root_resolved or candidate_resolved in root_resolved.parents:
            return (
                "pathspec %r resolves to the repo root or an ancestor of it" % (p,)
            )
    return None


def _reject_stale_index_paths(paths: List[str], worktree_root: str) -> Optional[str]:
    """Return a rejection reason when any element of `paths` would commit a
    STALE INDEX entry -- worktree identical to HEAD, index not -- or `None`.

    state/bug-backlog/2026-08-19-shared-git-index-holds-stale-pre-head-sn-
    b5b83e42e275.yaml: on this shared tree the index keeps the PRE-commit blob
    for a path a pathspec commit just landed, so the path re-presents as `MM`
    with an empty HEAD->worktree diff and a staged half that REVERTS the commit
    that just landed. Committing it publishes that revert. It happened at HEAD
    on 2026-08-20 (a54addce reverting cd751b79's `ipc.py` fix, restored at
    d55d8e8e) through `session.safe_commit_offer`, which computes its own
    pathspec -- so no operator ever saw the path list to sanity-check it, the
    scoped tests still passed (they read the worktree), and the commit reported
    success naming 14 in-scope files. Nothing in the ordinary signal set can
    catch this; only the commit's own diff shows it.

    "Worktree matches HEAD AND index does not" is never a legitimate commit
    intent, in either direction: it is either the pre-commit blob above (a
    revert) or index-only content whose sole copy is the index (this entry's
    negative_spec names two live instances, +2062 and +1161 lines). Refusing
    is right for both -- the first must not land, and the second must not be
    swept away by a blanket `git restore --staged`.

    Two batched git calls, independent of `len(paths)` -- the amplification
    gate (`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`)
    forbids the per-path probe the bug entry's `repro_steps` describe, and this
    runs on the commit hot path.

    Fails OPEN (returns `None`) when either probe errors -- an unanswerable
    git must not wedge every commit on the box. The stale-index shape is
    persistent, not transient, so a probe that fails now catches it next call.

    Not applied under `stage_patch`: that path stages from a patch file under a
    process-private index and carries its own `stage_from_patch_cas_refusal`,
    so the shared index's residue is not what it commits.
    """
    if not paths:
        return None
    worktree_probe = git_native._git(
        ["-c", "core.quotepath=false", "diff", "--name-only", "HEAD", "--", *paths],
        cwd=worktree_root,
    )
    index_probe = git_native._git(
        [
            "-c", "core.quotepath=false",
            "diff", "--cached", "--name-only", "HEAD", "--", *paths,
        ],
        cwd=worktree_root,
    )
    if not (worktree_probe.ok and index_probe.ok):
        return None

    worktree_changed = {
        line.strip() for line in worktree_probe.stdout.splitlines() if line.strip()
    }
    staged_only = sorted(
        {line.strip() for line in index_probe.stdout.splitlines() if line.strip()}
        - worktree_changed
    )
    if not staged_only:
        return None

    shown = ", ".join(staged_only[:_STALE_INDEX_PATHS_SHOWN])
    if len(staged_only) > _STALE_INDEX_PATHS_SHOWN:
        shown += " (+%d more)" % (len(staged_only) - _STALE_INDEX_PATHS_SHOWN,)
    return (
        "stale index on %d path(s): %s. Their worktree matches HEAD and their "
        "index does not, so committing them publishes the index's version over "
        "landed work. Read `git diff --cached -- <path>` first: it is either a "
        "revert of an already-landed commit (clear it with `git restore "
        "--staged -- <path>`) or content that exists ONLY in the index, which "
        "may be a peer's sole copy. Re-issue without these paths."
        % (len(staged_only), shown)
    )


def _reject_path_shaped_message(message: str) -> Optional[str]:
    """Return a rejection reason if `message` is a FILE PATH rather than a
    subject line, or `None` if it reads as prose.

    Landed 2026-08-04 on a doe-claude-em FYI memo (`2026-08-04-doe-claude-em-
    zero-join-amendment-in-force-and-your-commit-messages.md`): commits
    `fdbff578b7dc` and `40bf1064a124` on `work/machine-b/2026-07-21to26` have
    a `/private/tmp/.../scratchpad/*.txt` path as their subject line and an
    otherwise-empty body. A caller composing a long message in a scratchpad
    file passed that file's PATH as `message`, expecting `git commit -F`
    semantics; this op's contract is `-m` semantics (subject string, with the
    body in `prose`), so it faithfully committed the path. The reasoning
    behind the AC14 discriminator change is unrecoverable from `git log` as a
    result -- the failure is silent at commit time and only legible later, so
    prose telling callers to pass contents discharges nothing. This does.

    The discriminator is deliberately narrow enough that no real subject can
    trip it: a subject containing NO whitespace at all, that also names a
    path separator, and that is either absolute or resolves to a file that
    exists right now. A conventional-commit subject scoped to a directory
    (`fix(ops/ceremony): ...`) carries spaces and is unaffected.

    AC3/AC11: what this protects THAT GIT DOES NOT -- `git commit -m <path>`
    happily commits the literal string as the subject; git has no way to
    know the caller meant `-F <path>` instead, so the failure is silent and
    only legible later reading `git log` (see the 2026-08-04 incident
    above). Outlet, no human: the returned `error` string names the fix
    directly (read the file, pass its first line as `message`, the rest as
    `prose`) -- a single-request reject the caller retries immediately with
    corrected params, never a hold.
    """
    if not isinstance(message, str):
        return None
    candidate = message.strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return None
    if "/" not in candidate and "\\" not in candidate:
        return None
    looks_absolute = Path(candidate).is_absolute()
    try:
        exists_as_file = Path(candidate).is_file()
    except OSError:
        exists_as_file = False
    if not (looks_absolute or exists_as_file):
        return None
    return (
        "'message' is a file path (%r), not a subject line -- this op takes "
        "`git commit -m` semantics, so the path itself would become the "
        "commit subject. Read the file and pass its first line as 'message' "
        "and the remainder as 'prose'." % (candidate,)
    )


def _resolve_committing_session_id(params: dict, worktree_root: str) -> str:
    """Resolve the CALLING session's own identity: an explicit
    `params["session_id"]` override takes precedence over resolving the
    environment's ambient session identity via `session_core.
    resolve_session_id`.

    Review: code-reviewer -- Finding 3, 2026-08-05. Extracted so the
    identical `params.get("session_id") or session_core.resolve_session_id
    (worktree_root)` expression is spelled once rather than repeated at each
    call site. Since the 2026-08-08 ownership-gate excision the sole
    remaining consumer is `_handler`'s post-commit `owner_session_id` for
    `release_committed_claims`.
    """
    return params.get("session_id") or session_core.resolve_session_id(worktree_root)


#: Shared per-directory `git status --porcelain` cache type -- maps a
#: directory-shaped pathspec element to the porcelain lines last probed for
#: it (`None` on a git failure), so `_dirty_tracked_files_under` and
#: `_untracked_files_under` can be handed the SAME cache dict across one
#: `_handler` invocation and each pay the spawn at most once per directory,
#: never twice. See `_directory_porcelain_lines`'s own docstring.
_DirectoryPorcelainCache = Dict[str, Optional[List[str]]]


def _directory_porcelain_lines(
    worktree_root: str, dir_path: str, cache: Optional[_DirectoryPorcelainCache] = None
) -> Optional[List[str]]:
    """Return the raw `git status --porcelain --untracked-files=all` output
    lines for *dir_path*, or `None` on a git failure.

    Review: code-reviewer -- Finding [P2], 2026-08-12. `_dirty_tracked_
    files_under` and `_untracked_files_under` used to each spawn an
    IDENTICAL `git status --porcelain -- <dir_path>` subprocess against the
    same directory -- one classifying everything-but-`??`, the other only
    `??` -- doubling git spawns on this hot path for every directory-shaped
    pathspec element (this repo's own load norm sizes every op against
    50-70 concurrent sessions; doubling here is a real cost, not a
    micro-optimization). This is the single shared probe: ONE process, TWO
    classifications derived from its output by the two callers below, kept
    as separate named functions (not merged into one toggle-driven helper)
    for the same reason `_untracked_files_under`'s own docstring already
    gives -- `_dirty_tracked_files_under`'s exclusion of `??` is itself
    load-bearing, and collapsing the two would put that safety property one
    accidental flag-flip away from silently inverting.

    `--untracked-files=all` is pinned explicitly (never left to ambient git
    config) so this probe does not depend on `status.showUntrackedFiles`:
    with that config set to `no`, plain `--porcelain` emits no `??` lines at
    all, and `_untracked_files_under` would silently return `[]` -- exactly
    the class of silent omission the 2026-08-12 fix this module carries
    exists to close, just moved one config layer down. `=all` over `=normal`
    is the deliberate choice: `=normal` can report a wholly-untracked
    subdirectory as a single `<dir>/` line rather than each file beneath it,
    which would under-report `_untracked_files_under`'s per-file sample and
    dedup set; `=all` always lists individual files. This flag only affects
    how UNTRACKED paths are reported -- it does not add, remove, or
    re-classify any tracked-file XY porcelain code, so it cannot change
    `_dirty_tracked_files_under`'s classification.

    *cache*, when given, is a `dict` shared across a single `_handler`
    invocation's `_collect_untracked_omitted` and `_expand_directory_
    pathspecs` calls (both loop over the SAME directory-shaped pathspec
    elements, back to back) -- a directory already probed within that call
    returns its cached lines instead of spawning again. `None` (the
    default) always spawns fresh, for any caller (e.g. a test) that calls
    this in isolation.

    Read via `git_native._git` (not a bare `subprocess.run`) for the same
    `CREATE_NO_WINDOW` reason `_commit_paths_are_clean` is -- this runs on
    the same commit/session hot path.
    """
    if cache is not None and dir_path in cache:
        return cache[dir_path]
    probe = git_native._git(
        [
            "-c", "core.quotepath=false",
            "status", "--porcelain", "--untracked-files=all",
            "--", dir_path,
        ],
        cwd=worktree_root,
    )
    lines = probe.stdout.splitlines() if probe.ok else None
    if cache is not None:
        cache[dir_path] = lines
    return lines


def _dirty_tracked_files_under(
    worktree_root: str, dir_path: str, cache: Optional[_DirectoryPorcelainCache] = None
) -> List[str]:
    """Return the dirty TRACKED files `git status --porcelain` currently
    reports beneath *dir_path*, repo-relative, order preserved.

    Never untracked (`??`) -- a directory pathspec silently sweeping an
    untracked file into a commit because someone named its parent directory
    is precisely the harm the scope guard exists to prevent (see this
    module's `_expand_directory_pathspecs` docstring). A rename line
    (`R  old -> new`, or the analogous `RM`/`MR` staged+worktree pairing)
    reports its NEW path -- the one that will actually exist post-commit.

    The underlying probe is shared with `_untracked_files_under` via
    `_directory_porcelain_lines` (and *cache*, when given) -- see that
    function's own docstring for why one spawn now serves both
    classifications.

    Fails closed to `[]` (never a discovered-directory content) on any git
    failure -- an unresolvable probe must never be read as "nothing dirty
    here" AND ALSO never invent members that were never actually reported.
    """
    lines = _directory_porcelain_lines(worktree_root, dir_path, cache)
    if lines is None:
        return []
    expanded: List[str] = []
    for line in lines:
        if len(line) < 4:
            continue
        code = line[:2]
        if code == "??":
            continue
        rest = line[3:].strip()
        if not rest:
            continue
        if "R" in code and " -> " in rest:
            rest = rest.split(" -> ", 1)[1].strip()
        if rest:
            expanded.append(rest)
    return expanded


#: Cap on how many untracked-file paths `_collect_untracked_omitted` reports
#: by name -- matches `branch_resolution.py`'s own `candidate_plans[:20]`
#: precedent for a receipt-sized sample rather than an unbounded dump. The
#: `count`/`truncated` pair always carries the true total regardless of the
#: cap, so nothing here silently under-counts -- only the printed sample is
#: bounded.
_UNTRACKED_OMITTED_CAP = 20


def _untracked_files_under(
    worktree_root: str, dir_path: str, cache: Optional[_DirectoryPorcelainCache] = None
) -> List[str]:
    """Return the UNTRACKED (`??`) files `git status --porcelain
    --untracked-files=all` currently reports beneath *dir_path*,
    repo-relative, order preserved.

    Mirrors `_dirty_tracked_files_under` exactly except for which porcelain
    code it keeps (`??` here, everything-but-`??` there) -- the two are
    deliberately kept as separate functions rather than one parameterized
    helper: `_dirty_tracked_files_under`'s exclusion of `??` is itself load-
    bearing (its own docstring: naming a directory must never be a way to
    launder an untracked file into a commit), and collapsing the two into
    one toggle-driven helper would put that safety property one accidental
    flag-flip away from silently inverting. The underlying probe IS shared
    with `_dirty_tracked_files_under`, via `_directory_porcelain_lines` and
    *cache* -- see that function's own docstring.

    `.gitignore` is honored for free: `--untracked-files=all` (no
    `--ignored`) never reports an ignored path as `??` at all, so an ignored
    file is never a candidate here -- this function does not need its own
    ignore-filtering logic, and `__pycache__`/`*.pyc` (ignored by this
    repo's own `.gitignore`) never appear in its output.
    """
    lines = _directory_porcelain_lines(worktree_root, dir_path, cache)
    if lines is None:
        return []
    found: List[str] = []
    for line in lines:
        if len(line) < 4:
            continue
        code = line[:2]
        if code != "??":
            continue
        rest = line[3:].strip()
        if rest:
            found.append(rest)
    return found


def _collect_untracked_omitted(
    worktree_root: str,
    paths: List[str],
    cache: Optional[_DirectoryPorcelainCache] = None,
) -> Optional[Dict[str, Any]]:
    """Surfacing half of the 2026-08-12 bug-backlog fix (state/bug-backlog/
    2026-08-12-scoped-git-commit-silently-omits-untracked-files-in-a-
    pathspec.yaml): a directory element in the caller's own pathspec that
    has untracked content beneath it is never staged (by design --
    `_expand_directory_pathspecs`/`_dirty_tracked_files_under` exclude `??`
    lines on purpose, and that exclusion is NOT changed here), but a caller
    who never independently runs `git status` afterward has no way to learn
    the gap exists at all. This is a REPORTING addition only.

    Must be called with the ORIGINAL (pre-`_expand_directory_pathspecs`)
    pathspec -- expansion replaces each directory element with its dirty
    tracked member files, at which point there is no longer a directory
    string here to probe for untracked siblings.

    Returns `None` when no untracked file was found beneath any
    directory-shaped element of *paths* (the common case -- keeps the
    response thin, matching `declined_paths`/`worktree_excluded`'s own
    omit-when-empty convention). Otherwise a dict with the TRUE `count`
    (never truncated), a `paths` sample capped at `_UNTRACKED_OMITTED_CAP`
    (sorted, deduplicated across every directory element named), and
    `truncated` (True iff `count` exceeds the sample length).

    *cache*, when given, is threaded into `_untracked_files_under` -- see
    `_directory_porcelain_lines`'s own docstring. `_handler` passes the SAME
    cache dict here and to `_expand_directory_pathspecs` (both loop over the
    same *paths*), so each directory's `git status` is spawned once total
    across both calls, not once per call.
    """
    seen = set()
    all_found: List[str] = []
    # Review: staff-eng -- a caller can name BOTH a directory and an
    # untracked file beneath it in the same `paths` list (e.g.
    # `paths=[dir, dir/untracked_file]`); the explicitly-named file IS
    # staged and committed via that second element, but was previously
    # still reported here as "NOT staged" because this loop only ever
    # looked at directory-shaped elements, never subtracting the caller's
    # own non-directory elements from what it found. Normalized (forward
    # slashes) so the comparison is stable across platforms.
    explicitly_named = set()
    for p in paths:
        if not isinstance(p, str):
            continue
        try:
            if (Path(worktree_root) / p).is_dir():
                continue
        except OSError:
            pass
        explicitly_named.add(p.replace("\\", "/"))
    for p in paths:
        try:
            is_dir = isinstance(p, str) and (Path(worktree_root) / p).is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            continue
        for f in _untracked_files_under(worktree_root, p, cache):
            if f.replace("\\", "/") in explicitly_named:
                continue
            if f not in seen:
                seen.add(f)
                all_found.append(f)
    if not all_found:
        return None
    all_found.sort()
    capped = all_found[:_UNTRACKED_OMITTED_CAP]
    return {
        "count": len(all_found),
        "paths": capped,
        "truncated": len(all_found) > len(capped),
    }


def _expand_directory_pathspecs(
    worktree_root: str,
    paths: List[str],
    cache: Optional[_DirectoryPorcelainCache] = None,
) -> List[str]:
    """Expand every directory-shaped element of *paths* to its dirty TRACKED
    member files, so the normal classification path (ownership gate,
    clean/dirty partition, staging) sees individual files rather than an
    unclassifiable directory string.

    *cache*, when given, is threaded into `_dirty_tracked_files_under` -- see
    `_directory_porcelain_lines`'s own docstring. `_handler` passes the SAME
    cache dict here and to `_collect_untracked_omitted` (both loop over the
    same, pre-expansion *paths*), so each directory's `git status` is
    spawned once total across both calls, not once per call.

    Live incident this closes (2026-08-06): a caller named a directory of
    125 rewritten tracked JSON records and was refused, because the
    then-live ownership gate classified individual dirty paths and never a
    directory string. The only way through was hand-pasting 125 explicit
    paths. That gate is gone (2026-08-08), but the expansion is kept: the
    downstream staging path and the directory-pathspec rejection below both
    still want individual files rather than a directory string.

    A directory element that expands to at least one dirty tracked file is
    REPLACED, in place, by that expansion (deduplicated, order preserved
    across the whole returned list). A directory element that expands to
    NOTHING (clean, or containing only untracked/ignored content) is left
    UNCHANGED -- it falls through to the existing hard directory-pathspec
    rejection further down the pipeline (`git_native.directory_pathspecs()` /
    `commit_pipeline`'s pre-stage guard), which still applies: this function
    only ever narrows a directory element that has real tracked content to
    classify, it never manufactures a reason to accept one that doesn't.

    Never sweeps untracked content (`_dirty_tracked_files_under` excludes
    `??` lines) -- naming a directory must not be a way to launder an
    untracked file into a commit that a caller would have had to name
    explicitly otherwise.

    Each expanded member is just another string in the returned list,
    treated downstream exactly like any caller-named file.
    """
    root = Path(worktree_root)
    expanded_paths: List[str] = []
    seen = set()

    def _append(p: str) -> None:
        if p not in seen:
            expanded_paths.append(p)
            seen.add(p)

    for p in paths:
        try:
            is_dir = isinstance(p, str) and (root / p).is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            _append(p)
            continue
        members = _dirty_tracked_files_under(worktree_root, p, cache)
        if not members:
            _append(p)
            continue
        for m in members:
            _append(m)
    return expanded_paths




#: `push_state` values — the three-valued push-reporting discriminator, modelled
#: on `workstream_complete/directives_commit_tail.py::PushLandedGate`'s
#: `pushed: Optional[bool]` + `deferred: bool` pair. The invariant this encodes:
#: an unconfirmable push is NEVER rendered as a failed push. A two-valued
#: pushed/not-pushed report cannot express that, and rendering the third state
#: as the second points the reader at a *corrective* action (re-push, amend,
#: force-push) that is more hazardous on an auto-push-armed shared branch than
#: the symptom it is correcting.
PUSH_STATE_PUSHED = "pushed"
PUSH_STATE_FAILED = "push-failed"
PUSH_STATE_UNCONFIRMED = "unconfirmed"
PUSH_STATE_NO_REMOTE = "no-remote"
#: A branch-policy decline (or an unresolvable branch) reported by the
#: pipeline's own `push_status` (C2's canonical vocabulary,
#: `commit_pipeline.PUSH_STATUS_DECLINED`) -- known and deliberate, never
#: routed through the remote probe (see `_resolve_push_report`, C5).
PUSH_STATE_DECLINED = "declined"
#: A push this op deliberately did not attempt because publication was handed
#: to the background pusher (`push_mode="deferred"`, this op's default since
#: the 2026-08-19 op-tail-latency work). Distinct from
#: `PUSH_STATE_UNCONFIRMED` on purpose: "unconfirmed" points the reader at a
#: corrective re-check, which is exactly the wrong instruction on the path
#: where NOT pushing inline is the designed behaviour and
#: `hooks/auto_push.py` is already carrying the commit. Collapsing the two
#: would put "re-check, do not re-push" on every ordinary commit.
PUSH_STATE_DEFERRED = "deferred"



#: Bucket name `commit_ledger.oracle._DOCS_KIND` treats as docs-only --
#: duplicated as a literal (not imported) because `oracle.py` declares it
#: private to its own two-figure split; this module's `kind` is a peer
#: producer of the same vocabulary, not a consumer of that constant.
_LEDGER_DOCS_KIND = "doctrine"

#: Generic non-doctrine `kind` this module stamps for a commit whose
#: pathspec is not entirely `"doctrine"`-bucketed (or is entirely noise) --
#: the oracle (`commit_ledger.oracle.py`) only ever branches on `kind ==
#: "doctrine"`; any other value counts toward the code-only figure, so one
#: stable non-doctrine label is all that predicate needs.
_LEDGER_CODE_KIND = "code"


def _ledger_kind_and_weight(worktree_root: str, paths: List[str]) -> "tuple[str, float]":
    """Commit-level `(kind, weight_basis)` for the commit ledger (C5),
    derived from `paths` -- the pathspec this handler already holds, never
    a fresh diff (hard constraint: `kind`/`weight_basis` are computed ONCE,
    at write time, from the staged pathspec; re-deriving them by diffing at
    read time is the mistake the plan's own K-007 measurement cost 2.8s on).

    `weight_basis` is the sum of `commit_ledger.classify.weight_for_path`
    over every path (that function already returns 0.0 for a noise path,
    per its own AC5 contract -- never negative).

    `kind` is `"doctrine"` iff every non-noise path classifies (`review_
    brightline_gate.classify_surface`) as `"doctrine"` -- mirrors `commit_
    ledger.oracle.py`'s own docs-only split ("a commit whose changed paths
    are entirely doctrine-bucketed is EXCLUDED from the code-only figure").
    A commit with at least one non-doctrine path, or with no non-noise path
    at all, gets `_LEDGER_CODE_KIND` -- the oracle only ever tests `kind ==
    "doctrine"`, so a single stable non-doctrine label is sufficient.

    Pure local computation: `weight_for_path`/`classify_surface` read only
    `coordinator.local.md` and do fnmatch/string classification -- no git
    subprocess, no nested interpreter (this chunk's own runtime negative).
    """
    # Local import: `commit_ledger.store` imports `ops.resolve_swept_baton`,
    # so a module-level import here closes a cycle back into this module and
    # leaves `commit_ledger.store` partially initialized whenever anything
    # imports it before `coordinator_core.ops` -- which de-registers this op.
    from coordinator_core.commit_ledger.classify import weight_for_path

    total_weight = 0.0
    any_classified = False
    any_non_doctrine = False
    for p in paths:
        total_weight += weight_for_path(worktree_root, p)
        if _is_noise_path(p):
            continue
        any_classified = True
        if classify_surface(p) != _LEDGER_DOCS_KIND:
            any_non_doctrine = True
    kind = _LEDGER_DOCS_KIND if (any_classified and not any_non_doctrine) else _LEDGER_CODE_KIND
    return kind, total_weight


@register_op("ceremony.scoped_git_commit")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'ceremony.scoped_git_commit' handler.

    Deliberately a PLAIN sync function, not `async def` (2026-08-07 transport-
    hang fix). This handler calls `run_commit_pipeline()` synchronously below,
    which blocks on `git` subprocess calls (stage/commit/push, incl. a
    network round trip) -- and this file contains zero `await` statements
    anywhere. A coroutine that never awaits can still be dispatched as
    `async def`, but `coordinator_core.ipc.dispatch_message` then invokes it
    via `asyncio.wait_for(handler(...), timeout=op_timeout)` on the async
    branch, which "cannot interrupt a blocked event loop" (dispatch_message's
    own documented C3 invariant) -- so `DISPATCH_TIMEOUT_SECS` silently
    stopped bounding this op at all, and a caller waiting on a slow `git`
    call (e.g. a network-bound push) hung well past both the server's own
    timeout budget and the CLI transport's client-side ceiling
    (cc_invoke.py), surfacing only as an opaque `engine timeout after Ns`
    from the CLIENT side. Being a plain sync function instead routes this
    handler through
    dispatch_message's SYNC branch, which already offloads it via
    `asyncio.to_thread` -- restoring the timeout's actual effect (a clean,
    bounded `op timed out after Ns` response) instead of an unbounded hang.
    Negative-spec: do NOT re-mark this `async def` without first moving every
    blocking call inside it (including inside `run_commit_pipeline`) behind
    its own `asyncio.to_thread` -- the CI grep gate
    (`coordinator_core/tests/test_async_handler_discipline.py`) only flags a
    blocking call written DIRECTLY in an async body, not one reached through
    an intermediate function call like `run_commit_pipeline()`, so it would
    not catch a regression here.

    Params (from JSON-RPC request params dict):
        worktree_root (str, required) — path to the worktree to commit in.
        paths         (list[str], required) — repo-relative paths to stage
                                               and commit (the explicit
                                               pathspec — AC5, never a bare
                                               `git commit`).
        message       (str, required) — commit message subject line. A
                                         path-shaped value is REJECTED rather
                                         than committed verbatim (see
                                         `_reject_path_shaped_message`): this
                                         op is `-m`, not `-F`, and a caller
                                         handing it a scratchpad message-file
                                         path silently loses the whole body.
        prose         (str, optional) — commit message BODY, composed after
                                         a blank line per `compose_message`'s
                                         existing subject/prose/blocks shape
                                         (see `commit_message.py`). Empty
                                         string (the default) reproduces the
                                         prior subject-only behavior exactly
                                         — added for callers (e.g. an
                                         unattended auto-commit) that need to
                                         put an unbounded path list or
                                         explanatory context in the body
                                         rather than crowd it into the
                                         subject line.
        deliverable_id (str, optional) — C7a (docs/plans/2026-08-10-a-commit-
                                         trailer-that-names-the-session.md):
                                         a caller who already knows which
                                         deliverable this commit belongs to.
                                         VALIDATED here (must be a string
                                         when given — a non-string value is a
                                         validation error, same shape as a
                                         missing required param), then
                                         forwarded through
                                         `run_commit_pipeline()`
                                         (`commit_pipeline.py`, C7b) to
                                         `git_native.commit_scoped()`, which
                                         owns the shape/existence guard and
                                         the message-trailer precedence
                                         ruling — see its own
                                         `deliverable_id` param. This op does
                                         not itself decide precedence and
                                         does not resolve the value: an id
                                         that does not resolve to a real
                                         artifact is rejected downstream, not
                                         here.
        push_mode      (str, optional) — "sync" | "deferred" | "none".
                                         DEFAULTS TO "deferred" (2026-08-19
                                         op-tail-latency): publication is
                                         handed to the already-installed
                                         background pusher
                                         (`coordinator_core/hooks/
                                         auto_push.py`), which owns the hold
                                         window, pending records and race
                                         resolution. The inline push this
                                         replaces cost 1.3-4.9s of network
                                         round trip on every one of ~692
                                         daily calls, and nothing branched on
                                         its result -- `pushed`/`push_state`
                                         reach only the CLI's operator-facing
                                         note. Under a non-sync mode the op
                                         reports `push_state="deferred"`
                                         (or `"no-remote"` when there is no
                                         remote to queue for), never
                                         `"unconfirmed"`, whose note tells
                                         the reader to re-check.
                                         Pass "sync" ONLY when the caller
                                         genuinely needs the sha on the
                                         remote before this op returns, and
                                         say why at the call site -- the
                                         deferred default is what keeps this
                                         op off the network.
        stage_patch    (str, optional) — C3 (docs/plans/2026-08-14-the-tool-
                                         stages-what-it-commits.md): a path to
                                         a patch file. The tool stages what it
                                         commits -- when given, this path is
                                         validated (must exist and be
                                         readable) BEFORE anything mutates,
                                         then forwarded to `run_commit_
                                         pipeline()`, which applies it under a
                                         process-private temporary index
                                         (never the shared repo index) and
                                         commits exactly the resulting blobs
                                         for every named path the patch
                                         covers -- provenance by construction,
                                         never by asking who a session is
                                         (this plan's own anti-scope). A named
                                         path the patch does NOT cover still
                                         takes today's ordinary staged-or-
                                         worktree route (AC4, additive per-
                                         path) -- see `unprovenanced_paths`
                                         below for how that mixed shape is
                                         named on the response.

    Returns:
        {
          "committed": bool,        # True iff a NEW commit landed this call
          "sha":       str | None,  # post-commit HEAD SHA, None if nothing
                                     # landed (no-op or a gate/commit failure)
          "pushed":    bool | None, # True: the sha is on the remote; False:
                                     # confirmed absent from the remote;
                                     # None: no remote, or unconfirmable
          "push_state": str,        # present iff "committed" -- the
                                     # three-valued discriminator that makes
                                     # `pushed is None` readable: one of
                                     # PUSH_STATE_* below
        }

        Conditionally present (only when a commit landed but its sha could
        not be resolved -- W3, docs/plans/2026-08-08-a-landed-commit-
        reported-as-failed.md; "committed" is still True on this path, "sha"
        is still None, and it is pushed exactly as any other landed commit):
          "sha_unverified": bool,      # always True when present
          "diagnostics":    list[str], # names what happened: history
                                        # changed, the sha is unresolvable

        `pushed`/`push_state` answer "is this commit published", NOT "did
        this op's own push step do the publishing" -- a concurrent
        `coordinator-auto-push` post-commit push that wins the race and
        lands the sha yields `PUSH_STATE_PUSHED`, because from every
        caller's point of view the commit IS on the remote. Rendering that
        case as a failure is the false negative this tri-state exists to
        kill (doe-claude-em memo, 2026-07-30).

        Conditionally present (only when "committed" is False):
          "reconcile_decline": str,  # present iff the commit step's landed-
                                     # despite-failure reconcile RAN and
                                     # declined -- "<tag> (searched <range>)".
                                     # Diagnostic only, never a predicate: it
                                     # says why the reconcile could not
                                     # confirm a landed commit, which is the
                                     # one thing that can contradict a benign
                                     # "empty-commit-set" reason on this
                                     # branch. Absent -- never "" -- when no
                                     # reconcile ran.
          "commit_failed": bool,       # True iff a gate or the commit step
                                        # itself failed; False for a benign
                                        # no-op (already-committed/empty pathspec)
          "diagnostics":   list[str],  # gate/commit failure detail, [] on a
                                        # benign no-op
          "reason":        str,        # present only for the empty-commit-set
                                        # no-op ("reason": "empty-commit-set")

        Conditionally present (only when a genuine push-invariant violation
        is confirmed — this op's own push reported failure, which since
        opro-01 C-01 is authoritative rather than racy: see
        `_resolve_push_report`):
          "integrity_breach": bool  # always True when present

        Conditionally present (2026-08-04 fix, never a silent drop —
        `_declined_paths`): a path NAMED in the caller's own `paths` that
        was declined from the commit set for any reason (not found in the
        worktree/index and not attributable to a deletion, or excluded by
        `.gitignore`) — present whenever non-empty, regardless of whether
        `committed` is True (a PARTIAL decline inside an otherwise
        successful commit) or False:
          "declined_paths": [{"path": str, "reason": str}, ...]

        Conditionally present (2026-08-12 fix, state/bug-backlog/2026-08-12-
        scoped-git-commit-silently-omits-untracked-files-in-a-pathspec.yaml;
        REPORTING ONLY -- never changes what gets staged, see
        `_collect_untracked_omitted`'s own docstring): a directory element
        in the caller's own `paths` that has untracked content beneath it
        which was NOT staged (untracked files under a directory pathspec
        are, by design, never swept into the commit) -- present whenever
        non-empty, regardless of `committed`:
          "untracked_paths_omitted": {
            "count":     int,       # true total, never truncated
            "paths":     [str, ...],# sample, capped at _UNTRACKED_OMITTED_CAP
            "truncated": bool,      # True iff count exceeds len(paths) above
          }

        Conditionally present (AC4, C3, docs/plans/2026-08-14-the-tool-
        stages-what-it-commits.md; never a silent drop -- mirrors
        `worktree_excluded`'s own posture): present whenever `stage_patch`
        was supplied and at least one named path was NOT covered by the
        patch -- that subset took today's ordinary staged-or-worktree route
        instead, unprovenanced:
          "unprovenanced_paths": [str, ...]

    On a validation error (missing/empty required param):
        {"committed": False, "sha": None, "pushed": None, "error": str}

    On a rejected pathspec: a sweeping pathspec element (`.`, `./`, `:/`,
    `:(top)`, a glob, an empty element, the repo root, an ancestor of it, or
    `-A`/`-a`/`--all`) — see `_reject_sweeping_pathspec` — is rejected
    BEFORE any staging happens:
        {"committed": False, "sha": None, "pushed": None, "error": str}
    — the same shape as a validation error; the reason is folded into
    `error` rather than given its own key, since this is still "the call
    was invalid", just discovered one step later.

    `include_orphans` (bool, optional): RETIRED (C2, AC9) -- accepted for
    backward compatibility, but has no effect on any outcome. Under this
    gate an "orphan" is a claim whose claimant is not live, and the
    liveness check (`session_liveness.session_live`) already lets a
    not-live claimant's path through without needing an opt-in -- there is
    no remaining semantics for this flag to relax. Wiring it to also relax
    the UNANSWERABLE-path branch was considered and rejected: that would
    fail OPEN on exactly the case the positive/negative asymmetry rule (see
    `claim_index.py`'s docstring) forbids -- an index that could not answer
    is not the same thing as a claim that resolved to "orphaned".
    """
    worktree_root_raw = params.get("worktree_root")
    paths_raw = params.get("paths")
    message = params.get("message")
    prose_raw = params.get("prose", "")
    deliverable_id_raw = params.get("deliverable_id")
    stage_patch_raw = params.get("stage_patch")
    push_mode_raw = params.get("push_mode", PUSH_MODE_DEFERRED)

    # AC3/AC11 (C3): the required-param and `deliverable_id`-shape checks
    # below (through the `not message` check) are malformed-REQUEST
    # rejects, not blocks -- they protect against nothing git enforces
    # (a missing worktree_root/paths/message is not a git concept at all,
    # it is this op's own JSON-RPC contract), and their outlet is trivial:
    # the SAME call, corrected, with no wait and no human -- there was
    # never anything to retry against, only a malformed request to fix.
    if not worktree_root_raw:
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": "ceremony.scoped_git_commit: 'worktree_root' param is required",
        }
    if push_mode_raw not in (PUSH_MODE_SYNC, PUSH_MODE_DEFERRED, PUSH_MODE_NONE):
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": (
                "ceremony.scoped_git_commit: 'push_mode' param must be one of "
                f"'{PUSH_MODE_SYNC}'/'{PUSH_MODE_DEFERRED}'/'{PUSH_MODE_NONE}', "
                f"got {push_mode_raw!r}"
            ),
        }
    if deliverable_id_raw is not None and not isinstance(deliverable_id_raw, str):
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": "ceremony.scoped_git_commit: 'deliverable_id' param must be a string when given",
        }
    # Review: code-reviewer -- Finding [P2], 2026-08-10. `commit_scoped`'s
    # downstream guards (`if deliverable_id:`) are truthy checks, so an
    # empty/whitespace-only string forwarded past this point is silently
    # DROPPED -- AC19's shape/existence guard never runs, no trailer is
    # applied, and no error is surfaced. Reject here instead, at the
    # validation boundary, same posture as the AC19 rejections downstream.
    # `None` (not supplied) is unaffected -- this only fires on a non-None
    # string that is empty/whitespace-only.
    if deliverable_id_raw is not None and not deliverable_id_raw.strip():
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": (
                "ceremony.scoped_git_commit: 'deliverable_id' param must not be "
                "empty/whitespace-only when given"
            ),
        }
    if not paths_raw:
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": "ceremony.scoped_git_commit: 'paths' param is required (non-empty list)",
        }
    if not message:
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": "ceremony.scoped_git_commit: 'message' param is required",
        }

    path_shaped = _reject_path_shaped_message(str(message))
    if path_shaped is not None:
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": "ceremony.scoped_git_commit: rejected -- %s" % (path_shaped,),
        }

    # AC1/AC3 (C3, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md):
    # `stage_patch`, when given, must be a readable file -- validated BEFORE
    # anything mutates (before staging or any git call below), same posture
    # as every other validation guard above. A missing/unreadable patch
    # refuses loudly and early rather than surfacing only once `commit()`'s
    # own `git apply --cached` fails deep inside the pipeline.
    stage_patch: Optional[str] = None
    if stage_patch_raw is not None:
        if not isinstance(stage_patch_raw, str) or not stage_patch_raw.strip():
            return {
                "committed": False,
                "sha": None,
                "pushed": None,
                "error": (
                    "ceremony.scoped_git_commit: 'stage_patch' param must be a "
                    "non-empty string path when given"
                ),
            }
        stage_patch = normalize_native_path(stage_patch_raw)
        if not Path(stage_patch).is_file():
            return {
                "committed": False,
                "sha": None,
                "pushed": None,
                "error": (
                    "ceremony.scoped_git_commit: 'stage_patch' path does not "
                    "exist or is not a file: %s" % (stage_patch,)
                ),
            }
        try:
            Path(stage_patch).open("r", encoding="utf-8").close()
        except OSError as exc:
            return {
                "committed": False,
                "sha": None,
                "pushed": None,
                "error": (
                    "ceremony.scoped_git_commit: 'stage_patch' path is not "
                    "readable: %s (%s)" % (stage_patch, exc)
                ),
            }

    worktree_root = normalize_native_path(worktree_root_raw)
    paths: List[str] = [str(p) for p in paths_raw]
    # Review: code-reviewer -- Finding [P3], 2026-08-08. `include_orphans` is
    # RETIRED (AC9, see this function's own docstring) and read nowhere in
    # this file. The param itself stays accepted (backward compatibility),
    # it is simply never bound to a local now that there is nothing left for
    # it to gate.

    # 2026-08-12 fix (state/bug-backlog/2026-08-12-scoped-git-commit-
    # silently-omits-untracked-files-in-a-pathspec.yaml): computed against
    # the ORIGINAL, unexpanded pathspec -- `_expand_directory_pathspecs`
    # below replaces each directory element with its dirty TRACKED members,
    # after which there is no directory string left to probe for untracked
    # siblings. `None` when nothing was found (the common case).
    #
    # `directory_porcelain_cache` is shared with `_expand_directory_
    # pathspecs` below -- both loop over the SAME directory-shaped elements
    # of `paths`, and without a shared cache each would spawn its own `git
    # status` per directory (2026-08-12 fix, P2 finding: doubled git spawns
    # on this hot path). See `_directory_porcelain_lines`'s own docstring.
    directory_porcelain_cache: _DirectoryPorcelainCache = {}
    untracked_omitted = _collect_untracked_omitted(
        worktree_root, paths, cache=directory_porcelain_cache
    )

    # Directory-pathspec expansion (2026-08-06 fix, live incident -- see
    # `_expand_directory_pathspecs`'s own docstring): a directory element
    # with dirty TRACKED content beneath it is replaced by that content
    # BEFORE anything downstream (sweeping check, ownership gate, staging)
    # ever sees the directory string -- everything below this point
    # operates on the expanded pathspec. A directory with nothing to expand
    # to is left unchanged and falls through to the existing hard
    # directory-pathspec rejection unchanged.
    paths = _expand_directory_pathspecs(
        worktree_root, paths, cache=directory_porcelain_cache
    )

    # Sweeping-pathspec rejection (C4c) -- independent of ownership, and
    # evaluated FIRST: a sweeping pathspec is unsafe on a shared branch even
    # when every path it happens to resolve to right now is this session's
    # own (see this module's own docstring, "Sweeping-pathspec rejection").
    sweep_reason = _reject_sweeping_pathspec(paths, worktree_root)
    if sweep_reason is not None:
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": "ceremony.scoped_git_commit: rejected -- %s" % (sweep_reason,),
        }

    # Stale-index rejection -- evaluated on the EXPANDED pathspec, before
    # anything stages or commits. See `_reject_stale_index_paths` for the
    # incident (a54addce silently reverting cd751b79 through safe-commit-
    # offer) and for why the `stage_patch` path is carved out.
    if stage_patch is None:
        stale_reason = _reject_stale_index_paths(paths, worktree_root)
        if stale_reason is not None:
            return {
                "committed": False,
                "sha": None,
                "pushed": None,
                "error": "ceremony.scoped_git_commit: rejected -- %s"
                % (stale_reason,),
            }

    # Resolved ONCE, here, and reused by the post-commit `release_committed_
    # claims` call further down -- the CALLING session's own identity, never
    # the private per-invocation `scoped-git-commit-<uuid4>` nonce minted
    # just below for `run_commit_pipeline` (see that mint's own comment).
    owner_session_id = _resolve_committing_session_id(params, worktree_root)

    # C2's ownership gate call site (`_check_claim_conflicts`) was removed
    # outright 2026-08-13 (docs/plans/2026-08-13-claim-release-deadlock-and-
    # the-doctrine-that-rejects-it.md, C1) -- see this module's docstring,
    # "Sink-side ownership enforcement". Nothing GATES here in its place.
    # C1d's advisory `_warn_recent_edits` log line stood here until
    # 2026-08-19; see this module's docstring for why it was removed.

    # W3 dead-wire finding (docs/plans/2026-08-08-a-landed-commit-reported-
    # as-failed.md, item 4 -- verified on disk 2026-08-08): `session_id` is
    # UNREAD across the entirety of `run_commit_pipeline`'s body (grepped;
    # its only other appearance in commit_pipeline.py is the parameter
    # declaration itself) -- the absorbed-peer-claims trailer that used to
    # consume it was removed by a later commit, per this plan's own text.
    # NOT removed here: `session_id` is a required (no-default) keyword-only
    # parameter of `run_commit_pipeline`, so dropping this mint would also
    # require dropping the parameter in `commit_pipeline.py` -- explicitly
    # out of this chunk's file scope (W1/W2 own that file). Left in place,
    # both halves intact, pending a follow-up chunk scoped to that file.
    session_id = f"scoped-git-commit-{uuid.uuid4().hex}"

    result = run_commit_pipeline(
        worktree_root,
        session_id=session_id,
        subject=str(message),
        prose=str(prose_raw or ""),
        stage_paths=paths,
        caller_paths=set(paths),
        deliverable_id=deliverable_id_raw,
        stage_patch=stage_patch,
        # state/bug-backlog/2026-08-18-scoped-git-commit-stamps-a-foreign-
        # session-id-8d21f0c4e7b9.yaml: `owner_session_id` (above) is this
        # request's own params-aware resolution -- an explicit
        # `params["session_id"]` override, falling back to `session_core.
        # resolve_session_id`, never the private `scoped-git-commit-<uuid4>`
        # nonce this call's own `session_id=` arg carries (that nonce is
        # UNREAD by `run_commit_pipeline`'s body -- see its own docstring).
        # Threading it here makes it authoritative for the `Session-Id:`
        # trailer too, closing the gap where the trailer was previously
        # resolved a SECOND, independent time deep in `git_native.py` via a
        # blind `os.environ` read that had no visibility into this
        # request's own override.
        attributed_session_id=owner_session_id,
        # 2026-08-19 op-tail-latency: this op's p50 was 8.9s over 692 calls/24h
        # (58% of all engine wall time), and `push_with_retry`'s network round
        # trip was 1.3-4.9s of it -- paid synchronously by every caller even
        # though NOTHING branches on the result (`pushed`/`push_state` reach
        # only `coordinator/bin/scoped-git-commit::_render`'s operator-facing
        # string; `integrity_breach` is DERIVED from a sync push having
        # failed, so it has nothing to guard once the push is deferred).
        # `deferred` hands publication to the already-installed background
        # pusher (`coordinator_core/hooks/auto_push.py`, which owns the hold
        # window, pending records and race resolution) by leaving
        # `suppress_post_commit_auto_push` False -- the same default
        # `wsc_tail.py` has always used. Negative-spec: a caller needing the
        # sha on the remote BEFORE this op returns must pass
        # `push_mode="sync"` explicitly and say why -- do not flip this
        # default back to make one caller's ordering work.
        push_mode=push_mode_raw,
    )

    # W3b (2026-08-19), split out of the `committed` expression below so the
    # two lookups can have DIFFERENT strictness -- they are guarding different
    # things and collapsing them into one `getattr` chain was a review finding
    # (Review: coordinator:code-reviewer -- Q2, 2026-08-19).
    #   `commit` via getattr: legitimately absent on the partial
    #     `SimpleNamespace` doubles this module's own tests construct, and
    #     declared `Optional[CommitOutcome]` besides -- absence is a real
    #     state to tolerate.
    #   `.landed` via a BARE read gated on `is not None`: a rename or
    #     restructure of `CommitOutcome` must raise `AttributeError` loudly,
    #     not degrade to `False`. Degrading is precisely the defect W3b
    #     exists to repair -- a landed commit silently reported as not
    #     landed -- and a `getattr` here would have re-opened it one field
    #     over, which is what the review caught.
    _commit_outcome = getattr(result, "commit", None)
    _commit_landed = _commit_outcome is not None and _commit_outcome.landed

    response: Dict[str, Any] = {
        # W3 (docs/plans/2026-08-08-a-landed-commit-reported-as-failed.md):
        # `committed_sha is not None` used to be the WHOLE predicate, so a
        # commit that landed with an unresolvable sha (`PipelineResult.
        # sha_unverified`) reported `committed: False` -- the exact "landed
        # commit reported as failed" shape this op's own docstring promises
        # never happens (`"committed": bool, # True iff a NEW commit landed
        # this call`). `sha` stays `result.committed_sha` (still correctly
        # `None` on this path -- there is genuinely no sha to report) --
        # only `committed` widens to also cover "landed, sha unknown".
        # Read as a bare attribute here, deliberately, even though the
        # `sha_unverified` probe just below uses `getattr(..., False)`: THIS
        # key must never silently answer `False`. A fallback here would
        # re-report a landed commit as `committed: False` -- the exact defect
        # the widening exists to remove -- the moment the field is renamed,
        # whereas the probe below only decides whether to ATTACH an
        # explanatory key. The partial `SimpleNamespace` doubles in this
        # package (see `_declined_paths`' matching `getattr(result, "stage",
        # None)`) reach that probe but never this constructor, so the strict
        # read costs them nothing. Verified: relaxing this to `getattr` breaks
        # nothing, tightening the probe below to a bare read breaks three.
        # W3b (2026-08-19): W3 widened this to cover ONE of the pipeline's
        # landed-but-no-sha shapes, `sha_unverified`. It is not the only one.
        # `CommitOutcome.landed` is the git layer's own answer to the exact
        # question this key asks -- its docstring commits it to True on
        # "every path where commit_scoped() succeeded ... because in every one
        # of those cases `git commit` already created the commit; only
        # `committed_sha` is unknown" -- and that flag is NOT mirrored onto
        # `PipelineResult`, so this predicate could not see it. The remaining
        # uncovered paths (empty message subject, zero-or-ambiguous
        # commit-token match) therefore still rendered `committed: False` over
        # a commit that exists in history, and the caller's next stop is
        # `_classify_uncommitted`, which finds the tree clean BECAUSE the
        # commit landed and reports the benign `reason: "empty-commit-set"` --
        # a landed commit reported to the operator as "no commit landed".
        # Observed live: a462af36d/11d1db069 (state/sizings/, 2026-08-19).
        # Reached through `result.commit` (declared `Optional[CommitOutcome]`)
        # rather than a mirrored field, so nothing new has to be threaded
        # through the pipeline. `_commit_landed` is resolved just above this
        # dict, where its two lookups get the different strictness each one
        # needs -- see that block for why the `commit` lookup tolerates
        # absence while the `.landed` read deliberately does not.
        # Mirroring `landed` onto `PipelineResult` as a first-class field is
        # the structurally better repair and is filed as follow-up work
        # (Review: coordinator:code-reviewer -- Q3, 2026-08-19); it is NOT
        # load-bearing for this predicate's correctness, which the same
        # review verified by tracing every `CommitOutcome` construction site.
        "committed": (
            result.committed_sha is not None
            or result.sha_unverified
            or _commit_landed
        ),
        "sha": result.committed_sha,
        "pushed": result.pushed,
    }
    if getattr(result, "sha_unverified", False):
        # Surfaced unconditionally (not folded into the failure-only
        # `diagnostics` key below, which is gated on `not response[
        # "committed"]` and therefore never reached on this path) -- a
        # caller needs to know WHY `sha` is `None` on an otherwise-committed
        # response, not just that it is.
        response["sha_unverified"] = True
        response["diagnostics"] = list(result.diagnostics)

    # A bare {"committed": False, "sha": None, "pushed": False} is returned for
    # THREE materially different outcomes -- the benign already-committed no-op,
    # a staging/gate failure, and an integrity breach -- and a caller reading
    # only `committed` cannot tell them apart. Surfacing the pipeline's own
    # outcome predicates is additive to the response and changes no commit
    # semantics. Withheld on the success path so the green response stays the
    # thin three-key shape callers already parse.
    if response["committed"]:
        # `push_status` (C2's canonical vocabulary) is preferred over the
        # bare `pushed` tri-state -- it is what lets a policy decline
        # short-circuit before the remote probe (C5). Every `PipelineResult`
        # construction site sets `push_status` (C2), so a plain attribute
        # read is correct here -- a `getattr(..., PUSH_STATUS_NOT_ATTEMPTED)`
        # fallback would silently degrade a future missing-field bug into
        # "not attempted", which on THIS call site means a policy decline
        # would quietly fall through to the remote probe AC5 exists to
        # prevent (C7b, docs/plans/2026-08-08-the-push-leg-that-never-asked-
        # which-branch.md). Test doubles in this package's own test file
        # carry `push_status` explicitly now.
        push_status = result.push_status
        pushed, push_state = _resolve_push_report(
            push_status, push_mode=push_mode_raw
        )
        # A deferred push never reaches the remote-resolution leg, so it
        # cannot report NO_REMOTE the way the sync path did -- and "queued for
        # background push" in a repo with no remote is a note that will never
        # come true. One LOCAL spawn (`git remote`, no network) restores the
        # distinction; it runs only on the deferred branch, which has already
        # given back the whole 5-spawn push leg, so this is net -4 against the
        # spawn budget rather than a new cost.
        if push_state == PUSH_STATE_DEFERRED and not _repo_has_remote(worktree_root):
            pushed, push_state = None, PUSH_STATE_NO_REMOTE
        response["pushed"] = pushed
        response["push_state"] = push_state

        # Post-commit claim release (C3, AC1): only reached once the commit
        # has demonstrably landed (`response["committed"]` is derived from
        # `result.committed_sha is not None`, above) -- never before.
        # `scope.release_committed_claims` is C1's own per-path helper: it
        # re-derives cleanliness itself, per path, via a single porcelain
        # call (TOCTOU belt-and-braces its own docstring calls out) --
        # deliberately NOT `_commit_paths_are_clean`, the aggregate cousin
        # that can only answer "are ALL of these paths clean", and would
        # therefore suppress release for every clean path in a multi-path
        # commit if even one path alongside it were still dirty.
        #
        # Failure direction: a failure releasing claims must never fail the
        # commit that already landed -- the commit is the durable outcome: a
        # retained stale claim is the safe residue, so this is wrapped and
        # never allowed to propagate into the response.
        try:
            session_scope.release_committed_claims(
                owner_session_id, paths, cwd=worktree_root
            )
        except Exception:
            _LOG.debug(
                "release_committed_claims failed post-commit; claim(s) retained",
                exc_info=True,
            )

        # Phantom-claim self-heal (state/bug-backlog/2026-08-06-a-phantom-
        # touch-claim-from-an-interrupte-c21f5bbdd077.yaml), run right
        # alongside `release_committed_claims` above so a phantom retires at
        # the same moment a real claim does -- the operator's very next
        # commit sees a clean offer rather than carrying the phantom for the
        # rest of the session. `release_phantom_claims` is internally
        # fail-safe RETAIN (any read/parse/git failure skips release for
        # this call) and is NOT wrapped a second time here beyond the
        # `except Exception` below, which exists only to guarantee this
        # bookkeeping call can never turn an already-landed commit into a
        # failed response -- it adds no swallowing `release_phantom_claims`
        # does not already do itself.
        #
        # Invocation cost: this only ever runs post-commit (never on a
        # dry-run/preview path -- gated the same as `release_committed_
        # claims` above, inside `if response["committed"]`), and its own
        # implementation (`session.scope.release_phantom_claims`) issues at
        # most TWO extra subprocesses -- `_tracked_at_head`'s `git ls-tree`
        # and `_staged_in_index`'s `git ls-files --stage` (added 2026-08-06)
        # -- and only when this session's own touched.txt names a claimed
        # path currently absent from disk -- the common case (every claimed
        # path still exists, or was already retired by `release_committed_
        # claims` above) costs zero subprocesses: it returns after the
        # in-memory `claimed`/`candidates` scan finds nothing absent. A
        # phantom claim is the rare, bug-residue case this exists to clean
        # up, not a per-commit steady-state cost.
        try:
            session_scope.release_phantom_claims(owner_session_id, cwd=worktree_root)
        except Exception:
            _LOG.debug(
                "release_phantom_claims failed post-commit; claim(s) retained",
                exc_info=True,
            )

    if not response["committed"]:
        commit_failed, diagnostics, empty_commit_set = _classify_uncommitted(
            worktree_root, paths, result
        )
        response["commit_failed"] = commit_failed
        response["diagnostics"] = diagnostics
        if empty_commit_set:
            # The pathspec resolved to an empty commit set. That is benign when
            # the paths are already committed, and a silent caller error when
            # the pathspec matched nothing at all (a directory pathspec is the
            # common shape here -- it stages, but contributes no
            # `commit_paths`, so the op no-ops while leaving the caller's files
            # staged on a shared branch).
            response["reason"] = "empty-commit-set"
        elif getattr(result, "reason", ""):
            # AC7 (docs/plans/2026-08-14-the-tool-stages-what-it-commits.md,
            # C3): the pipeline's own machine-readable failure tag --
            # "patch-did-not-apply", "head-blob-cas-refusal", "index-head-
            # cas-refusal", or "commit-failure" -- mirrored verbatim so a
            # caller (the CLI included) can key a distinct exit path off
            # `reason` rather than parsing `diagnostics` prose. Never set
            # here for the `empty_commit_set` no-op above -- that reason is
            # decided one layer up (this function's own `_classify_
            # uncommitted`), which alone has the `git status` probe that
            # distinction needs; a stale `CommitOutcome.reason` left over
            # from a prior gate must never leak through as this call's
            # reason once `_classify_uncommitted` has already reclassified
            # it as benign.
            response["reason"] = result.reason
        # The one key that can contradict the `empty-commit-set` reason just
        # above. That reason is derived from a CLEAN `git status` -- and a
        # tree is equally clean when the pathspec was already committed
        # (benign) and when this call's own commit landed while `commit_
        # scoped()` reported failure (the 2026-08-19 defect: a landed commit
        # reported as not landed). `commit_pipeline._reconcile_landed_despite_
        # failure` exists to separate those two, and when it declines, this
        # tag names WHICH precondition declined -- so the next live occurrence
        # arrives self-diagnosed rather than costing another session's
        # investigation. Present only when a reconcile actually ran and
        # declined; absent (not empty) otherwise, keeping every other
        # uncommitted response byte-identical.
        _reconcile_decline = getattr(
            getattr(result, "commit", None), "reconcile_decline", ""
        )
        if _reconcile_decline:
            response["reconcile_decline"] = _reconcile_decline
    # C-01/C-03: the pipeline derives `integrity_breach` from `push_status ==
    # PUSH_STATUS_FAILED`, and with one publisher per commit that predicate is
    # no longer racy -- so this is a straight carry-through, not a correction.
    # The `push_state` conjunct is retained rather than dropped as redundant:
    # the pipeline has a second `integrity_breach` construction site (the
    # `sha_unverified` path, where the commit landed but its sha could not be
    # read back), and this re-derivation is what keeps THIS response's
    # `integrity_breach` and `push_state` answering the same question. It fails
    # closed to "no breach" on every non-FAILED state, unchanged.
    if result.integrity_breach and response.get("push_state") == PUSH_STATE_FAILED:
        response["integrity_breach"] = True

    # Never a silent drop (2026-08-04 fix, live incident -- defect A): a
    # path NAMED in the caller's own pathspec that this call declines to
    # include in the commit set, for ANY reason, must say so on the way
    # out -- whether the overall call succeeds (other paths landed) or
    # fails outright. Computed unconditionally (both on the committed and
    # not-committed branches above), never folded into the failure-only
    # `diagnostics` key, so a genuine PARTIAL decline inside an otherwise
    # successful commit is still visible to the caller. Omitted from the
    # response entirely when nothing was declined -- keeps the green-path
    # response thin (see `test_successful_commit_response_stays_thin`).
    declined = _declined_paths(result)
    if declined:
        response["declined_paths"] = declined

    # Never silent (state/bug-backlog/2026-08-10-scoped-git-commit-reports-
    # success-while-334e90d707f9.yaml): `commit_scoped()`'s private-index
    # branch commits the STAGED content of a diverged path, not its
    # worktree content -- a legitimate success, but one the operator must
    # be told about by name. Surfaced unconditionally (both on a committed
    # and an uncommitted response -- mirrors `declined_paths` above), and
    # omitted entirely when nothing was excluded so the clean-path response
    # stays byte-identical to today's.
    commit_outcome = getattr(result, "commit", None)
    worktree_excluded = list(getattr(commit_outcome, "worktree_excluded", ()) or ())
    if worktree_excluded:
        response["worktree_excluded"] = worktree_excluded
        response["worktree_excluded_warning"] = getattr(commit_outcome, "stderr", "") or (
            "worktree edits to %s were NOT included -- the staged (index) "
            "version was committed instead" % (", ".join(worktree_excluded),)
        )

    # AC4 (C3, docs/plans/2026-08-14-the-tool-stages-what-it-commits.md):
    # never a silent drop, mirroring `worktree_excluded`'s own posture --
    # surfaced unconditionally on both the committed and uncommitted
    # branches, omitted entirely when `stage_patch` was never supplied or
    # every named path was covered by the patch. Named explicitly rather
    # than left for the caller to infer from absence: a mixed invocation is
    # provenanced for some paths and worktree-sourced for others in the SAME
    # commit, and the plan's own headline claim would be false-by-omission
    # per-invocation without this.
    unprovenanced_paths = list(getattr(result, "unprovenanced_paths", ()) or ())
    if unprovenanced_paths:
        response["unprovenanced_paths"] = unprovenanced_paths

    # Never silent (2026-08-12 fix, see `_collect_untracked_omitted`'s own
    # docstring): surfaced unconditionally, on both the committed and
    # uncommitted branches -- an untracked file beneath a named directory
    # pathspec was never staged either way, and the caller needs to know
    # regardless of whether the rest of the pathspec happened to land.
    # Omitted entirely when nothing was found, keeping the green-path
    # response byte-identical to today's (matches `declined_paths`/
    # `worktree_excluded`'s own omit-when-empty convention).
    if untracked_omitted:
        response["untracked_paths_omitted"] = untracked_omitted

    # AC3 (docs/plans/2026-08-16-authorship-survives-the-sweep.md): never a
    # silent drop, mirroring the rest of this response's additive-key
    # convention -- surfaced unconditionally on both the committed and
    # uncommitted branches (a caller's very next call may retry the same
    # overlapping pathspec), omitted entirely when nothing was found.
    #
    # Deliberately its OWN key, never folded into `response["diagnostics"]`:
    # `coordinator/bin/scoped-git-commit`'s `_explanatory_diagnostics`
    # treats any non-bare `diagnostics` entry on the NOT-committed branch as
    # part of a REFUSED verdict, and `diagnostics` is also the pipeline's
    # own real-failure-detail channel (`_classify_uncommitted`) -- mixing an
    # advisory disclosure into that list would let it get read as (or
    # silently absorbed into) a refusal reason on a call that also happens
    # to fail for an unrelated cause, which is exactly the removed claim
    # gate wearing a new hat (AC4). A dedicated key has no renderer that
    # branches on its presence, in this CLI or any other consumer.

    # C5 (docs/plans/2026-08-19-the-baton-carries-its-commits.md): the
    # commit ledger join. Deliberately the LAST thing this handler does --
    # placed after every response mutation above, in particular after
    # `commit_pipeline._reconcile_landed_despite_failure` (which runs
    # INSIDE `run_commit_pipeline`, above, before `result` is ever returned
    # to this handler) has already folded its reconciled sha into
    # `response["sha"]`. A timed-out-but-landed commit is therefore
    # ledgered under its ACTUAL sha, never a pre-reconcile guess.
    #
    # Hard constraint: a ledger write must never fail the commit it
    # accompanies -- the commit is the durable outcome, the ledger is
    # derived (mirrors `release_committed_claims`'s identical failure
    # direction earlier in this handler). Every step below (classification,
    # owner resolution, the append itself) is wrapped in ONE broad
    # `except Exception` for exactly that reason -- AC3.
    if response["committed"]:
        landed_sha = response.get("sha")
        if landed_sha:
            try:
                from coordinator_core.commit_ledger.resolve_owner import (
                    resolve_owner_handoff_id,
                )
                from coordinator_core.commit_ledger.store import (
                    append_entry as _ledger_append_entry,
                )

                kind, weight_basis = _ledger_kind_and_weight(worktree_root, paths)
                handoff_id, _degraded = resolve_owner_handoff_id(
                    owner_session_id, Path(worktree_root)
                )
                if handoff_id:
                    _ledger_append_entry(
                        handoff_id,
                        landed_sha,
                        kind,
                        weight_basis=weight_basis,
                        cwd=worktree_root,
                    )
                # `handoff_id is None` is the legitimate standalone outcome
                # (`resolve_owner_handoff_id`'s own zero-held-claims arm) --
                # not an error, nothing to bill this commit to, no warning.
            except Exception:
                _LOG.warning(
                    "ceremony.scoped_git_commit: commit ledger write failed "
                    "for %s; the commit already landed and is unaffected",
                    landed_sha,
                    exc_info=True,
                )
        else:
            # sha_unverified / ambiguous-Commit-Token class (see
            # `_commit_landed`'s own comment above, naming a462af36d/
            # 11d1db069): this commit cannot be sha-keyed. SKIP it with a
            # warning rather than inventing a placeholder entry -- entries
            # key on sha, and a sha-less entry could never be deduped or
            # marked reviewed later (AC3b).
            _LOG.warning(
                "ceremony.scoped_git_commit: commit landed but its sha is "
                "unresolvable (sha_unverified=%s) -- skipped from the "
                "commit ledger rather than recorded under a placeholder sha",
                response.get("sha_unverified", False),
            )

    return response


def _declined_paths(result: PipelineResult) -> List[Dict[str, str]]:
    """Every path named in the caller's own pathspec that `explicit_stage()`
    declined to include in the commit set, paired with a human-readable
    reason -- the never-silent-drop report (2026-08-04 fix).

    Scoped by construction: both `StageOutcome.missing_caller_paths` and
    `StageOutcome.ignored_caller_paths` are already filtered to paths this
    call's own `caller_paths` named (`_handler` always passes
    `caller_paths=set(paths)` -- see its `run_commit_pipeline` call site) --
    this function does not re-filter against `paths` itself, it only
    labels the two buckets `explicit_stage()` already computed.

    Deliberately NOT exhaustive over every `StageOutcome.skipped` tag: a
    diverged path, an already-staged-deletion, a staged-deletion source, or
    a swept-rename source are NOT declines -- each is still included in the
    commit set (via `staged_paths`/`deletion_paths`/`swept_renames`), just
    not via a fresh `git add` this call issued. Only a path with genuinely
    NOTHING backing it into `commit_paths` belongs here.

    `result.stage` may be absent -- several tests in this package (and, in
    principle, any future caller) monkeypatch `run_commit_pipeline` itself
    to return a bare `SimpleNamespace` covering only the fields THAT test
    cares about, with no `stage` attribute at all. Missing `stage` degrades
    to "nothing declined" (`[]`) rather than raising -- this function is a
    purely-additive reporting layer over an already-computed result, and
    must never turn a stand-in test double into a hard `AttributeError`.
    """
    stage = getattr(result, "stage", None)
    if stage is None:
        return []
    # 2026-08-10 fix (P1 live incident: a decline reason asserted "not found
    # in the worktree or index" for 54 paths `git status --porcelain`
    # immediately confirmed WERE tracked, modified, and on disk): a path
    # only earns that reason when `explicit_stage()` actually TESTED for it
    # -- see `StageOutcome.unverifiable_missing_caller_paths`'s own
    # docstring. A path in that set was classified "genuinely absent" only
    # because one of the two rename/deletion probes it depends on returned a
    # non-ok `GitResult` and silently degraded to "found nothing" -- the
    # absence was never verified, so the reason says exactly that instead of
    # asserting a fact this call could not confirm.
    unverifiable = set(getattr(stage, "unverifiable_missing_caller_paths", ()) or ())
    declined: List[Dict[str, str]] = []
    for p in stage.missing_caller_paths:
        if p in unverifiable:
            declined.append({
                "path": p,
                "reason": (
                    "could not be classified -- the rename/deletion probe(s) "
                    "this decision depends on did not answer, so absence was "
                    "assumed, not confirmed; re-run once git can be queried "
                    "reliably"
                ),
            })
        else:
            declined.append({
                "path": p,
                "reason": (
                    "not found in the worktree or index, and not attributable "
                    "to a deletion (never existed, or already removed by "
                    "something other than a tracked deletion)"
                ),
            })
    for p in stage.ignored_caller_paths:
        declined.append({"path": p, "reason": "excluded by .gitignore"})
    return declined


#: Mirrors `coordinator/bin/scoped-git-commit`'s own `_BARE_EXIT_CODE_RE`
#: (independently defined here, not imported, so this module carries no
#: coupling to that CLI's actively-changing internals): `commit_pipeline.
#: commit()`'s `not result.ok` branch (the "nothing to commit" no-op)
#: deliberately leaves `CommitOutcome.stderr` in EXACTLY this bare
#: `exit_code=N` shape when git itself printed nothing diagnostic to either
#: stream (confirmed empirically, see that branch's own comment) -- any
#: OTHER stderr content on that same `not result.ok` branch is real git/hook
#: diagnostic text (`condense_git_diagnostic(result.stderr)`), which a
#: rejecting `pre-commit`/`commit-msg` hook populates. Review: code-reviewer
#: -- Finding [P3], 2026-08-15 (chain-ancestry slice): `_classify_
#: uncommitted`'s prior `exit_code == 1`-only reclassifier could not
#: distinguish a genuine no-op from a hook rejection that happened to leave
#: the pathspec byte-identical to HEAD (the porcelain probe alone cannot
#: tell those apart). Gating reclassification on this bare-shape match closes
#: that residual: a hook that prints ANY rejection text is never laundered
#: into the benign no-op, whatever the porcelain probe reports.
_BARE_EXIT_CODE_STDERR_RE = re.compile(r"^exit_code=\d+$")


def _classify_uncommitted(
    worktree_root: str,
    paths: List[str],
    result: PipelineResult,
) -> tuple[bool, List[str], bool]:
    """Split the pipeline's "nothing landed" outcome into genuine-failure vs
    benign already-committed no-op. Returns
    ``(commit_failed, diagnostics, empty_commit_set)``.

    AC10 (docs/plans/2026-08-15-the-ceremony-tail-stops-lying-about-why-it-
    failed.md): on the non-reclassified (real-failure) return path only,
    `result.commit.stdout_diagnostic` -- when non-empty -- is appended to
    `diagnostics`, since a commit-step failure diagnosed on stdout otherwise
    reaches the caller with `stderr`'s bare `exit_code=N` and nothing else.
    The benign no-op `return False, [], True` above is untouched by this --
    `diagnostics` stays `[]` there, unconditionally.

    The pipeline classifies EVERY non-zero `git commit` exit as
    `commit_failed`, including the "nothing to commit" exit 1 its own
    in-branch comment names -- so a re-invocation over paths a previous call
    already committed comes back indistinguishable from a refused commit. See
    this module's docstring, § Idempotency (AC7), for the live incident and
    why the discrimination lives here rather than in `commit_pipeline`.

    Discriminator: `git status --porcelain` over the caller's OWN pathspec,
    read AFTER the pipeline's rollback `finally` has already run. Nothing left
    to commit under it means nothing was lost, whatever git's exit code said.
    Deliberately not a stderr/exit-code text match -- `commit()` returns a
    bare `exit_code=N` for the no-op ON PURPOSE (a downstream renderer keys
    off that exact shape; see `commit_pipeline.commit`'s own comment), so the
    only honest signal left is the repository state itself.

    Untracked files count as "left to commit": that is what keeps the
    `pre-commit`-hook refusal a failure. Its path is rolled back to untracked
    by the pipeline, so the index no longer diverges from HEAD, but the
    content the caller asked to commit is demonstrably still uncommitted.

    Narrow by construction, and fails closed in every direction:
      - only when the COMMIT STEP itself ran and returned non-zero (a gate or
        staging failure leaves `result.commit` None and is never reclassified);
      - only when nothing landed;
      - a git that cannot answer stays `commit_failed=True`;
      - only when the commit step's `exit_code` is the POSITIVE code `git
        commit` itself returns (1, on its own "nothing to commit" no-op) --
        never a negative sentinel. `git_native.py` uses `returncode=-1`
        uniformly for a PYTHON-SIDE refusal that never asked `git commit` to
        run at all (a compare-and-swap refusal in `_agree_branch_cas_
        refusal`, a subprocess that never started) -- `commit_scoped()`'s CAS
        refusal is exactly this shape (2026-08-14,
        state/audits/2026-08-14-scoped-commit-partial-stage-sweep.md): a
        loud, diagnostic-bearing refusal that must never be read as "maybe
        already committed". Reclassifying it here on the strength of
        `_commit_paths_are_clean()`'s own case-sensitive `git status
        --porcelain` probe silently swallowed it back into the SAME benign
        no-op an ordinary idempotent re-commit produces -- the refusal's
        `stderr` never has a way to reach the caller once that happens.
        Narrowing `reclassifiable` to `exit_code == 1` leaves the ordinary
        idempotent-re-commit case (which always surfaces as exit_code 1)
        classified exactly as before, and leaves every non-1 commit-step
        failure -- including this CAS refusal, and any other -1 sentinel --
        `commit_failed=True` all the way out to the CLI, which already
        renders any non-bare `diagnostics` entry as a REFUSED verdict (see
        `coordinator/bin/scoped-git-commit`'s `_explanatory_diagnostics`);
      - only when `result.commit.stderr` is ALSO exactly the bare
        `exit_code=N` shape (`_BARE_EXIT_CODE_STDERR_RE`) `commit()` leaves
        it in on the genuine no-op. `exit_code == 1` alone is not sufficient:
        a rejecting `pre-commit`/`commit-msg` hook also conventionally exits
        1, and while the ordinary hook-rejection case is caught by the
        `_commit_paths_are_clean()` porcelain probe below (a rejected hook
        normally leaves the pathspec still dirty), a hook that reverts its
        own edits on failure -- leaving the tree byte-identical to HEAD --
        would pass that probe too. Any real diagnostic text on `stderr`
        (which a rejecting hook populates) fails this shape check and keeps
        `commit_failed=True`, whatever the porcelain probe says. See
        `test_classify_uncommitted_hook_rejection_with_clean_tree_stays_
        failed` for the pinned case.

    Still earns its keep post-W3 (docs/plans/2026-08-08-a-landed-commit-
    reported-as-failed.md): this probe answers a DIFFERENT question than
    `PipelineResult.sha_unverified` does. `sha_unverified` covers "the commit
    step's own verification could not resolve a sha for the commit it just
    made" (`commit_pipeline.commit()`'s nonce-grep failing loud); this
    function covers "a LATER, separate re-invocation over paths a PRIOR call
    already committed" (the idempotency case, AC7) -- a genuine "nothing to
    commit" `git commit` exit 1 that the pipeline correctly reports as
    `commit_failed=True, landed=False`, which W1/W2 leave untouched by
    design (see `commit_pipeline.CommitOutcome.landed`'s own docstring: the
    ordinary no-op must stay `landed=False`). The two states are mutually
    exclusive at the point this function runs -- `reclassifiable` requires
    `result.committed_sha is None`, which also holds for the (now-tracked-
    separately) `sha_unverified` state, but `_handler` never reaches this
    function on that path: `response["committed"]` is already `True`
    (widened for `sha_unverified` at this handler's own call site) before
    `_classify_uncommitted` is ever invoked.
    """
    commit_failed = result.commit_failed
    diagnostics = list(result.diagnostics)

    # Review: code-reviewer -- Finding [P3], 2026-08-15 (chain-ancestry
    # slice). `exit_code == 1` alone conflates git's own "nothing to commit"
    # no-op with a rejecting `pre-commit`/`commit-msg` hook (hooks
    # conventionally also exit 1) -- the ordinary case is caught by the
    # `_commit_paths_are_clean()` porcelain probe below (a hook rejection
    # normally leaves the pathspec still dirty), but a hook that reverts its
    # own edits on failure, leaving the tree byte-identical to HEAD, would
    # pass that probe too. `_BARE_EXIT_CODE_STDERR_RE` closes the residual:
    # `commit()`'s `not result.ok` branch leaves `CommitOutcome.stderr` in
    # exactly this bare `exit_code=N` shape ONLY when git itself printed
    # nothing diagnostic to either stream (the genuine no-op) -- any hook
    # that rejects the commit populates real diagnostic text there instead
    # (see `_BARE_EXIT_CODE_STDERR_RE`'s own comment), so requiring the bare
    # shape here means a hook that prints ANY rejection text is never
    # reclassified, regardless of what the porcelain probe reports.
    reclassifiable = (
        commit_failed
        and result.committed_sha is None
        and result.commit is not None
        and result.commit.exit_code == 1
        and bool(_BARE_EXIT_CODE_STDERR_RE.match((result.commit.stderr or "").strip()))
    )
    if reclassifiable and _commit_paths_are_clean(worktree_root, paths):
        return False, [], True

    # AC10 (docs/plans/2026-08-15-the-ceremony-tail-stops-lying-about-why-it-
    # failed.md): `commit_pipeline.CommitOutcome.stdout_diagnostic` carries a
    # condensed diagnosis of the commit STEP's stdout, populated unconditionally
    # whenever that step failed -- but `CommitOutcome.stderr` stays bare
    # `exit_code=N` for two matched downstream consumers (this function's own
    # `reclassifiable` shape check above, and `coordinator/bin/scoped-git-
    # commit`'s renderer), so a real failure whose diagnosis landed on stdout
    # would otherwise be reported with no diagnostic text at all. Appended only
    # on THIS fall-through (real-failure) branch, never on the benign no-op
    # `return` above -- that branch's `diagnostics=[]` must stay untouched, or
    # the 2026-08-03 cry-wolf incident this module's docstring documents comes
    # back. Guarded for `result.commit is None` (a gate or staging failure never
    # reaches the commit step, so there is no `stdout_diagnostic` to read) and
    # for an empty/falsy value (nothing to add). Never gated on `stderr`'s bare
    # shape -- a real failure diagnosed only on stdout ALSO leaves `stderr`
    # bare, so that discriminator would suppress exactly the case this exists
    # to surface; `_commit_paths_are_clean()`'s porcelain probe above is the
    # only valid discriminator between "benign" and "real failure" here.
    stdout_diagnostic = getattr(result.commit, "stdout_diagnostic", "") if result.commit is not None else ""
    if stdout_diagnostic:
        diagnostics = diagnostics + [stdout_diagnostic]

    return commit_failed, diagnostics, not diagnostics and not commit_failed


def _commit_paths_are_clean(worktree_root: str, paths: List[str]) -> bool:
    """True iff NO path under *paths* has anything left to commit — no
    worktree modification, no staged change, no untracked content.

    Fails closed to False whenever git declines to answer: an indeterminate
    probe must never be the thing that turns a reported failure into a
    reported success.

    Routed through `git_native._git` rather than a bare `subprocess.run` for
    its `CREATE_NO_WINDOW` flag — this op runs on the commit/session hot path,
    where a per-invocation console window on Windows is break-class. The
    `core.quotepath=false` is the same dialect every other scan in this
    pipeline uses; here it only affects whether a non-ASCII path renders
    C-escaped, and any non-empty output means "not clean" either way.
    """
    if not paths:
        return False
    probe = git_native._git(
        ["-c", "core.quotepath=false", "status", "--porcelain", "--", *paths],
        cwd=worktree_root,
    )
    if not probe.ok:
        return False
    return not any(line.strip() for line in probe.stdout.splitlines())


def _repo_has_remote(worktree_root: Union[str, Path]) -> bool:
    """True when this repo has at least one configured remote.

    One local `git remote` spawn, no network. Used only on the deferred-push
    branch, to keep "no remote" distinguishable from "queued for background
    push" now that the sync push leg (which used to surface that fact as a
    by-product of resolving the upstream) no longer runs.

    Fails OPEN -- an unreadable/erroring git returns True, so the report says
    "queued" rather than asserting a missing remote. The asymmetry is
    deliberate: claiming a remote is absent when the probe merely failed
    would send an operator chasing a configuration problem that does not
    exist, while an over-optimistic "queued" is corrected by the background
    pusher's own logging within the hold window.
    """
    result = git_native._git(["remote"], cwd=str(worktree_root))
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _resolve_push_report(
    push_status: str,
    *,
    push_mode: str = PUSH_MODE_SYNC,
) -> tuple[Optional[bool], str]:
    """Map the pipeline's canonical `push_status` onto this module's tri-state
    report. A pure mapping: no git, no remote, no sleep.

    C-02/C-03 (opro-01) collapsed this. It used to short-circuit on
    DECLINED/PUSHED/NO_REMOTE and route everything else through
    `_remote_sha_state`, a remote-confirmation probe costing 4 git spawns and
    1.0s of `time.sleep` on the push-raced path. The probe existed because
    `push_status` answered "did THIS op's push step sync", which was the wrong
    question while a SECOND publisher existed: `coordinator-auto-push`'s
    post-commit hook detached rather than blocking `git commit`, raced this
    op's own push, and on winning made a landed commit render as a failed one
    (the 2026-07-30 false negative).

    C-01 removed the second publisher instead of correcting for it -- the hook
    stands down for exactly the commits this op publishes itself (see
    `git_native._sole_publisher_env`). With one publisher, `push_status` IS the
    answer rather than a guess about it, and there is nothing left for a remote
    read to overturn:

      - a peer's push cannot carry OUR commit, which is local-only until this
        op pushes it, so a collision with a peer never means "already landed";
      - a non-fast-forward reject is handled upstream by `push_with_retry`'s
        own fetch+rebase+retry, and reaches here only once genuinely exhausted;
      - network/auth failures were never ambiguous.

    `PUSH_STATE_UNCONFIRMED` is retained, NOT collapsed into failure: a push
    that was never attempted (deferred/none mode, or a commit that did not
    land) is genuinely unknown, and rendering unknown as failure is how a
    report starts lying. That rung is the one thing the probe got right and it
    outlives it.

    FIX-I (2026-08-19): `PUSH_STATUS_UNCONFIRMED` -- the push subprocess
    itself timed out, so `push_with_retry` never observed whether it landed
    -- is handled by the same wildcard branch as `PUSH_STATUS_NOT_ATTEMPTED`
    below, and is called out explicitly here rather than left to fall
    through silently: both are genuinely unknown and both render identically,
    but they are not the same fact (a push WAS attempted in the timeout
    case), and a reader diffing this function against `derive_push_status`'s
    own mapping table should find this state named, not just implied.
    """
    if push_status == PUSH_STATUS_DECLINED:
        return None, PUSH_STATE_DECLINED
    if push_status == PUSH_STATUS_PUSHED:
        return True, PUSH_STATE_PUSHED
    if push_status == PUSH_STATUS_NO_REMOTE:
        return None, PUSH_STATE_NO_REMOTE
    if push_status == PUSH_STATUS_FAILED:
        return False, PUSH_STATE_FAILED
    if push_status == PUSH_STATUS_UNCONFIRMED:
        return None, PUSH_STATE_UNCONFIRMED
    # `PUSH_STATUS_NOT_ATTEMPTED` under a non-sync `push_mode` is not unknown
    # at all -- it is this op's designed behaviour, with `hooks/auto_push.py`
    # holding the commit. Reporting it as UNCONFIRMED would put the corrective
    # "re-check, do not re-push" note on every ordinary commit and teach
    # operators to ignore the one state that means a real push is in doubt.
    # Gated on the MODE, not on the status alone: a sync-mode NOT_ATTEMPTED
    # (a commit that never landed) is still genuinely unknown and still falls
    # through to UNCONFIRMED below.
    if push_status == PUSH_STATUS_NOT_ATTEMPTED and push_mode != PUSH_MODE_SYNC:
        return None, PUSH_STATE_DEFERRED
    # Any other unrecognized value falls here.
    return None, PUSH_STATE_UNCONFIRMED
