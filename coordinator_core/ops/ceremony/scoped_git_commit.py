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

`run_commit_pipeline` composes its own session_id-scoped `ceremony_lock`
acquisition and message; this op has no real ceremony session backing it, so
it mints a private per-invocation session_id (`scoped-git-commit-<uuid4>`)
purely as the lock's re-entrancy/liveness-bridge key — never reused across
calls, so it carries no session-registry meaning beyond "this one lock hold".
(2026-08-05, P2 fix: this minted id is passed to `run_commit_pipeline` ONLY
as its `session_id`/lock key, never for attribution — the ACTUAL committing
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

Spec backlink: docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md
§ DEC-3, Wave 1 C1d.

Sink-side ownership enforcement (C4c, AC17/AC18,
docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-classes.md): this
module's `_handler` is the EM-selected insertion point for the ownership-scope
gate, in preference to `run_commit_pipeline` itself, on three independent
grounds:

  1. CONTRACT. `run_commit_pipeline` has three in-process callers — this
     module's `_handler`, `ops/ceremony/wsc_tail.py`, and
     `execute_plan_assemble`/`close_out_and_stamp.py`. The latter two
     legitimately stage ceremony artifacts and the plan document, which are
     not necessarily inside a session's OWN touched-paths scope. An
     ownership gate inside `run_commit_pipeline` would break both of them.
     Gating here keeps `run_commit_pipeline`'s existing contract clean.
  2. COLLISION. `commit_pipeline.py` carries a live peer session's
     uncommitted work at chunk-authoring time; this module does not.
  3. THREAT MODEL. AC18 protects the property "the sink holds against a
     caller that bypasses the PreToolUse guard". The attacker path is an
     obfuscated Bash payload (the AC13 residual —
     `subprocess.run(['g'+'it','com'+'mit'])` defeats any text matcher
     structurally) invoking the `ceremony.scoped_git_commit` op directly.
     That path necessarily goes through this module's `_handler`. Direct
     in-process `run_commit_pipeline` imports (wsc_tail,
     execute_plan_assemble) are claude-klabauter's own trusted ceremony callers, never
     reachable by obfuscating a command line.

Correction to a premise a reviewer flagged and ruled out already:
`run_commit_pipeline`'s existing `caller_paths` parameter (see its docstring
around `missing_caller_paths`/`ignored_caller_paths`) is a
staging-tolerance bookkeeping set that distinguishes caller-supplied paths
from swept/discovered ones for missing/ignored diagnostics. It carries no
ownership or session-scope semantics and is NOT a pre-existing ownership
gate — the check below is new sink-side logic, not a wiring-up of dormant
machinery.

REVERTED 2026-08-03 (P1 security fix, same-day reversal of a same-day
amendment): the gate briefly composed `coordinator_core.ops.session
.scope_report.assert_paths_not_foreign_owned`, a deny-list-shaped predicate
adopted after a live incident where the strict allow-list rejected the EM's
OWN commit of a file its own freshly-dispatched executor had written moments
earlier (`compute_offer` cannot yet attribute a sub-agent's touch to its
dispatching session during the async `.agents/<aid>/em-session-id.txt`
back-pointer race window — see `coordinator_core.session.scope.compute_scope`
Step 3b). That amendment is UNSOUND: `compute_offer`'s own orphan/race-window
pass-through is not scoped to the CALLING session at all — it scans every
currently-dirty path in the repo regardless of who is asking, and a
fabricated `session_id` resolves just as successfully as a real one. Composed
into this sink, that let ANY caller — including a fabricated identity
reaching the sink via the exact obfuscated-payload threat model AC18 exists
to defend against — sweep a genuinely live peer's in-flight race-window path,
or a dead session's leftover uncommitted work, into its own commit. See
`scope_report.py`'s own module docstring ("REVERTED 2026-08-03") for the full
rationale; the permissive predicate has been deleted from that module
entirely rather than left unused.

The gate is back to composing `coordinator_core.ops.session.scope_report
.assert_paths_in_session_scope` — the SAME strict allow-list
`block_subagent_commit` (C4b) uses. A fail-closed gate with occasional
operational friction beats a gate with a hole: the accepted cost is that a
caller invoking this sink during the back-pointer race window described above
can be falsely rejected. The workaround is an explicit `git add -- <paths>` /
`git commit -- <paths>`, which is EM-available and not subagent-available, so
AC17/AC18's caller-confinement property is preserved. Imported wrapped (see
the `try`/`except` around the import below) so an ImportError degrades to a
hard deny rather than either propagating (taking op registration down with
it) or silently disabling the gate.

