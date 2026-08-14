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
warn AC1d called for: `_warn_recent_edits`, below, logs (never gates,
pauses, or prompts) when a path in the caller's pathspec was EDITED
within `_RECENT_EDIT_WARN_WINDOW_SECS` by a live peer session -- "someone's
hands are on it right now".

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
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.git import divergence as git_divergence
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.commit_pipeline import (
    PUSH_STATUS_DECLINED,
    PUSH_STATUS_NO_REMOTE,
    PUSH_STATUS_NOT_ATTEMPTED,
    PUSH_STATUS_PUSHED,
    PipelineResult,
    run_commit_pipeline,
)
from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core
from coordinator_core.session import liveness as session_liveness
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

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

#: Recency-of-EDIT warn window (C1d, AC1d,
#: state/audits/2026-08-13-edit-recency-spike.md). Grounded in OBSERVED
#: edit cadence, not machine metrics -- staff-eng finding 7 asked for this
#: to be derived from hook write latency plus claim-index rebuild time
#: (how fast the machinery runs); DECLINED, because what the warn tracks
#: is how long a human-shaped editing burst lasts, a different quantity
#: those numbers don't measure. Observed: edits land roughly every 2s
#: while an executor is actively working, and a quiet stretch past ~20s
#: usually means that executor is done editing for now -- 30s sits just
#: past that quiet threshold, so the warn covers an active editing run and
#: goes silent once one has ended. Corollary (PM, stated directly): no EM
#: needs longer than 30s to get a commit in, so a window this size cannot
#: strand anyone. This is a SOFT threshold on observed behaviour, not a
#: derived bound -- freely tunable if the observed cadence changes.
_RECENT_EDIT_WARN_WINDOW_SECS = 30


def _warn_recent_edits(
    worktree_root: str,
    paths: List[str],
    caller_session_id: str,
    now: Optional[datetime] = None,
) -> None:
    """Log a WARN (never gate, pause, or prompt) when a path in `paths` was
    EDITED, within `_RECENT_EDIT_WARN_WINDOW_SECS`, by a live peer session
    other than `caller_session_id` -- "someone's hands are on it right
    now" (C1d, AC1d, module docstring's "Nothing in this file replaces it").

    Reads `claim_index.lookup()`'s `.edit_ts` (C1d widening -- see that
    module's docstring), never disk mtime: `touched.txt` T-events are
    EDIT-only by construction (the hook that writes them fast-exits outside
    `Write|Edit|MultiEdit|NotebookEdit`, state/audits/2026-08-13-edit-
    recency-spike.md finding 1), so a bare filesystem touch with no
    matching T-event produces no warn here, regardless of how recent it is.

    Per-claimant `edit_ts` is read AS-IS (DR-296, PM ruling -- see
    `claim_index.py`'s RECENCY-OF-EDIT WARN section): it is the FIRST edit
    of that claimant's current claim run, not its latest, and this
    function does nothing to correct that under-firing -- a session mid-
    edit for longer than the window goes silent here, deliberately.

    `now`, when given, pins the comparison instant for tests exercising
    the window's edges; production callers never pass it (defaults to
    `datetime.now(timezone.utc)`).

    Every failure mode here (an index that could not answer, a liveness
    check that raises, anything else) is swallowed to a debug log line,
    never re-raised -- a warn that could fail the commit it accompanies
    would be the same defect in softer clothes.
    """
    try:
        now_ts = now if now is not None else datetime.now(timezone.utc)
        result = claim_index.lookup(paths, cwd=worktree_root)
        for path in paths:
            path_edit_ts = result.edit_ts.get(path)
            if not path_edit_ts:
                continue
            for sid, ts in path_edit_ts.items():
                if sid == caller_session_id or ts is None:
                    continue
                age = (now_ts - ts).total_seconds()
                if age < 0 or age > _RECENT_EDIT_WARN_WINDOW_SECS:
                    continue
                if not session_liveness.session_live(sid, cwd=worktree_root):
                    continue
                _LOG.warning(
                    "ceremony.scoped_git_commit: %s was edited %ds ago by "
                    "live peer session %s -- committing anyway",
                    path,
                    int(age),
                    sid,
                )
    except Exception:  # noqa: BLE001 -- advisory-only, must never gate the commit
        _LOG.debug(
            "ceremony.scoped_git_commit: recency-of-edit warn check failed, ignoring",
            exc_info=True,
        )


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

#: `_remote_sha_state()` verdicts.
_REMOTE_PRESENT = "present"
_REMOTE_ABSENT = "absent"
_REMOTE_UNKNOWN = "unknown"


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
          "commit_failed": bool,       # True iff a gate or the commit step
                                        # itself failed; False for a benign
                                        # no-op (already-committed/empty pathspec)
          "diagnostics":   list[str],  # gate/commit failure detail, [] on a
                                        # benign no-op
          "reason":        str,        # present only for the empty-commit-set
                                        # no-op ("reason": "empty-commit-set")

        Conditionally present (only when a genuine push-invariant violation
        is confirmed — see `_sha_missing_from_remote`):
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

    # Resolved ONCE, here, and reused by the post-commit `release_committed_
    # claims` call further down -- the CALLING session's own identity, never
    # the private per-invocation `scoped-git-commit-<uuid4>` nonce minted
    # just below for `run_commit_pipeline` (see that mint's own comment).
    owner_session_id = _resolve_committing_session_id(params, worktree_root)

    # C2's ownership gate call site (`_check_claim_conflicts`) was removed
    # outright 2026-08-13 (docs/plans/2026-08-13-claim-release-deadlock-and-
    # the-doctrine-that-rejects-it.md, C1) -- see this module's docstring,
    # "Sink-side ownership enforcement". Nothing GATES here in its place.
    # `_warn_recent_edits` (C1d, AC1d) is advisory-only -- a log line, never
    # control flow -- and runs unconditionally below.
    _warn_recent_edits(worktree_root, paths, owner_session_id)

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
    )

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
        "committed": result.committed_sha is not None or result.sha_unverified,
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
            worktree_root, result.committed_sha, push_status
        )
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
    # `result.integrity_breach` is derived by the pipeline as "committed locally
    # AND pushed is False", which over-fires wherever something other than this
    # op's own push step does the pushing -- a post-commit auto-push hook wins
    # the race, the op's own push then collides with it and reports failure, and
    # the commit is on the remote despite the predicate. `_resolve_push_report`
    # has already confirmed the sha against the remote, so the breach reduces to
    # its verdict: only a CONFIRMED-absent sha is a breach. An unconfirmable or
    # absent remote is not -- there is no remote invariant to violate -- so this
    # fails closed to "no breach".
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