Ownership-gate observability (`ownership_gate`, 2026-08-04 — reported by
Example-cockpit-repo-em after a live consumer-side probe: the op ran to
completion and emitted exactly one line, `committed 8c246dfadf98 (pushed)`).
Only the DENY path produced any text at all — an `error` string — so a
consumer could not tell whether the gate had evaluated, and could only infer
an allow backwards from a commit having happened. The closing evidence
`docs/plans/2026-08-04-ownership-inherited-at-dispatch.md` AC9 asks for
("the guard ALLOW decision plus the resulting commit sha") was therefore an
artifact the engine structurally could not produce. `_run_ownership_gate`
now returns its verdict alongside the rejection envelope, and `_handler`
attaches it to every response the gate was consulted for — see
`OWNERSHIP_VERDICT_*`.

  NEGATIVE SPEC (hard) for `ownership_gate` — this is a VISIBILITY change
  and nothing else:
    - It does NOT change any allow/deny outcome. Every path that committed
      before still commits; every path that was refused before is still
      refused, with the same `error` text.
    - It adds NO new denial condition, and nothing anywhere gates on the
      new field. It is written, never read, by this module.
    - It never fails the op. An unresolvable owner identity renders as
      `owner_session_id: None` (the tri-state's unknown arm), never as a
      refusal, and a response that predates the field (an older engine
      across the seam) is a valid response, not an error.
    - "The helper was unimportable, so the predicate never ran"
      (`OWNERSHIP_VERDICT_HELPER_UNIMPORTABLE`) and "the pathspec was clean,
      so the gate was skipped" (`OWNERSHIP_VERDICT_NOT_EVALUATED`) stay
      DISTINCT from "evaluated and allowed" (`OWNERSHIP_VERDICT_ALLOWED`).
      Collapsing any of the three into another re-creates the exact
      unobservability this field exists to remove.

Sweeping-pathspec rejection (below the ownership gate, independent of it):
`.`, `./`, `:/`, `:(top)`, a glob (`*`/`?`/`[`), an empty pathspec element,
the repo root, an ancestor of the repo root, or a `-A`/`-a`/`--all` flag
token is rejected REGARDLESS of ownership — a sweeping pathspec is unsafe on
a shared branch even when every path it happens to resolve to right now is
this session's own; the strict own-scope check above does not, and must not,
relax this.
"""

from __future__ import annotations

import logging
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core._settings_home import normalize_native_path
from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.commit_pipeline import PipelineResult, run_commit_pipeline
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope

# C4c (2026-08-03-narrow-subagent-commit-confinement-two-classes.md):
# imported wrapped so an ImportError degrades to `None` rather than
# propagating and taking this whole op's registration down with it --
# `_handler` below hard-denies whenever this is `None`, never treats a
# missing import as "no ownership constraint applies". Mirrors the same
# wrapped-import pattern the C4b guard-side check
# (`coordinator_core.bash_guards.block_subagent_commit`) uses, for the same
# landing-order-safety reason -- composes the SAME strict allow-list the
# guard uses (see this module's own docstring, "REVERTED 2026-08-03", for
# why a separate sink-polarity predicate was tried and reverted). Never
# shell this out as the `session.scope_report` op -- see that module's own
# docstring for why an in-process import is required on this commit-sink's
# fail-closed seam.
try:
    from coordinator_core.ops.session.scope_report import (
        assert_paths_in_session_scope as _assert_paths_in_session_scope,
    )
except Exception:
    _assert_paths_in_session_scope = None

# Advisory-only: used solely to word the orphan-adoption OFFER on a denial
# (see `_run_ownership_gate`) -- never consulted for the verdict itself,
# which is `_assert_paths_in_session_scope` alone. `None` on import failure
# just means the offer wording is skipped; it never changes allow/deny.
try:
    from coordinator_core.ops.session.safe_commit_offer import (
        compute_offer as _compute_offer_for_wording,
    )
except Exception:
    _compute_offer_for_wording = None

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


def _reject_sweeping_pathspec(paths: List[str], worktree_root: str) -> Optional[str]:
    """Return a human-readable rejection reason for the first sweeping
    pathspec element in `paths`, or `None` if none of them are sweeping.

    Checked INDEPENDENTLY of `assert_paths_in_session_scope` (C4c) --
    ownership answers "who does this path belong to", not "does this
    pathspec element name more than one path". A sweeping element is unsafe
    on a shared branch even when everything it currently resolves to happens
    to be this session's own; the ownership check must never be read as
    having relaxed this.

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

    Landed 2026-08-04 on a example-doctrine-repo-em FYI memo (`2026-08-04-example-doctrine-repo-em-
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
    (worktree_root)` expression -- the ownership gate's `owner_session_id`,
    `_handler`'s `attribution_session_id`, and `_handler`'s post-commit
    `owner_session_id` for `release_committed_claims` -- is spelled once,
    not three times: a future edit to the precedence rule (e.g. adding a
    fourth tier) now has one call site to keep in sync, not three.
    """
    return params.get("session_id") or session_core.resolve_session_id(worktree_root)


def _partition_clean_dirty(
    worktree_root: str, paths: List[str]
) -> tuple[List[str], List[str]]:
    """Partition *paths* into `(clean_subset, dirty_subset)` -- the PER-PATH
    variant of `_commit_paths_are_clean` the mixed-pathspec fix needs
    (SC-DR-019: "a scoped-commit refusal is per-path, not per-commit").

    Deliberately reuses `_commit_paths_are_clean` (probed once per path)
    rather than adding a THIRD cleanliness probe alongside it and the
    now-removed `_ownership_gate_paths_are_clean` alias -- both answered the
    exact same underlying question ("does git think there is anything left
    to commit under this pathspec"), just over a whole pathspec at once
    instead of per element. Order of `paths` is preserved within each
    returned list.

    Fails closed the same way the shared primitive does: any path git
    cannot answer for is treated as dirty (not clean), never silently
    dropped from either list.
    """
    clean: List[str] = []
    dirty: List[str] = []
    for p in paths:
        if _commit_paths_are_clean(worktree_root, [p]):
            clean.append(p)
        else:
            dirty.append(p)
    return clean, dirty


def _dirty_tracked_files_under(worktree_root: str, dir_path: str) -> List[str]:
    """Return the dirty TRACKED files `git status --porcelain` currently
    reports beneath *dir_path*, repo-relative, order preserved.

    Never untracked (`??`) -- a directory pathspec silently sweeping an
    untracked file into a commit because someone named its parent directory
    is precisely the harm the scope guard exists to prevent (see this
    module's `_expand_directory_pathspecs` docstring). A rename line
    (`R  old -> new`, or the analogous `RM`/`MR` staged+worktree pairing)
    reports its NEW path -- the one that will actually exist post-commit.

    Read via `git_native._git` (not a bare `subprocess.run`) for the same
    `CREATE_NO_WINDOW` reason `_commit_paths_are_clean` is -- this runs on
    the same commit/session hot path.

    Fails closed to `[]` (never a discovered-directory content) on any git
    failure -- an unresolvable probe must never be read as "nothing dirty
    here" AND ALSO never invent members that were never actually reported.
    """
    probe = git_native._git(
        ["-c", "core.quotepath=false", "status", "--porcelain", "--", dir_path],
        cwd=worktree_root,
    )
    if not probe.ok:
        return []
    expanded: List[str] = []
    for line in probe.stdout.splitlines():
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


def _expand_directory_pathspecs(worktree_root: str, paths: List[str]) -> List[str]:
    """Expand every directory-shaped element of *paths* to its dirty TRACKED
    member files, so the normal classification path (ownership gate,
    clean/dirty partition, staging) sees individual files rather than an
    unclassifiable directory string.

    Live incident this closes (2026-08-06): a caller named a directory of
    125 rewritten tracked JSON records and was refused -- "unclaimed/never
    classified by compute_offer" -- because `compute_offer`/
    `assert_paths_in_session_scope` classify individual dirty paths, never a
    directory string, and orphan adoption matches on named file paths too.
    The only way through was hand-pasting 125 explicit paths.

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

    Per-path classification survives expansion by construction: each
    expanded member is just another string in the returned list, gated
    individually by `_run_ownership_gate` exactly like any caller-named file
    -- a directory that expands to 10 files where 2 belong to a live peer
    still denies on those 2, named individually, not on the directory.
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
        members = _dirty_tracked_files_under(worktree_root, p)
        if not members:
            _append(p)
            continue
        for m in members:
            _append(m)
    return expanded_paths


def _directory_denial_note(paths: List[str], worktree_root: str) -> str:
    """Explain a denial that still carries an unexpanded directory element
    (B): `_expand_directory_pathspecs` only leaves a directory unresolved
    when it found no dirty TRACKED member to expand to (empty, fully clean,
    or containing only untracked/ignored content) -- name that explicitly so
    a caller does not have to reverse-engineer "directory vs. file" from an
    ownership-shaped denial that says nothing about it.

    Returns "" when none of *paths* is currently a directory on disk.
    """
    root = Path(worktree_root)
    dirs = [p for p in paths if isinstance(p, str) and (root / p).is_dir()]
    if not dirs:
        return ""
    return (
        " -- %s %s a directory pathspec with no dirty TRACKED file beneath "
        "it to resolve to (untracked/ignored content is never swept in by "
        "directory expansion): name the individual file path(s) directly" % (
            ", ".join(repr(d) for d in dirs),
            "is" if len(dirs) == 1 else "are",
        )
    )


def _include_orphans_ineffective_note(
    include_orphans: bool, directory_note: str
) -> str:
    """Explain why `include_orphans: true` did not change the outcome (B) --
    the second half of the incident this module's docstring closes: a
    caller re-invoked with `--include-orphans` after an unexplained refusal
    and got the SAME unexplained refusal back, with nothing new to act on.

    Only fires when the caller already opted in (`include_orphans` True) and
    the denial persists. When `directory_note` is non-empty, the directory
    explanation already covers it (an unresolved directory is never an
    orphan-adoptable shape -- orphan matching is on named file paths) and
    this stays silent rather than duplicating the same fact two ways.
    """
    if not include_orphans or directory_note:
        return ""
    return (
        " -- --include-orphans made no difference here: the denied path(s) "
        "are claimed by a live peer session, not an orphan; orphan adoption "
        "never overrides a peer claim"
    )


def _orphan_offer_suffix(owner_session_id: str, worktree_root: str, paths: List[str]) -> str:
    """Word the orphan-adoption OFFER for a denied pathspec, or return `""`
    when no offer applies.

    Advisory-only classification, entirely independent of the verdict itself
    (`_assert_paths_in_session_scope` already decided that) -- a failure here
    degrades to no offer, never to a changed allow/deny outcome. Offers ONLY
    when EVERY path this call would deny is an orphan (claimed by no session
    at all) -- a mixed pathspec (one orphan, one peer-claimed, or one
    unclassified) makes no offer at all, since `include_orphans: true` would
    not resolve the non-orphan half anyway.

    Review: staff-eng F5 -- the predicate previously read
    `any_orphan and not any_peer_owned`, which offers whenever at least one
    path is an orphan and none is peer-claimed -- true even for a pathspec
    like `["orphan.md", "never-classified.md"]`, where the offered remedy
    (`include_orphans: true`) would still deny on the second path. Corrected
    to `all(...)`, matching this docstring.
    """
    if _compute_offer_for_wording is None or not owner_session_id:
        return ""
    try:
        offer = _compute_offer_for_wording(owner_session_id, worktree_root)
    except Exception:
        return ""

    orphan_set = set(offer.get("orphans") or [])
    str_paths = [p for p in paths if isinstance(p, str)]
    if not str_paths or not all(p in orphan_set for p in str_paths):
        return ""
    return (
        " -- re-invoke with include_orphans: true to include the orphan "
        "path(s) (claimed by no session, not a live peer)"
    )


#: `ownership_gate.verdict` values -- the four-valued ownership-gate
#: observability discriminator, modelled on `push_state` below (same
#: convention: module-level `#:`-documented constants, and an explicit arm
#: for every state including the ones a two-valued report would have to
#: guess at). The invariant it encodes: an ALLOW is an OBSERVABLE decision,
#: not something a consumer infers backwards from a commit having happened.
#: A gate that was never consulted, and a gate whose predicate could not
#: even be imported, are each their own arm -- neither is an allow, and
#: neither is a deny the caller can act on.
OWNERSHIP_VERDICT_ALLOWED = "allowed"
OWNERSHIP_VERDICT_DENIED = "denied"
OWNERSHIP_VERDICT_HELPER_UNIMPORTABLE = "helper-unimportable"
OWNERSHIP_VERDICT_NOT_EVALUATED = "not-evaluated-clean-pathspec"

#: Response key carrying the verdict record built by `_ownership_verdict`.
OWNERSHIP_GATE_KEY = "ownership_gate"


def _ownership_verdict(
    verdict: str, owner_session_id: Optional[str], include_orphans: bool
) -> Dict[str, Any]:
    """Build the `ownership_gate` record: WHAT the gate decided, WHO it
    resolved the caller to, and WHETHER orphan adoption was in effect.

    `owner_session_id` is tri-state by construction -- a real session id, or
    `None` for "not resolved" (the gate was skipped, or the ambient identity
    came back empty). An empty string is normalized to `None` rather than
    rendered as an identity that resolved to nothing: `""` reads as an answer
    and is not one.

    Purely descriptive. See this module's docstring, § Ownership-gate
    observability, for the negative spec -- nothing in this op reads the
    record back, and no allow/deny outcome depends on it.
    """
    return {
        "verdict": verdict,
        "owner_session_id": owner_session_id or None,
        "include_orphans": bool(include_orphans),
    }


def _run_ownership_gate(
    params: dict, worktree_root: str, paths: List[str], include_orphans: bool = False
) -> tuple[Optional[dict], Dict[str, Any]]:
    """Run the C4c ownership-scope gate (AC17/AC18) against *paths*.

    Returns `(rejection, verdict)`. `rejection` is `None` when the gate
    passes -- the caller proceeds to staging -- and the handler's rejection
    envelope when it does not, so both call sites in `_handler` (the upfront
    gate, and the TOCTOU re-check
    immediately before staging -- see `_handler`'s own "TOCTOU closure"
    comment) share one gate implementation rather than drifting apart, and
    both MUST pass the same `include_orphans` value -- that shared-gate
    property is deliberate.

    `include_orphans` (default `False`, orphan-adoption opt-in — example-doctrine-repo
    doctrine, scoped-safety-commits.md:131): threaded straight through to
    `assert_paths_in_session_scope`'s `allow_orphans` kwarg. Does NOT relax
    the peer-claimed case -- a path claimed by a live peer session still
    denies regardless (incident 62e9a1f73); see that function's own
    docstring for why the two classes cannot collide.

    On an orphan-only denial (every denied path is an unclaimed orphan, none
    peer-claimed), the error is an OFFER: it names the path an orphan and
    tells the caller that re-invoking with `include_orphans: true` will
    include it. On a peer-claimed denial -- or a mixed pathspec with even one
    peer-claimed path -- no offer is made; the peer-claimed rejection wins
    and the error just names the owning session and stops.

    Resolves the CALLING session's own identity, never the private
    per-invocation `scoped-git-commit-<uuid4>` id `_handler` mints for
    `run_commit_pipeline`'s ceremony_lock -- that id carries no
    session-registry meaning, and using it here would make every call
    trivially "own" whatever it asked to stage, defeating the check.
    `params.get("session_id")` mirrors `session.scope_report`'s own handler
    (C4a): an explicit override takes precedence over resolving the
    environment's ambient session identity.

    `verdict` is the always-returned `_ownership_verdict` record, on the
    allow path as much as the deny path -- the observability the ALLOW path
    previously had none of (see this module's docstring, § Ownership-gate
    observability, including its negative spec). The fail-closed
    unimportable-helper branch below reports
    `OWNERSHIP_VERDICT_HELPER_UNIMPORTABLE`, never
    `OWNERSHIP_VERDICT_DENIED`: nothing was evaluated there, and a consumer
    reading the record must be able to tell an unrun predicate from a
    predicate that ran and refused.
    """
    if _assert_paths_in_session_scope is None:
        return (
            {
                "committed": False,
                "sha": None,
                "pushed": None,
                "error": (
                    "ceremony.scoped_git_commit: rejected -- ownership-scope "
                    "helper is unimportable, failing closed"
                ),
            },
            _ownership_verdict(
                OWNERSHIP_VERDICT_HELPER_UNIMPORTABLE, None, include_orphans
            ),
        )

    owner_session_id = _resolve_committing_session_id(params, worktree_root)
    allowed, deny_reason = _assert_paths_in_session_scope(
        owner_session_id, paths, worktree_root, allow_orphans=include_orphans
    )
    if not allowed:
        offer_suffix = ""
        if not include_orphans:
            offer_suffix = _orphan_offer_suffix(owner_session_id, worktree_root, paths)
        directory_note = _directory_denial_note(paths, worktree_root)
        ineffective_note = _include_orphans_ineffective_note(
            include_orphans, directory_note
        )
        return (
            {
                "committed": False,
                "sha": None,
                "pushed": None,
                "error": (
                    "ceremony.scoped_git_commit: rejected -- path(s) outside "
                    "session scope: %s%s%s%s"
                    % (deny_reason, offer_suffix, directory_note, ineffective_note)
                ),
            },
            _ownership_verdict(
                OWNERSHIP_VERDICT_DENIED, owner_session_id, include_orphans
            ),
        )
    return None, _ownership_verdict(
        OWNERSHIP_VERDICT_ALLOWED, owner_session_id, include_orphans
    )


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

#: `_remote_sha_state()` verdicts.
_REMOTE_PRESENT = "present"
_REMOTE_ABSENT = "absent"
_REMOTE_UNKNOWN = "unknown"


@register_op("ceremony.scoped_git_commit")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'ceremony.scoped_git_commit' handler.

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
          "ownership_gate": {       # present on EVERY response the gate was
                                     # consulted for, allow and deny alike --
                                     # an ALLOW is an observable decision,
                                     # never inferred backwards from a commit
                                     # having happened
            "verdict": str,         # one of OWNERSHIP_VERDICT_*
            "owner_session_id": str | None,  # None = not resolved
            "include_orphans": bool,
          },
        }

        `pushed`/`push_state` answer "is this commit published", NOT "did
        this op's own push step do the publishing" -- a concurrent
        `coordinator-auto-push` post-commit push that wins the race and
        lands the sha yields `PUSH_STATE_PUSHED`, because from every
        caller's point of view the commit IS on the remote. Rendering that
        case as a failure is the false negative this tri-state exists to
        kill (example-doctrine-repo-em memo, 2026-07-30).

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

    On a validation error (missing/empty required param):
        {"committed": False, "sha": None, "pushed": None, "error": str}

    On a rejected pathspec (C4c, AC17): when the calling session's identity
    is unresolvable, its scope is unreadable/malformed, or any element of
    `paths` is outside the calling session's own scope (per
    `coordinator_core.ops.session.scope_report.assert_paths_in_session_
    scope` — the SAME strict allow-list the C4b guard-side check uses; see
    that function's own docstring), the call is rejected BEFORE any staging
    happens. The SAME rejection shape covers
    a sweeping pathspec element (`.`, `./`, `:/`, `:(top)`, a glob, an empty
    element, the repo root, an ancestor of it, or `-A`/`-a`/`--all`) —
    checked independently of ownership, see `_reject_sweeping_pathspec`:
        {"committed": False, "sha": None, "pushed": None, "error": str}
    — the same shape as a validation error; the reason is folded into
    `error` rather than given its own key, since this is still "the call
    was invalid", just discovered one step later.

    `include_orphans` (bool, optional, default `False`): when a denied path
    is an orphan (dirty, claimed by no session at all — never a path a live
    peer session claims, which stays a hard rejection regardless of this
    flag; incident 62e9a1f73), re-invoking with `include_orphans: true`
    admits it. Threaded to BOTH the upfront ownership gate and the TOCTOU
    re-check immediately before staging (see `_run_ownership_gate`) — the
    same value at both call sites is deliberate, not incidental.
    """
    worktree_root_raw = params.get("worktree_root")
    paths_raw = params.get("paths")
    message = params.get("message")
    prose_raw = params.get("prose", "")

    if not worktree_root_raw:
        return {
            "committed": False,
            "sha": None,
            "pushed": None,
            "error": "ceremony.scoped_git_commit: 'worktree_root' param is required",
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
    include_orphans = bool(params.get("include_orphans", False))

    # Directory-pathspec expansion (2026-08-06 fix, live incident -- see
    # `_expand_directory_pathspecs`'s own docstring): a directory element
    # with dirty TRACKED content beneath it is replaced by that content
    # BEFORE anything downstream (sweeping check, ownership gate, staging)
    # ever sees the directory string -- everything below this point
    # operates on the expanded pathspec. A directory with nothing to expand
    # to is left unchanged and falls through to the existing hard
    # directory-pathspec rejection unchanged.
    paths = _expand_directory_pathspecs(worktree_root, paths)

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

    # Ownership-scope gate (C4c, AC17/AC18) -- resolved from the CALLING
    # session's own identity, never from the private per-invocation
    # `scoped-git-commit-<uuid4>` id minted below. That id exists solely as
    # `run_commit_pipeline`'s ceremony_lock re-entrancy key and carries no
    # session-registry meaning -- using it here would make every call
    # trivially "own" whatever it asked to stage, defeating the check.
    # `params.get("session_id")` mirrors `session.scope_report`'s own
    # handler (C4a): an explicit override takes precedence over resolving
    # the environment's ambient session identity.
    #
    # Clean-pathspec bypass, PER-PATH (SC-DR-019: "a scoped-commit refusal
    # is per-path, not per-commit").
    #
    # Live incident, 2026-08-05, which this partition exists to prevent
    # recurring: a caller named two paths -- one dirty and in-scope, one
    # already committed moments earlier by `pickup-assemble apply`. The
    # bypass was evaluated over the WHOLE pathspec, so the single dirty path
    # made `paths_were_clean` False and dragged the already-clean path
    # through a gate that structurally cannot classify it. The commit was
    # refused, naming the clean path "unclaimed/never classified by
    # compute_offer" -- an ownership-shaped message for what was really a
    # nothing-to-commit condition, routing the operator toward
    # `--include-orphans` (which cannot help: a clean path is never in
    # `orphans`) instead of toward dropping the path. `compute_
    # offer` (and therefore `assert_paths_in_session_scope`) only ever
    # classifies DIRTY paths -- see `compute_scope`'s own docstring, Steps
    # 1-5. A path with nothing left to commit (already-landed idempotent
    # re-invocation, AC7; or a caller-named path that never existed/was
    # never dirty) is structurally invisible to that classification, and is
    # not a sweep of anyone's work either -- there is no uncommitted content
    # to sweep. Partitioned via `_partition_clean_dirty` (per-path variant
    # of `_commit_paths_are_clean`, reused rather than a third probe): the
    # ownership gate runs ONLY over the dirty subset; the full, ORIGINAL
    # pathspec is still passed to `run_commit_pipeline` below unchanged --
    # this bypass only ever narrows what reaches `_run_ownership_gate`, it
    # never narrows what gets staged/committed.
    #
    # TOCTOU closure (review finding, 2026-08-03): a point-in-time clean
    # probe can be raced -- a peer's write can land in the window between
    # this probe and `run_commit_pipeline`'s own staging step, so a path
    # skipped here as "nothing to sweep" could be genuinely dirty foreign
    # content by the time it is actually staged, with NO ownership check
    # ever having run against it. The bypass itself stays (AC7 idempotency
    # is a real, polarity-independent need `compute_offer` structurally
    # cannot serve any other way -- see the comment above), but it is never
    # the SOLE reason a path is allowed: immediately before staging, below,
    # the probe is repeated over the CLEAN subset only, and anything that
    # turned dirty in the interim is routed back through the same ownership
    # gate before `run_commit_pipeline` is ever called.
    ownership_gate = _ownership_verdict(
        OWNERSHIP_VERDICT_NOT_EVALUATED, None, include_orphans
    )

    clean_paths, dirty_paths = _partition_clean_dirty(worktree_root, paths)

    if dirty_paths:
        rejection, ownership_gate = _run_ownership_gate(
            params, worktree_root, dirty_paths, include_orphans
        )
        if rejection is not None:
            rejection[OWNERSHIP_GATE_KEY] = ownership_gate
            return rejection

    # Re-probe immediately before staging (see "TOCTOU closure" above),
    # scoped to the paths this call's FIRST probe found clean -- the dirty
    # subset above was already gated (or, if empty, there was nothing to
    # gate). If any of the clean subset is no longer clean now, something
    # landed in the race window and must be gated exactly as if it had been
    # dirty from the start -- never staged on the strength of a stale probe.
    if clean_paths:
        _, now_dirty = _partition_clean_dirty(worktree_root, clean_paths)
        if now_dirty:
            rejection, race_gate = _run_ownership_gate(
                params, worktree_root, now_dirty, include_orphans
            )
            if rejection is not None:
                rejection[OWNERSHIP_GATE_KEY] = race_gate
                return rejection
            ownership_gate = race_gate

    session_id = f"scoped-git-commit-{uuid.uuid4().hex}"

    # 2026-08-05 fix (P2, plan "touched.txt sibling-path escape and the
    # suppressed absorbed-peer-claims trailer"): the minted `session_id`
    # above is `run_commit_pipeline`'s ceremony_lock re-entrancy key ONLY
    # (AC7 -- see this module's own docstring, "one wrapper" section) and
    # STAYS that; it carries no session-registry meaning, so no `started_at`
    # is ever written for it and `_derive_absorbed_peer_claims_trailer`'s own
    # `compute_offer` call previously received it anyway, silently emitting
    # "" on EVERY invocation (SC-DR-019 never fired). Resolve the ACTUAL
    # committing session's identity the same way the C4c ownership gate
    # above already does -- reusing that resolver, INCLUDING its
    # `params["session_id"]` override precedence, rather than inventing a
    # second one -- and thread it separately as `attribution_session_id`.
    attribution_session_id = _resolve_committing_session_id(params, worktree_root)

    result = run_commit_pipeline(
        worktree_root,
        session_id=session_id,
        subject=str(message),
        prose=str(prose_raw or ""),
        stage_paths=paths,
        caller_paths=set(paths),
        attribution_session_id=attribution_session_id,
    )

    response: Dict[str, Any] = {
        "committed": result.committed_sha is not None,
        "sha": result.committed_sha,
        "pushed": result.pushed,
        OWNERSHIP_GATE_KEY: ownership_gate,
    }

    # A bare {"committed": False, "sha": None, "pushed": False} is returned for
    # THREE materially different outcomes -- the benign already-committed no-op,
    # a staging/gate failure, and an integrity breach -- and a caller reading
    # only `committed` cannot tell them apart. Surfacing the pipeline's own
    # outcome predicates is additive to the response and changes no commit
    # semantics. Withheld on the success path so the green response stays the
    # thin three-key shape callers already parse.
    if response["committed"]:
        pushed, push_state = _resolve_push_report(
            worktree_root, result.committed_sha, result.pushed
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
            owner_session_id = _resolve_committing_session_id(params, worktree_root)
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
        # most ONE extra `git ls-tree` subprocess, and only when this
        # session's own touched.txt names a claimed path currently absent
        # from disk -- the common case (every claimed path still exists, or
        # was already retired by `release_committed_claims` above) costs
        # zero subprocesses: it returns after the in-memory `claimed`/
        # `candidates` scan finds nothing absent. A phantom claim is the
        # rare, bug-residue case this exists to clean up, not a per-commit
        # steady-state cost.
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
    declined: List[Dict[str, str]] = []
    for p in stage.missing_caller_paths:
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
    pipeline_pushed: Optional[bool],
) -> tuple[Optional[bool], str]:
    """Map the pipeline's two-valued push outcome onto the tri-state report.

    `pipeline_pushed` (`commit_pipeline.derive_pushed_tristate`) answers "did
    THIS op's push step sync", which is the wrong question whenever another
    publisher exists: `coordinator-auto-push`'s post-commit hook detaches
    (os.fork()/detached Popen) rather than blocking `git commit` to completion,
    so it races this op's own push. When it wins, this op's `git push` collides
    with it, fails for a reason that is not a fast-forward reject, and
    `derive_pushed_tristate` reports False on a commit that is on the remote.

    So a False from the pipeline is treated as UNKNOWN, not as failure, and the
    remote itself decides. Only `_REMOTE_ABSENT` renders as a failed push.
    """
    if pipeline_pushed is True:
        return True, PUSH_STATE_PUSHED
    if pipeline_pushed is None:
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
        )
        if upstream.returncode != 0:
            return _REMOTE_UNKNOWN  # no upstream configured — nothing to violate
        upstream_ref = upstream.stdout.strip()

        for attempt in range(attempts):
            contains = subprocess.run(
                ["git", "merge-base", "--is-ancestor", sha, upstream_ref],
                cwd=worktree_root, capture_output=True, text=True, timeout=2,
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