def _classify_uncommitted(
    worktree_root: str,
    paths: List[str],
    result: PipelineResult,
) -> tuple[bool, List[str], bool]:
    """Split the pipeline's "nothing landed" outcome into genuine-failure vs
    benign already-committed no-op. Returns
    ``(commit_failed, diagnostics, empty_commit_set)``.

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
      - a git that cannot answer stays `commit_failed=True`.

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

    reclassifiable = (
        commit_failed
        and result.committed_sha is None
        and result.commit is not None
        and result.commit.exit_code != 0
    )
    if reclassifiable and _commit_paths_are_clean(worktree_root, paths):
        return False, [], True

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


def _resolve_push_report(
    worktree_root: str,
    sha: Optional[str],
    push_status: str,
) -> tuple[Optional[bool], str]:
    """Map the pipeline's canonical `push_status` onto this module's tri-state
    report.

    A policy decline (`push_status == PUSH_STATUS_DECLINED`) short-circuits
    HERE, before any remote probe: it is known and deliberate, not unknown --
    `commit_pipeline` already decided the branch policy, and re-deriving it
    from remote state would be exactly the second copy this plan exists to
    eliminate. `PUSH_STATUS_NO_REMOTE` maps directly to `PUSH_STATE_NO_REMOTE`
    for the same reason -- nothing to probe.

    Everything else (`PUSH_STATUS_PUSHED` aside) falls through to the remote
    probe below, unchanged from before this signature took `push_status`:
    `push_status` answers "did THIS op's push step sync", which is the wrong
    question whenever another publisher exists -- `coordinator-auto-push`'s
    post-commit hook detaches (os.fork()/detached Popen) rather than blocking
    `git commit` to completion, so it races this op's own push. When it wins,
    this op's `git push` collides with it, fails for a reason that is not a
    fast-forward reject, and the pipeline reports a status other than
    `PUSH_STATUS_PUSHED` on a commit that is on the remote.

    So anything other than a confirmed decline/no-remote/pushed status is
    treated as UNKNOWN, not as failure, and the remote itself decides. Only
    `_REMOTE_ABSENT` renders as a failed push.
    """
    if push_status == PUSH_STATUS_DECLINED:
        return None, PUSH_STATE_DECLINED
    if push_status == PUSH_STATUS_PUSHED:
        return True, PUSH_STATE_PUSHED
    if push_status == PUSH_STATUS_NO_REMOTE:
        return None, PUSH_STATE_NO_REMOTE

    state = _remote_sha_state(worktree_root, sha)
    if state == _REMOTE_PRESENT:
        return True, PUSH_STATE_PUSHED
    if state == _REMOTE_ABSENT:
        return False, PUSH_STATE_FAILED
    return None, PUSH_STATE_UNCONFIRMED


def _remote_sha_state(
    worktree_root: str,
    sha: Optional[str],
    *,
    attempts: int = 3,
    retry_delay_s: float = 0.5,
) -> str:
    """Tri-state: is *sha* on the tracked remote branch, absent from it, or
    unknowable?

    Retries the merge-base check a few times (bounded, ~1-2s total) before
    concluding absence -- the detached auto-push described in
    `_resolve_push_report` may still be landing, so a single point-in-time
    check can race it. The retry absorbs that completion window without
    turning this into an unbounded wait; both subprocess calls are local ref
    lookups with no network I/O. It narrows the race, it does not eliminate it
    -- which is exactly why the residual is `_REMOTE_UNKNOWN` and never a
    manufactured failure.
    """
    if not sha:
        return _REMOTE_UNKNOWN
    try:
        upstream = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            cwd=worktree_root, capture_output=True, text=True, timeout=2,
            **no_console_creationflags(),
        )
        if upstream.returncode != 0:
            return _REMOTE_UNKNOWN  # no upstream configured — nothing to violate
        upstream_ref = upstream.stdout.strip()

        for attempt in range(attempts):
            contains = subprocess.run(
                ["git", "merge-base", "--is-ancestor", sha, upstream_ref],
                cwd=worktree_root, capture_output=True, text=True, timeout=2,
                **no_console_creationflags(),
            )
            if contains.returncode == 0:
                return _REMOTE_PRESENT
            if contains.returncode == 1 and attempt < attempts - 1:
                time.sleep(retry_delay_s)
                continue
            # 1 = definitively not an ancestor; anything else is git failing to
            # answer, which is unknown rather than absent.
            return _REMOTE_ABSENT if contains.returncode == 1 else _REMOTE_UNKNOWN
        return _REMOTE_UNKNOWN  # unreachable in practice; keeps the contract explicit
    except Exception:
        # A bug in the probe itself (not just an unreachable remote) would
        # otherwise silently and permanently suppress breach reporting with
        # no diagnostic trace -- log it, but never manufacture a failure out
        # of a failed probe.
        _LOG.debug("_remote_sha_state probe failed", exc_info=True)
        return _REMOTE_UNKNOWN


def _sha_missing_from_remote(
    worktree_root: str,
    sha: Optional[str],
    *,
    attempts: int = 3,
    retry_delay_s: float = 0.5,
) -> bool:
    """True only when *sha* is CONFIRMED absent from the tracked remote branch.

    The boolean collapse of `_remote_sha_state`, kept because "is this a
    breach" is genuinely two-valued at the call site: an unknowable remote
    fails closed to "no breach" exactly like a present sha does. Callers that
    must distinguish the two — anything that RENDERS a push outcome to a human
    — want `_remote_sha_state` instead, because collapsing unknown into the
    same bucket as one of the certainties is how a report starts lying.
    """
    return (
        _remote_sha_state(
            worktree_root, sha, attempts=attempts, retry_delay_s=retry_delay_s
        )
        == _REMOTE_ABSENT
    )
