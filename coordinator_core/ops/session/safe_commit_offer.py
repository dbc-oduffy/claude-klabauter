"""
coordinator_core.ops.session.safe_commit_offer — automatic, unattended
stop-event commit+push for a session's own claimed dirty paths.

PIVOT (PM ruling, 2026-07-31, superseding this module's original "offer,
then one confirmation" shape): "I get annoyed when I'm asked if there should
be a commit or not. y'all are the engineers... I don't want work lost." The
confirmation step this module originally computed FOR is gone — being asked
whether to commit was itself the defect. Module filename kept as-is (the
pathspec computation this name describes is unchanged) even though it no
longer merely "offers" — see `auto_commit_session` below for the part that
changed.

The REAL failure mode this exists to prevent, per the PM, restated: not
primarily cross-session sweeps (those are accepted collateral of a many-EM
workflow — see below) but sessions finishing real work and never committing
it at all. A dirty tree with 40+ uncommitted paths, some from sessions that
are simply gone, is data loss waiting on a machine failure. So the stop-event
TRIGGER reliability matters as much as the computation — see
`docs/wiki/scoped-safety-commits.md` § 3b and the SessionEnd hook script this
module's CLI is designed to be called from
(`coordinator/hooks/scripts/sessionend-auto-commit.py`, DoE-claude side).

Composition, not new computation — same two primitives as before:
  - `coordinator_core.session.scope.compute_scope` — session-vs-session
    ownership set math (`my_scope` / `skipped` / `orphans`).
  - `coordinator_core.session.claims.my_agent_touched` — unions a dispatched
    sub-agent's own fan-out into the session's claim.

CRITICAL — `my_agent_touched` mode MUST be `"exact"`, never `"broadened"`.
Empirically confirmed (2026-07-31, on a live repo with 5 concurrent
sessions): `"broadened"` returns an IDENTICAL union for every session id
tested — it is candidate-set-only, not per-session attribution, by design
(its own docstring: candidate set widens to every live session). `"exact"`
gave genuine per-session attribution on the same corpus (0/1/0/2/14 owned
paths across the 5 sessions). Driving an UNATTENDED auto-commit off
`"broadened"` would hand the same dirty paths to whichever session's
stop-event fires first and commit them under that session's message — an
automated reproduction of the exact bare-commit sweep failure this
mechanism exists to prevent. `coordinator_core.bash_guards.dispatch_checks`
uses `"broadened"` at its own scope-staging WARNING (never commits, always
human-reviewed before a `git commit` lands) — a materially different risk
profile; that choice is not a precedent for an unattended committer and is
NOT replicated here. (That guard's own broadened-mode false-positive
behavior — "likely owned by session X" against a file the calling session's
own sub-agent just wrote — is a separate, pre-existing defect on a
DIFFERENT surface; out of scope for this module, flagged for separate
routing.)

A DIFFERENT hazard, NOT resolved by the exact/broadened choice above: a
dirty file with NO `touched.txt` record anywhere (a peer crashed before
writing one, a record was pruned/rotated, or a session never ran the touch
hook) is invisible to `my_agent_touched` in EITHER mode — that function can
only return what some `.agents/*/touched.txt` actually recorded. Such a
file instead flows through `compute_scope`'s OWN pre-existing Step-2 mtime
fallback: if its mtime is `>= my own started_at`, it is indistinguishable
from "a file I touched but the hook missed recording" and joins MY
`safe_paths`. This is orthogonal to this module's mode choice, pre-dates it
entirely, and is the same resolution direction `coordinator-safe-commit`'s
own default path has always used — see
`test_mtime_fallback_orphan_adoption_is_now_declined_by_compute_scope` for
a live witness.

SUPERSEDED disposition (docs/decisions/DR-246-*, 2026-07-31): the mtime
fallback above is narrowed elsewhere in this workstream (C1a,
`coordinator_core.session.scope`) so an unclaimed dirty file no longer
silently joins another session's `safe_paths` merely by having a recent
mtime. The residual case this module now handles is the ADOPTION-DECLINED
side of that narrowing: a dirty file that is nobody's claimed work is
withheld from `safe_paths`, reported as an `excluded` orphan
("untouched by this session"), and — since the unattended SessionEnd
hook only inspects this CLI's exit code and never its stdout — also
written to the `sessionend-auto-commit-diagnostics.log` sink (see
`_log_excluded_diagnostic`) so a human reading the close actually sees it.
This is NOT a return to the old "ask for confirmation" shape (still gone,
per the PIVOT above): the file is simply left uncommitted and named, never
gated or blocked (DR-227 — advisory only).

Path-format fix, SUPERSEDED 2026-07-31 (touched-path-poisoning-normalize-
git-dir plan, C0/C2, atomic pair) — `.agents/<aid>/touched.txt` entries (the
sub-agent fan-out `my_agent_touched` reads) were PREVIOUSLY believed to be
recorded relative to the PLUGIN directory (`<repo>/coordinator`); that
belief was only ever true by accident, because the writer
(`coordinator_core.hooks.track_touched_files`) was itself poisoned — handed
the git COMMON dir (`<repo>/.git`) as `git_root` instead of the worktree
root — and every entry it wrote came out `../`-prefixed as a side effect of
that bug. Joining onto `coordinator/` and normalizing happened to cancel
that accidental prefix back out to the true repo-relative path. C2 fixes
the writer to emit clean repo-relative entries directly (no `../` prefix at
all); this function now treats every entry as ALREADY repo-relative, the
same dialect as session-keyed `touched.txt` entries, with NO plugin-dir
join. Keeping the old join here after C2 would silently turn a real entry
like `state/foo.md` into `coordinator/state/foo.md` — a path that exists in
no claude-klabauter checkout — dropping every peer sub-agent claim out of
`compute_scope`'s Step 3b `other_owner` map and widening `my_scope` in the
unsafe direction (the exact hazard this fix exists to prevent; see
docs/plans/2026-07-31-touched-path-poisoning-normalize-git-dir.md § C0).
An entry that escapes the repo root after normalization (e.g.
`../../../../../private/tmp/...`, an out-of-repo scratch path, or a
cross-repo `../sibling-repo/...` path) falls OUTSIDE this repo and is
dropped — it can never be a valid pathspec here, cross-repo or not. A
directory entry (trailing `/`) is expanded to the individual dirty files
currently under it (git-queried, not assumed) so it gets the same
candidate-level ownership check as everything else; a bare directory string
would otherwise never equal any per-file dirty-path candidate and would
silently attribute nothing (safe direction, but incomplete).

Read-only halves stay read-only; the mutating half
(`auto_commit_session`/`auto_commit_session_async`) is the ONLY part of this
module that stages, commits, or pushes — and it does so by composing the
ALREADY-EXISTING `ceremony.scoped_git_commit` op in-process (via
`coordinator_core.ipc.get_op_handler`), never a hand-rolled `git commit`.
That op already pushes-with-retry as part of its own contract (see its
module docstring) — auto-push for this mechanism is therefore the SAME seam
`scoped-git-commit` already uses, not a second push path. It is also already
written to coexist with `coordinator_core.hooks.auto_push`'s post-commit
git hook (`_resolve_push_report`'s docstring: a `False` from its own push
step is treated as UNKNOWN, not failure, deferring to the confirmed remote
state) — the two are non-conflicting if both happen to be installed.

Multi-session overlap on the SAME file remains accepted collateral, by
explicit PM ruling — this module does not attempt conflict resolution for
two sessions that both legitimately touched one file; it only prevents ONE
session's unattended auto-commit from sweeping a peer's UNRELATED work.

Spec backlink: DoE-claude state/sizings/2026-07-31-safe-commit-offer-at-
session-stop-events.yaml
"""

from __future__ import annotations

GENERATES = []  # only direct file write is an append to coordinator-sessions/logs/sessionend-auto-commit-diagnostics.log under the git common dir; actual commits delegate to the ceremony.scoped_git_commit op, not written here

import asyncio
import json
import posixpath
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional, Sequence, TypedDict

from coordinator_core.ops.dirty_tree_gate import parse_porcelain_paths
from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.session.claims import my_agent_touched
from coordinator_core.session import scope as scope_module
from coordinator_core.session.scope import compute_scope

class ExcludedPath(TypedDict):
    path: str
    reason: str


class PeerOwnedPath(TypedDict):
    """One path this call attributes to a NAMED peer/agent claim -- the
    serialized-for-the-wire projection of one `coordinator_core.session.
    scope.OwnerFact` entry (a `NamedTuple`, which cannot itself cross an op
    boundary as JSON). WIRE SHAPE, decided here (C5,
    docs/plans/2026-08-05-in-process-writers-declare-their-writes.md): one
    dict per attributed path, fields named identically to `OwnerFact`'s own
    (`owner`/`liveness`/`claim_source`) plus the `path` key itself, so a
    consumer never has to re-derive the mapping key -> value pairing by hand.
    `liveness`/`claim_source` mirror `OwnerFact`'s own `Literal` value sets
    verbatim -- see that class's docstring in `coordinator_core.session.scope`
    for what each value means.

    Never constructed for a claim whose `claim_source` is `"unreadable"` --
    see `_compute_ownership`'s docstring for why (AC7: never print an owner
    this call cannot stand behind)."""

    path: str
    owner: str
    liveness: Literal["live", "dead", "undetermined"]
    claim_source: Literal["session", "agent", "agent-race"]


class OwnershipReadout(TypedDict):
    """The four-bucket, per-session ownership readout (C5) -- EXTENDS the
    post-commit residue report C3 shipped (`AutoCommitReport.residue`,
    `SafeCommitOffer.excluded`), it does not replace either. Those stay
    candidate-set-only, by their own docstrings; this is the first surface
    on this module that answers "who does compute_scope's own `attribution`
    sidecar say owns this path", not merely "is this path safe to adopt".

    Buckets, mutually exclusive by construction:
      - ``mine``         — this session's own claimed dirty paths. Identical
        to `SafeCommitOffer.safe_paths` at the same call -- duplicated here
        (not a reference to the sibling key) so a caller consuming ONLY this
        one key gets a complete, self-contained four-bucket answer without
        also having to thread `safe_paths` through separately.
      - ``peer``          — every OTHER dirty path this call could attribute
        to a NAMED peer/agent claim, via `ScopeResult.attribution`
        (`coordinator_core.session.scope.OwnerFact`, UNGATED by liveness or
        the clean-path prune -- see that class's own docstring). A claim
        whose `OwnerFact.claim_source` is `"unreadable"` is NEVER placed
        here (AC7) -- it has no resolvable owner identity, so it renders as
        ``unattributed`` instead, same as a path with no claim at all.
      - ``unattributed``  — every other non-mine dirty path this call saw
        (an `excluded` entry with no resolvable, non-`"unreadable"`
        `OwnerFact`). Per DR-258 (`ScopeResult.orphans`'s own docstring)
        this bucket can NEVER be emptied by any heuristic: a peer's
        Bash-authored write carries no claim anywhere this function or
        `compute_scope` can read, and is genuinely indistinguishable from
        nobody's file. Do not chase it toward zero -- it is the honest
        default, not residue to optimise away (see `_compute_ownership`).
      - ``degraded``      — mirrors `SafeCommitOffer.indeterminate`
        (`ScopeResult.indeterminate`) for THIS call. `True` means at least
        one claim set this call needed was unreadable or an agent-race
        overlap was unresolved -- when set, ``peer`` is returned empty
        OUTRIGHT and every non-mine path this call saw (including one that
        DOES have a resolvable `OwnerFact` from some earlier, still-valid
        read) is folded into ``unattributed``: this call cannot stand
        behind ANY owner name it would otherwise print (AC7's binding
        requirement), so it renders the honest degraded shape rather than a
        partially-trustworthy one. A caller SHOULD render a call-wide
        banner when this is `True` rather than silently trusting `peer`
        (empty) as "nobody else has claims" -- it means "this call could not
        tell you", not "there are no peer claims"."""

    mine: List[str]
    peer: List[PeerOwnedPath]
    unattributed: List[str]
    degraded: bool


class SafeCommitOffer(TypedDict):
    session_id: str
    safe_paths: List[str]
    excluded: List[ExcludedPath]
    orphans: List[str]
    indeterminate: bool  # staff-eng P3 (2026-08-03, pass 3) — mirrors
    # ScopeResult.indeterminate (coordinator_core.session.scope), surfaced
    # here so a caller composing ONLY compute_offer (this module's own
    # negative-spec forbids calling compute_scope directly) can still
    # distinguish "this path is genuinely unclaimed" from "this call's claim
    # reads were degraded, so adoption was withheld call-wide" — see
    # `assert_paths_in_session_scope`'s deny-reason threading for the
    # consumer this exists for.
    ownership: OwnershipReadout  # C5 (2026-08-05 in-process-writers-declare-
    # their-writes plan) — the four-bucket ownership readout (mine / named
    # peer / unattributed / degraded), extending C3's post-commit `residue`
    # report rather than replacing it. ADDITIVE ONLY: every pre-existing key
    # on this TypedDict is unchanged in shape and meaning (two live sibling
    # plans consume this shape verbatim -- see the plan's § Cross-plan
    # coordination). See `OwnershipReadout`'s own docstring for the bucket
    # contract and `_compute_ownership` for how it is derived from
    # `ScopeResult.attribution`.


class CommitGroup(TypedDict, total=False):
    paths: List[str]  # required in practice; total=False only to make `prose` optional
    message: str  # required in practice; the commit SUBJECT line
    prose: str  # optional — the commit message BODY (see `_commit_group`)


class GroupResult(TypedDict):
    paths: List[str]
    message: str
    committed: bool
    sha: Optional[str]
    push_state: Optional[str]
    error: Optional[str]
    commit_failed: bool  # True iff this group genuinely failed to commit (a
    # gate or the commit subprocess itself) -- False for both a landed
    # commit AND a benign no-op (paths already committed / handler not
    # reached). Distinct from `error`, which is None on the benign no-op
    # even though `committed` is also False there -- see `_commit_group`.
    reason: Optional[str]  # Review: code-reviewer (Finding 3) — the op's own
    # benign-no-op reason (e.g. "empty-commit-set"), threaded through so
    # `_render_report`'s benign branch can say WHY, not merely that it was a
    # no-op. `None` on a landed commit or a genuine `commit_failed`.


class DroppedGroup(TypedDict):
    """One caller-supplied `CommitGroup` (handoff item 1,
    `state/handoffs/2026-08-03-touched-path-bookkeeping.md`) that lost some
    or all of its named paths to `safe_set` filtering in
    `auto_commit_session_async` -- i.e. `len(kept) < len(g["paths"])`,
    total-drop and partial-drop alike. A group that loses every path
    previously vanished from `groups`, `failed_groups`, and `excluded`
    (which is `compute_offer`-derived, not group-derived) all at once --
    silent to both the operator and the diagnostics-log sink. This is that
    group's own record, keyed on the caller-supplied `message` rather than
    the (possibly now-empty) `paths` list, since an all-dropped group has no
    surviving path to key on.

    ``named`` -- how many paths the caller put in this group. ``matched`` --
    how many of them were actually in this session's own computed
    `safe_paths`; `matched < named` is the entry's own reason for existing.
    Never populated for `_default_groups` output (the unattended-trigger
    fallback): that grouping is computed FROM `safe_paths` itself, so it can
    never lose a path to this filter -- see `auto_commit_session_async`."""

    message: str
    named: int
    matched: int


class AutoCommitReport(TypedDict):
    session_id: str
    groups: List[GroupResult]
    excluded: List[ExcludedPath]
    failed_groups: List[GroupResult]  # subset of `groups` with
    # `commit_failed` True -- surfaced separately (2026-07-31 fix) so a
    # caller (the SessionEnd hook) can detect and report a genuine
    # commit/gate failure without re-deriving it from `groups` + `error`,
    # and without conflating it with the benign already-committed no-op
    # shape, which must stay quiet (see module docstring's wolf-crying
    # constraint).
    dropped_groups: List[DroppedGroup]  # handoff item 1 (2026-08-03,
    # touched-path-bookkeeping) -- one entry per caller-supplied `groups`
    # entry that lost some or all of its named paths to the `safe_set`
    # filter, empty when `groups` is `None` (the computed `_default_groups`
    # path can never drop a path it did not itself put there). ADVISORY
    # ONLY, same as `excluded`/DR-227 -- never a gate, never changes
    # `main`'s exit code, and never widens `resolved_groups`/the commit
    # boundary; see `DroppedGroup`'s own docstring for the shape.
    residue: "OrderedDict[str, List[str]]"  # C3 (2026-08-05 engine-ops-
    # declare-what-they-write plan) -- REPORT-ONLY, never a gate: every dirty
    # path still present in `git status --porcelain` AFTER the commit groups
    # above landed, MINUS whatever this call actually committed and MINUS
    # any path a live peer session already owns (per a FRESH `compute_offer`
    # re-read taken immediately before residue is computed, post-commit --
    # see `_compute_residue`, Review: code-reviewer Finding 2), grouped by
    # top-level
    # `state/` class. Purely additive: nothing here feeds back into
    # `safe_set`/`resolved_groups` in `auto_commit_session_async`, so it
    # cannot widen the commit boundary (AC4, negative-spec: "do not widen
    # what any ceremony commits"). Empty is the common case and is not an
    # error -- an empty `residue` after a healthy commit is exactly what a
    # correctly-scoped ceremony should leave behind.


# ---------------------------------------------------------------------------
# Path normalization for the `.agents/*/touched.txt` fan-out
# ---------------------------------------------------------------------------


def _normalize_agent_touched_entry(entry: str) -> Optional[str]:
    """Repo-relative-ize one raw `.agents/<aid>/touched.txt` line.

    POST-C2: entries are already repo-relative (the writer,
    `coordinator_core.hooks.track_touched_files`, emits clean repo-relative
    paths directly) — this only normalizes separators/`.`/`..` segments
    in-place and rejects anything that is not, in fact, repo-relative. NO
    join onto a plugin-directory prefix happens here (see module docstring's
    "Path-format fix, SUPERSEDED" note for why that join existed and why it
    is gone).

    Returns ``None`` for an entry that resolves outside this repo (a
    `../`-escaping entry, cross-repo fan-out into a sibling repo, or an
    out-of-repo scratch path) — dropped, never passed through as a
    candidate. A directory entry (trailing ``/``) keeps its trailing slash
    in the return value; the caller expands it.
    """
    if not entry:
        return None
    # Reject absolute-path entries up front — covers all three shapes
    # multi-OS demands: POSIX-absolute, backslash-absolute (pre-
    # normalization), and a Windows drive-letter prefix.
    raw_check = entry.strip()
    if (
        raw_check.startswith("/")
        or raw_check.startswith("\\")
        or (len(raw_check) >= 2 and raw_check[1] == ":" and raw_check[0].isalpha())
    ):
        return None
    entry = entry.replace("\\", "/")
    is_dir = entry.endswith("/")
    stripped = entry.rstrip("/")
    if not stripped:
        return None
    combined = posixpath.normpath(stripped)
    if combined in (".", "..") or combined.startswith("../") or posixpath.isabs(combined):
        return None
    return combined + "/" if is_dir else combined


def _dirty_files_under(dir_path: str, cwd: Optional[str]) -> List[str]:
    """Repo-relative dirty files (tracked-modified ∪ untracked) currently
    under ``dir_path`` — a small, targeted git query (NOT a repo-wide scan)
    used only to expand a directory entry from the agent fan-out into
    individual file candidates. Fails closed (empty list) on any git error.

    ``core.quotepath=false`` is REQUIRED, not cosmetic: these paths are
    matched against ``compute_scope``'s repo-wide dirty set, which sets the
    same flag. With git's default quoting a non-ASCII path would come back
    C-escaped from one scan and raw from the other, so an expanded claim
    would silently fail the membership test and be pruned — widening the
    committer's allow-list, the direction ``compute_scope``'s fail-closed
    invariant forbids. The two scans must share one dialect.
    """
    out: List[str] = []
    for args in (
        ["-c", "core.quotepath=false", "diff", "--name-only", "HEAD", "--", dir_path],
        [
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            dir_path,
        ],
    ):
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=cwd,
                **no_console_creationflags(),
            )
        except OSError:
            continue
        if result.returncode != 0:
            continue
        out.extend(line for line in result.stdout.splitlines() if line)
    return out


def _dirty_files_under_batch(
    dir_paths: Sequence[str], cwd: Optional[str]
) -> "OrderedDict[str, List[str]]":
    """Batched sibling of `_dirty_files_under` — ONE `git diff` + ONE
    `git ls-files` pathspec-batched over every ``dir_paths`` entry, instead
    of that same pair of spawns once per directory entry (the N+1 shape this
    chunk exists to retire; see
    `_resolve_agent_touched_candidates`, its sole caller in this module).

    § Anti-scope 1/2/4: this is OBJECT/PATHSPEC batching, not range
    batching — `git status`/`git diff` over multiple pathspecs resolves each
    pathspec independently server-side, so handing git N directory
    pathspecs in one call is safe in a way collapsing N independent
    reachability RANGES into one expression is not (see this module's other
    git calls, which never do the latter).

    Same worktree-dirtiness read as `_dirty_files_under`
    (`git diff --name-only HEAD` ∪ `git ls-files --others
    --exclude-standard`) and the same deliberate omission of
    `--no-optional-locks` — C1's `dirty_tree_gate` batching (commit
    `9e3084df78e5`, `commit_gates.py`) made the identical choice for the
    identical reason (§ Anti-scope 14): suppressing the lock can leave
    phantom-dirty state permanently unresolved, which is worse than paying
    the lock cost here.

    Returns an `OrderedDict` keyed by EVERY entry of ``dir_paths``, in the
    order first seen, duplicates collapsed onto their first occurrence —
    never a subset. § Anti-scope 25 (absence must never silently read as
    "clean"): a directory entry present in the input is ALWAYS present as a
    key in the output, with an EMPTY list value meaning "queried, found
    nothing dirty under it" — this function never uses key-absence to mean
    either "clean" or "not asked"; a key is absent from the return value
    only when it was never in ``dir_paths`` to begin with. Mirrors
    `emit/sections/handoffs.py`'s `_resolve_shipped_in_dates` shape (a
    pre-seeded map plus prefix-match against the batched query output),
    cited per this chunk's brief rather than re-derived.

    A file may be reported under more than one ``dir_paths`` entry if the
    entries themselves nest (e.g. ``"a/"`` and ``"a/b/"`` both present) —
    each pathspec resolves independently per the anti-scope note above, so
    each entry's own membership test is answered on its own terms.

    Fails closed (every value empty) on any git error, same as
    `_dirty_files_under` — a git hiccup must never widen a caller's
    candidate set.
    """
    result: "OrderedDict[str, List[str]]" = OrderedDict()
    for d in dir_paths:
        result.setdefault(d, [])
    if not result:
        return result

    unique_dirs = list(result.keys())
    raw_files: List[str] = []
    for args in (
        ["-c", "core.quotepath=false", "diff", "--name-only", "HEAD", "--", *unique_dirs],
        [
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            *unique_dirs,
        ],
    ):
        try:
            proc = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                cwd=cwd,
                **no_console_creationflags(),
            )
        except OSError:
            continue
        if proc.returncode != 0:
            continue
        raw_files.extend(line for line in proc.stdout.splitlines() if line)

    seen_files: set = set()
    for f in raw_files:
        if f in seen_files:
            continue
        seen_files.add(f)
        for d in unique_dirs:
            if f.startswith(d):
                result[d].append(f)
    return result


def _resolve_agent_touched_candidates(session_id: str, cwd: Optional[str]) -> List[str]:
    """Repo-relative, de-duplicated, order-preserving candidate list from
    this session's own dispatched sub-agent fan-out (`"exact"` mode only —
    see module docstring).

    Directory entries (trailing ``/``) are expanded via ONE batched
    `_dirty_files_under_batch` call over every directory entry this
    session's fan-out named, rather than one `_dirty_files_under` spawn
    pair per entry — the N+1 shape this chunk (C16) retires. Per-entry
    order is preserved: a directory entry's expanded files are spliced in
    at that entry's own position, identically to the pre-batch per-call
    behaviour, only the git spawn count changes.
    """
    raw = my_agent_touched(session_id, "exact", cwd)
    normalized: List[Optional[str]] = [_normalize_agent_touched_entry(e) for e in raw]
    dir_entries = [n for n in normalized if n is not None and n.endswith("/")]
    dirty_by_dir = _dirty_files_under_batch(dir_entries, cwd)

    resolved: List[str] = []
    for norm in normalized:
        if norm is None:
            continue
        if norm.endswith("/"):
            resolved.extend(dirty_by_dir.get(norm, []))
        else:
            resolved.append(norm)

    seen = set()
    out: List[str] = []
    for p in resolved:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _compute_ownership(
    result: "scope_module.ScopeResult",
    safe_paths: List[str],
    excluded: List[ExcludedPath],
) -> OwnershipReadout:
    """Derive the four-bucket ownership readout from a single already-
    computed `ScopeResult` and its `compute_offer`-shaped `excluded` list --
    see `OwnershipReadout`'s own docstring for the bucket contract this
    implements.

    Deliberately NOT built from `result.other_owner`: that dict is
    subtraction-only (compute_scope's own docstring: it drops a dead peer's
    claim and a clean-path-pruned claim entirely) and would silently
    under-report a peer that `ScopeResult.attribution` still names. Built
    from `result.attribution` instead -- the UNGATED sidecar that carries
    every peer/agent claim Step 3/3b's loop ever read, including the ones
    `other_owner` had to drop for its own (correct, unrelated) reasons.

    `excluded` is the caller's own `compute_offer`-built list -- every
    non-mine dirty path this call saw, already deduped between the
    `result.skipped` (peer-owned) and `result.orphans` (unattributed)
    shapes (see `compute_offer`'s own docstring for that de-dup). Iterating
    it here, rather than `result.skipped ∪ result.orphans` directly, means
    this function never has to re-derive that de-dup a second time and can
    never disagree with what `excluded` itself reports.

    `result.indeterminate` is checked ONCE, call-wide, before iterating --
    not per-path. AC7's requirement ("never print an owner you cannot stand
    behind") is call-wide, not path-by-path: a claim read that succeeded for
    path A this call says nothing about whether the SAME call's read for
    path B was degraded, and `ScopeResult.indeterminate` itself is already a
    whole-call flag (see that field's own docstring) -- there is no
    per-path finer-grained signal to key off here. When set, `peer` is
    returned empty outright and every excluded path folds into
    `unattributed`, regardless of what `result.attribution` happens to
    contain for it."""
    degraded = bool(result.indeterminate)
    peer: List[PeerOwnedPath] = []
    unattributed: List[str] = []
    for entry in excluded:
        path = entry["path"]
        fact = None if degraded else result.attribution.get(path)
        if fact is not None and fact.claim_source != "unreadable":
            peer.append(
                {
                    "path": path,
                    "owner": fact.owner,
                    "liveness": fact.liveness,
                    "claim_source": fact.claim_source,
                }
            )
        else:
            unattributed.append(path)
    return {
        "mine": list(safe_paths),
        "peer": peer,
        "unattributed": unattributed,
        "degraded": degraded,
    }


# ---------------------------------------------------------------------------
# Pathspec computation (pure, read-only)
# ---------------------------------------------------------------------------


def compute_offer(
    session_id: str,
    cwd: Optional[str] = None,
    *,
    extra_candidates: Optional[Sequence[str]] = None,
) -> SafeCommitOffer:
    """Compute this session's safe pathspec and the excluded-path narration.

    ``safe_paths`` — this session's claimed dirty paths (touched.txt ∪
    mtime-since-start dirty files ∪ this session's OWN dispatched sub-agent
    fan-out, exact-mode only), with any path another live session's touch
    list claims already subtracted by ``compute_scope``.

    ``excluded`` — every OTHER dirty path, each with a reason:
      - ``"owned by session <id>"`` — another session's touched.txt claims
        it. ``<id>`` reads as ``unknown (claims unreadable: ...)`` when that
        session's touched.txt could not be read; fail-closed, so the path is
        withheld rather than assumed safe.
      - ``"untouched by this session"`` — a dirty orphan: not in this
        session's own claim set, not claimed by any other session either.

    ``orphans`` — every dirty path claimed by no session at all (NOT a
    peer-owned path, which stays in ``result.skipped``/``excluded`` instead).
    Purely additive — a path in ``orphans`` is, by construction, NEVER also
    in ``safe_paths`` (``compute_scope``'s Step 4/5 routing never puts the
    same path in both ``my_scope`` and ``orphans``). Added for
    ``coordinator_core.ops.session.scope_report.assert_paths_in_session_scope``'s
    ``allow_orphans`` mode — this function itself makes no policy decision
    about orphan adoption.

    ``indeterminate`` — mirrors ``ScopeResult.indeterminate`` verbatim
    (staff-eng P3, 2026-08-03 pass 3): ``True`` iff THIS call withheld at
    least one claim it could not attribute (an unreadable peer/agent claim
    set, or an unresolved agent-race overlap). Surfaced here — not just
    consumed internally to zero ``orphans`` below — so a caller composing
    ONLY this function (this module's own negative-spec forbids calling
    ``compute_scope`` directly) can still tell "this call's claim reads were
    degraded, so adoption was withheld call-wide" apart from "this path is
    genuinely unclaimed" in its own deny narration. See
    ``coordinator_core.ops.session.scope_report._classify_denied_path`` for
    the consumer this exists for.

    Review: staff-eng F1 (2026-08-03) — this is ``result.orphans`` MINUS
    ``skipped_paths``, NOT ``result.orphans`` verbatim. ``ScopeResult.orphans``
    means "unattributed", not "unclaimed" (see that field's own docstring):
    every fail-closed withhold arm in ``compute_scope`` Step 3/3b/4 (an
    unreadable peer ``touched.txt``, an unresolved sub-agent race-window
    overlap, a liveness-enumeration indeterminacy) also drains into it by
    construction, and every one of those same candidates is ALSO recorded in
    ``result.skipped`` (with a reason naming which withhold arm produced it —
    see ``compute_scope``'s own Step 4/5). Returning ``result.orphans``
    verbatim here let a caller's ``allow_orphans`` mode admit a path
    ``compute_scope`` deliberately withheld rather than genuinely found
    unclaimed (incident 62e9a1f73's degraded-read variant). Subtracting
    ``skipped_paths`` (the same set already computed above for the
    ``excluded`` de-dup) removes exactly the withheld candidates while
    leaving genuine "nobody's claim" orphans untouched.

    Review: staff-eng R1 (2026-08-03, re-review pass 2) — the
    ``skipped_paths`` subtraction above closes the withheld-candidate shape
    ONLY: it can only remove a path that entered ``touched_set`` and
    therefore got a ``skipped`` counterpart from ``compute_scope`` Step 4. A
    dirty path never adopted as a candidate at all this call (``started_at``
    in the future, unreadable, or an mtime predating session start) bypasses
    Step 4 entirely, gets no ``skipped`` entry for the subtraction to
    remove, and reached ``result.orphans`` even while a live peer's claim
    set was unreadable — committing that peer's in-flight file (incident
    62e9a1f73's non-candidate variant). Closed here by reading
    ``result.indeterminate``: when ``compute_scope`` withheld ANYTHING
    unattributably this call (an unreadable claim set, or an unresolved
    agent-race overlap), ``orphans`` is returned empty OUTRIGHT — the whole
    call's orphan set is untrustworthy for adoption, not just the specific
    candidate the subtraction above already knew to exclude. This is a
    wider blast radius than per-path narrowing, deliberately: nothing here
    can attribute WHICH of this call's orphans are actually safe once any
    claim set was unreadable, so withholding all of them is the only sound
    default. The pre-existing liveness-enumeration under-report residual
    (documented in ``compute_scope``'s own docstring, and confirmed NOT
    widened by this fix) is NOT chased here — a peer claim that residual
    releases never sets ``result.indeterminate`` either, so it stays outside
    this function's fix; see ``ScopeResult.indeterminate``'s own docstring.

    ``ownership`` — (C5, additive) the four-bucket per-session ownership
    readout (mine / named peer / unattributed / degraded), derived from
    ``ScopeResult.attribution`` and this same ``excluded`` list. See
    ``OwnershipReadout``'s own docstring for the bucket contract and
    ``_compute_ownership`` for the derivation. Extends, does not replace,
    ``excluded``/``orphans`` above or ``AutoCommitReport.residue`` (C3).

    ``extra_candidates`` — THE CLASSIFICATION SEAM (2026-08-07, scoped-commit
    ownership-gate misclassification). Keyword-only, defaulting to ``None``,
    which reproduces this function's pre-existing behaviour byte-for-byte:
    the candidate set stays this session's ``touched.txt`` union its own
    dispatched sub-agent fan-out. A caller that passes its own pathspec here
    is asking ONE question — "who holds these paths?" — and gets an answer
    only ``excluded``/``ownership`` can carry. It is asking that because a
    path NOBODY in this call's candidate set ever touched is invisible to
    ``compute_scope`` Step 3/Step 4 entirely: it never becomes a candidate,
    so it gets no ``skipped`` entry naming its peer holder, and Step 5 routes
    it away from ``orphans`` too (``other_owner`` has it). It is therefore
    absent from every key of this dict, and a consumer narrating a denial has
    nothing to name. Passing the pathspec as ``extra_candidates`` puts those
    paths through Step 1 adoption so Step 3/3b's peer-claim read is actually
    consulted for them.

    That is also exactly why the RESULT IS NOT AN ALLOW-LIST FOR THE CALLER
    THAT NAMED THE PATHS. Step 1 adoption is unconditional: a named path that
    no peer claims and no ``other_owner`` entry covers is adopted into
    ``my_scope``, and lands in this call's ``safe_paths`` — by construction,
    because the caller named it, not because the caller owns it. Reading
    ``safe_paths``/``orphans`` off such a call as a membership test would let
    any caller own any dirty path in the tree by putting it in its own
    pathspec — the same unsound shape
    ``coordinator_core.ops.session.scope_report``'s module docstring records
    under "REVERTED 2026-08-03", reached by a different route.

    NEGATIVE SPEC: never pass a caller-supplied pathspec to the
    ``compute_offer`` call whose ``safe_paths``/``orphans`` gate a commit. A
    consumer that wants both answers computes TWO offers — the primary one
    (no ``extra_candidates``) for the verdict, a second one for wording only
    — and never merges them. See
    ``coordinator_core.ops.session.scope_report.assert_paths_in_session_scope``
    for the one in-tree consumer of this parameter and the invariant test
    that pins the split.

    Read-only: makes no git or ``touched.txt`` mutation. Raises
    ``ValueError`` if ``session_id`` is empty.
    """
    candidates = _resolve_agent_touched_candidates(session_id, cwd)
    if extra_candidates:
        seen = set(candidates)
        for path in extra_candidates:
            if isinstance(path, str) and path and path not in seen:
                seen.add(path)
                candidates.append(path)
    result = compute_scope(session_id, cwd, extra_candidates=candidates)

    # A candidate withheld via `unreadable_other_sessions` (compute_scope
    # Step 4) lands in BOTH `result.skipped` (owner "unknown (claims
    # unreadable: ...)") and `result.orphans` (Step 5: absent from
    # my_scope_set, and `other_owner.get(dfile)` is None since it was never
    # actually claimed by another session, only indeterminate) -- the same
    # path would otherwise appear twice in `excluded` with contradictory
    # reasons. `skipped` is built first and is the more specific of the two
    # (it names WHY the candidate was withheld, not just that it wasn't
    # adopted), so an orphan already present via `skipped` is dropped here.
    excluded: List[ExcludedPath] = [
        {"path": path, "reason": "owned by session %s" % owner}
        for path, owner in result.skipped
    ]
    skipped_paths = {path for path, _owner in result.skipped}
    excluded.extend(
        {"path": path, "reason": "untouched by this session"}
        for path in result.orphans
        if path not in skipped_paths
    )

    # Review: staff-eng F1 — see this function's own docstring, "orphans"
    # paragraph. Never return `result.orphans` verbatim: it is unattributed,
    # not unclaimed, and a withheld candidate that lands in BOTH
    # `result.skipped` and `result.orphans` must not resurface here as a
    # genuine orphan.
    #
    # Review: staff-eng R1 — the subtraction above only recovers a withheld
    # CANDIDATE. `result.indeterminate` covers the non-candidate shape (and
    # the agent-race non-candidate shape) in one gate: if this call withheld
    # anything unattributably at all, treat every orphan this call reports
    # as unsafe to adopt, not just the ones the subtraction already knew to
    # exclude. See this function's own docstring for the full accounting.
    true_orphans = (
        []
        if result.indeterminate
        else [path for path in result.orphans if path not in skipped_paths]
    )

    return {
        "session_id": session_id,
        "safe_paths": list(result.my_scope),
        "excluded": excluded,
        "orphans": true_orphans,
        "indeterminate": result.indeterminate,
        "ownership": _compute_ownership(result, result.my_scope, excluded),
    }


# ---------------------------------------------------------------------------
# Grouping (mechanical fallback — an EM/ceremony with real judgment should
# prefer passing explicit `groups` instead, see `auto_commit_session`)
# ---------------------------------------------------------------------------


def _default_groups(safe_paths: List[str], session_id: str) -> List[CommitGroup]:
    """Mechanical grouping used ONLY when the caller supplies no explicit
    `groups` — i.e. an unattended trigger (a SessionEnd hook) with no
    EM/human judgment in the loop. Groups by the path's top-two segments (a
    coarse subsystem boundary — `coordinator/skills`, `state/handoffs`, a
    bare top-level dir when there's only one segment) rather than either
    extreme the PM named as worse than no automation: one mega-commit
    spanning unrelated subsystems, or one commit per file. An EM-run
    ceremony (e.g. `/handoff`) should draft real per-group descriptions and
    pass explicit `groups` instead of relying on this fallback — grouping
    "like an engineer would" needs judgment this mechanical layer doesn't
    have; this default only bounds the mechanical case, it doesn't aspire to
    replace authored judgment.

    Subject/body split (PM framing, 2026-07-31: "if I have to commit, it's a
    safety [net] because someone forgot to commit" — this is a SAFETY NET,
    not the primary/deliberate commit path a session makes during its own
    work). The subject stays SHORT and BOUNDED — file count + subsystem key
    + short session id, never an enumerated file list (unbounded at N
    paths, unreadable in a `git log --oneline`). The full path list, and
    which subsystem key grouped them, live in the BODY (`prose`), where
    length costs nothing and a future archaeologist reading `git log` (not
    `--oneline`) gets the complete picture plus the honest safety-net
    framing rather than a commit dressed up as a deliberate, curated change.
    """
    if not safe_paths:
        return []
    buckets: "OrderedDict[str, List[str]]" = OrderedDict()
    for p in safe_paths:
        segments = p.split("/")
        # The DIRECTORY prefix only (drop the filename) -- up to two
        # directory levels deep, e.g. "coordinator/skills" for
        # "coordinator/skills/handoff/SKILL.md". A bare top-level file (no
        # directory) never becomes the bucket key itself -- that would put
        # the exact filename back into the subject line via `key`, the same
        # unbounded-subject shape this split fixes. Joining a 2-segment path
        # like "sub/file.py" on segments[:2] would ALSO reproduce the
        # filename (there IS no directory-only prefix shorter than the
        # whole path) -- segments[:-1] (directories only) is the fix.
        dir_segments = segments[:-1][:2]
        key = "/".join(dir_segments) if dir_segments else "(repo root)"
        buckets.setdefault(key, []).append(p)

    groups: List[CommitGroup] = []
    for key, paths in buckets.items():
        subject = "auto-commit: %d file(s) rescued at session stop (session %s, %s)" % (
            len(paths), session_id[:6], key
        )
        prose = (
            "Stop-event safety net, not a deliberate change — these files were left "
            "uncommitted when session %s ended without committing them itself. This "
            "commit exists so the work is not lost, not to curate history; the good "
            "archaeological commits are the deliberate ones a session makes while it "
            "is still running. See docs/wiki/scoped-safety-commits.md § 3b.\n\n"
            "Grouped under %r (this session's own touch-list claim, subsystem-"
            "bucketed):\n%s"
        ) % (session_id, key, "\n".join("  - %s" % p for p in paths))
        groups.append({"paths": paths, "message": subject, "prose": prose})
    return groups


# ---------------------------------------------------------------------------
# Post-commit residue report (C3, 2026-08-05 engine-ops-declare-what-they-
# write plan) — REPORT-ONLY, read-only. Never stages, never blocks, never
# widens the commit pathspec (AC4). Runs AFTER the commit groups already
# landed, purely to name what git status still shows dirty.
# ---------------------------------------------------------------------------


def _current_dirty_paths(cwd: Optional[str]) -> List[str]:
    """Repo-relative paths `git status --porcelain` reports dirty RIGHT NOW
    -- a fresh, POST-commit re-read, deliberately not reused from any
    pre-commit scan (`compute_scope`'s own dirty-file enumeration runs
    before the commit groups in this same call land). ``core.quotepath=false``
    matches every other git invocation in this module (see
    `_dirty_files_under`'s own docstring for why the two dialects must
    agree). A rename line (`R  old -> new`) reports only the NEW path — the
    old path is gone from the tree and cannot be residue. Fails closed
    (empty list) on any git error, same as `_dirty_files_under`; a residue
    report that under-counts on a git hiccup is safe (REPORT-ONLY, never
    gates), one that raised would not be.

    ``--untracked-files=all`` (not the porcelain default of ``normal``) is
    REQUIRED: an entirely-untracked directory otherwise collapses to one
    ``?? state/`` line naming the directory, not its files — every file
    under it would then vanish from residue entirely rather than being
    named/grouped, exactly the invisibility AC3 exists to fix.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    # Review: code-reviewer (Finding 3) — reuse the ONE sanctioned porcelain
    # parser (`dirty_tree_gate.parse_porcelain_paths`) rather than a second
    # hand-rolled copy; that function's own docstring forbids a second copy.
    # `--untracked-files=all` is set on the `git status` call above, not by
    # the parser — the parser only splits lines already produced by that
    # call, so the all-files behavior this function's docstring promises is
    # unaffected by delegating the line-splitting itself.
    return [path for _xy, path in parse_porcelain_paths(result.stdout) if path.strip()]


def _residue_class(path: str) -> str:
    """Grouping key for the residue report: `state/<subdir>` for anything
    under `state/` (the class the plan's Problem section measures — e.g.
    `state/memo-outbox`, `state/ceremony`), or the bare top-level segment for
    anything else (e.g. `coordinator_core`). Never the full path -- grouping
    is the whole point of bounding this report (see `_compute_residue`)."""
    segments = path.split("/")
    if segments and segments[0] == "state" and len(segments) > 1:
        return "state/%s" % segments[1]
    return segments[0] if segments else path


def _compute_residue(
    session_id: str,
    group_results: List[GroupResult],
    worktree_root: Optional[str],
) -> "OrderedDict[str, List[str]]":
    """What the ceremony left dirty, grouped by `_residue_class` -- the
    diagnostic AC3 exists to add. REPORT-ONLY (AC4): read-only throughout,
    computed strictly AFTER `group_results` already landed, and never fed
    back into any pathspec -- the caller (`auto_commit_session_async`) only
    attaches this dict to the returned report, it never reads it back into
    `safe_set`/`resolved_groups`.

    Attribution before residue (AC5): a dirty path this call did NOT commit
    is first checked against a FRESH `compute_offer(session_id, ...)`
    re-read -- taken here, immediately before residue is computed, NOT the
    `offer["excluded"]` snapshot `auto_commit_session_async` took BEFORE the
    commit groups ran. Review: code-reviewer (Finding 2) -- that earlier
    snapshot predates every `git commit` subprocess call this session just
    ran; a peer session that dirtied a new file, or wrote its own claim for
    an already-dirty file, during that window was invisible to it and would
    render here as this session's own residue -- exactly the harm AC5 exists
    to prevent. Re-deriving ownership fresh, right before the dirty-path
    read below, closes that window: both reads are now taken back-to-back,
    post-commit, so a peer's claim recorded any time up to "now" is
    attributed correctly. A path with a `"owned by session <id>"` reason in
    this fresh read's `excluded` is a live peer's in-flight work, not this
    session's leftover, and is excluded from `residue` entirely rather than
    rendered as something this ceremony "left behind". Reporting a peer's
    file as residue is exactly the harm this module's own scope-narrowing
    exists to prevent (see the plan's C3 body) -- it is how an operator gets
    nudged into a bulk sweep that clobbers a peer's uncommitted work.

    Everything else still dirty after the commit groups landed -- a path
    that never entered `safe_paths` at all (an orphan, "untouched by this
    session"), or one that WAS in `safe_paths` but whose group never
    actually committed (`commit_failed`, or a group the caller's own
    `groups` override dropped) -- is genuine residue.

    Returns an empty (not merely falsy) `OrderedDict` when `worktree_root`
    is unavailable or nothing is left dirty; never raises.
    """
    empty: "OrderedDict[str, List[str]]" = OrderedDict()
    if not worktree_root:
        return empty

    committed_paths: set = set()
    for g in group_results:
        if g.get("committed"):
            committed_paths.update(g["paths"])

    fresh_offer = compute_offer(session_id, worktree_root)
    owned_paths = {
        e["path"]
        for e in fresh_offer.get("excluded") or []
        if str(e.get("reason", "")).startswith("owned by session")
    }

    buckets: "OrderedDict[str, List[str]]" = OrderedDict()
    for path in _current_dirty_paths(worktree_root):
        if path in committed_paths or path in owned_paths:
            continue
        buckets.setdefault(_residue_class(path), []).append(path)
    return buckets


# ---------------------------------------------------------------------------
# Auto-commit + auto-push (mutating — the only part of this module that is)
# ---------------------------------------------------------------------------


async def _commit_group(
    worktree_root: str, group: CommitGroup, session_id: Optional[str] = None
) -> GroupResult:
    """Run one group through `ceremony.scoped_git_commit`, and classify the
    result into `committed` / benign-no-op / genuine-`commit_failed` -- the
    op's own response conflates "committed": False across THREE materially
    different outcomes (see `scoped_git_commit._handler`'s own docstring):
    the benign already-committed no-op (`reason: "empty-commit-set"`,
    `commit_failed: False`), a genuine gate/commit failure
    (`commit_failed: True`, `diagnostics: [...]`, no `error` key at all --
    this was previously swallowed here, since this function only ever read
    `result.get("error")`), and a handler-level validation error (`error`
    set directly, `commit_failed` absent). All three collapse to `error`/
    `commit_failed` values a caller can act on without re-deriving them.
    """
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler("ceremony.scoped_git_commit")
    if handler is None:
        return {
            "paths": group["paths"],
            "message": group["message"],
            "committed": False,
            "sha": None,
            "push_state": None,
            "error": "ceremony.scoped_git_commit not registered",
            "commit_failed": True,
            "reason": None,
        }
    params = {
        "worktree_root": worktree_root,
        "paths": group["paths"],
        "message": group["message"],
        "prose": group.get("prose", ""),
    }
    if session_id:
        # Name the committing session explicitly rather than letting the op
        # re-resolve it from the ambient environment. THIS module already
        # resolved it (`main`'s `--session`, or `core.resolve_session_id`) and
        # computed `safe_paths` FROM it, so an op that re-derives its own
        # answer can disagree with the very scope the pathspec was built from
        # -- and does, whenever the caller passed `--session <id>` for a
        # session other than the ambient one (the unattended SessionEnd-hook
        # shape). The op ignores this key on any build that predates its
        # sink-side scope gate.
        params["session_id"] = session_id
    # `ceremony.scoped_git_commit`'s handler is a PLAIN SYNC `def` as of
    # `3241c7c95` (see that commit for the dispatch-timeout rationale).
    #
    # Called plainly rather than via an await-if-awaitable adapter: handler
    # sync-vs-async is a per-op fact, not a per-call-site unknown — the one
    # place that legitimately branches on it is `ipc.dispatch_message`, which
    # must serve every registered op. A composer resolving ONE named op knows
    # that op's shape, and tolerating both here would hide the same drift a
    # second time instead of failing on it.
    result = handler(params)
    committed = bool(result.get("committed"))
    commit_failed = bool(result.get("commit_failed")) if not committed else False
    error = result.get("error")
    if not committed and commit_failed and not error:
        diagnostics = result.get("diagnostics") or []
        error = "; ".join(str(d) for d in diagnostics) if diagnostics else "commit failed"
    if not committed and error and not commit_failed:
        # A handler-level validation error (missing/empty required param) --
        # never reachable in practice from this module's own callers (every
        # group here always supplies non-empty paths/message), but treated
        # as a genuine failure defensively rather than silently matching the
        # benign empty-commit-set no-op shape.
        commit_failed = True
    return {
        "paths": group["paths"],
        "message": group["message"],
        "committed": committed,
        "sha": result.get("sha"),
        "push_state": result.get("push_state"),
        "error": error,
        "commit_failed": commit_failed,
        "reason": result.get("reason"),
    }


async def auto_commit_session_async(
    session_id: str,
    cwd: Optional[str] = None,
    groups: Optional[List[CommitGroup]] = None,
) -> AutoCommitReport:
    """Compute this session's safe pathspec and commit+push it — NO
    confirmation step, by explicit PM ruling. ``groups`` lets a caller with
    real judgment (an EM-run ceremony) supply its own meaningful
    grouping/messages; any path in a supplied group that is NOT in this
    session's own computed ``safe_paths`` is silently dropped from its group
    — the auto-commit boundary is COMPUTED, never caller-widened. ``None``
    (the default — the unattended-trigger case) uses ``_default_groups``.
    """
    offer = compute_offer(session_id, cwd)
    safe_set = set(offer["safe_paths"])

    dropped_groups: List[DroppedGroup] = []
    if groups is None:
        resolved_groups = _default_groups(offer["safe_paths"], session_id)
    else:
        resolved_groups = []
        for g in groups:
            named_paths = g["paths"]
            kept = [p for p in named_paths if p in safe_set]
            if len(kept) < len(named_paths):
                # Handoff item 1 (2026-08-03, touched-path-bookkeeping) --
                # record the drop, total or partial, BEFORE the `if kept`
                # gate below decides whether the group survives at all. A
                # group that loses every path takes the `if kept` branch's
                # else (never reached, `resolved_groups.append` skipped) and
                # would otherwise leave no trace anywhere in this report.
                dropped_groups.append(
                    {
                        "message": g["message"],
                        "named": len(named_paths),
                        "matched": len(kept),
                    }
                )
            if kept:
                # Review: code-reviewer (Finding 4) — carry the caller-
                # supplied `prose` body through; it was previously dropped
                # here, so only the mechanical `_default_groups` fallback
                # ever produced a commit body.
                resolved_groups.append(
                    {"paths": kept, "message": g["message"], "prose": g.get("prose", "")}
                )

    worktree_root = core.git_root(cwd) or cwd or "."
    group_results = [
        await _commit_group(worktree_root, g, session_id) for g in resolved_groups
    ]
    failed_groups = [g for g in group_results if g.get("commit_failed")]
    residue = _compute_residue(session_id, group_results, worktree_root)

    return {
        "session_id": session_id,
        "groups": group_results,
        "excluded": offer["excluded"],
        "failed_groups": failed_groups,
        "dropped_groups": dropped_groups,
        "residue": residue,
    }


def auto_commit_session(
    session_id: str,
    cwd: Optional[str] = None,
    groups: Optional[List[CommitGroup]] = None,
) -> AutoCommitReport:
    """Sync wrapper — a single ``asyncio.run()`` drives the whole call
    (matches the single-event-loop convention other in-process op composers
    use, e.g. ``coordinator_core.ops.promote_shipped_in_flight_stubs``)."""
    return asyncio.run(auto_commit_session_async(session_id, cwd, groups))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _log_failed_groups_diagnostic(
    worktree_root: str, session_id: str, failed_groups: List[GroupResult]
) -> None:
    """Best-effort write to the SAME
    ``coordinator-sessions/logs/sessionend-auto-commit-diagnostics.log`` file
    the SessionEnd hook (`DoE-claude/coordinator/hooks/scripts/
    sessionend-auto-commit.py._log_diagnostic`) appends to, naming WHICH
    groups failed and why -- not merely that something did. Never raises; a
    diagnostics-write failure must not break the CLI's own exit path.

    Review: code-reviewer (Finding 1) — `failed_groups` was computed and
    tested but never surfaced anywhere a human or the hook actually reads.
    The hook only inspects the subprocess exit code (never stdout), so this
    module's own `main()` must both (a) return a distinct exit code and (b)
    write the failure detail in-process, since nothing downstream parses the
    report body.
    """
    try:
        from coordinator_core.lifecycle import git_common_dir

        common_dir = git_common_dir(Path(worktree_root))
        log_dir = common_dir / "coordinator-sessions" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            "[%s] safe-commit-offer: %d group(s) genuinely failed to commit "
            "for session %s:" % (stamp, len(failed_groups), session_id)
        ]
        for g in failed_groups:
            lines.append(
                "  - %s (%d path(s)): %s"
                % (g["message"], len(g["paths"]), g.get("error") or "commit failed")
            )
        with (log_dir / "sessionend-auto-commit-diagnostics.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        return


#: Bound on how many excluded (declined-adoption) paths get named inline in
#: the diagnostics-log entry -- after C1a's scope narrowing, `excluded` can
#: hold roughly every unclaimed dirty path in the tree on every session
#: close (dozens, observed live on this working tree). Naming a count plus
#: this many paths plus a pointer to the CLI's own bounded `--json`/text
#: output keeps the log entry itself bounded; it is a breadcrumb pointing at
#: full detail, not the full detail (DR-227 -- advisory only, see
#: `_log_excluded_diagnostic`).
#:
#: Review: code-reviewer (Finding 4) — `excluded` is built `skipped`-derived
#: ("owned by session X") entries first, then `orphans`-derived ("untouched
#: by this session") entries (see `compute_offer`). A plain `excluded[:N]`
#: slice is therefore order-dependent: >=N legitimately owned-by-a-peer
#: paths would fill the whole preview and push every newly-declined orphan
#: — the exact case AC6 exists to surface loudly — into the "...and N more"
#: tail. `_log_excluded_diagnostic` deliberately biases the preview toward
#: "untouched by this session" entries first: those paths were NOT visible
#: before this slice (nobody claimed them, nothing else reports them), while
#: "owned by session X" paths were already visible via that owning session's
#: own reporting. Do not "simplify" this back to a plain slice — the preview
#: is intentionally NOT representative of `excluded`'s raw order, only of
#: which reason class is more urgent to surface.
_EXCLUDED_LOG_PREVIEW_COUNT = 10


def _log_excluded_diagnostic(
    worktree_root: str, session_id: str, excluded: List[ExcludedPath]
) -> None:
    """Best-effort write to the SAME
    ``coordinator-sessions/logs/sessionend-auto-commit-diagnostics.log`` file
    `_log_failed_groups_diagnostic` appends to (mirrors its directory
    resolution, exception-swallowing, and append shape exactly) -- naming
    declined-adoption paths in the ONE sink the unattended SessionEnd hook
    can actually surface, since that hook only inspects the subprocess exit
    code and never reads this CLI's stdout (see `_log_failed_groups_diagnostic`'s
    own docstring). Never raises; a diagnostics-write failure must not break
    the CLI's own exit path.

    DR-227 -- advisory ONLY. This never changes `main`'s exit code and must
    never be wired to do so: a declined adoption is the correct, expected
    outcome of C1a's narrowing (per this module's wolf-crying constraint,
    see the module docstring), not a failure. Do not promote this to a gate.
    """
    if not excluded:
        return
    try:
        from coordinator_core.lifecycle import git_common_dir

        common_dir = git_common_dir(Path(worktree_root))
        log_dir = common_dir / "coordinator-sessions" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Review: code-reviewer (Finding 4) — bias the preview toward
        # "untouched by this session" (orphan-derived) entries first; see
        # `_EXCLUDED_LOG_PREVIEW_COUNT`'s comment for why. Total bound is
        # unchanged; only the ordering fed into the slice changes.
        untouched = [e for e in excluded if e["reason"] == "untouched by this session"]
        other = [e for e in excluded if e["reason"] != "untouched by this session"]
        preview = (untouched + other)[:_EXCLUDED_LOG_PREVIEW_COUNT]
        lines = [
            "[%s] safe-commit-offer: %d file(s) declined adoption for "
            "session %s (not committed -- advisory only, DR-227, exit code "
            "unaffected):" % (stamp, len(excluded), session_id)
        ]
        lines.extend("  - %s — %s" % (e["path"], e["reason"]) for e in preview)
        remaining = len(excluded) - len(preview)
        if remaining > 0:
            lines.append(
                "  ... and %d more (see safe-commit-offer --dry-run --json "
                "for the full list)" % remaining
            )
        with (log_dir / "sessionend-auto-commit-diagnostics.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        return


def _log_dropped_groups_diagnostic(
    worktree_root: str, session_id: str, dropped_groups: List[DroppedGroup]
) -> None:
    """Best-effort write to the SAME
    ``coordinator-sessions/logs/sessionend-auto-commit-diagnostics.log`` file
    `_log_failed_groups_diagnostic`/`_log_excluded_diagnostic` append to --
    mirrors both exactly in directory resolution, timestamp format,
    exception-swallowing, and append shape. Handoff item 1 (2026-08-03,
    touched-path-bookkeeping): a caller-supplied group that loses some or all
    of its named paths to the `safe_set` filter previously appeared in
    neither `groups`, `failed_groups`, nor `excluded` -- this is the ONE sink
    the unattended SessionEnd hook can actually surface for it, since that
    hook only inspects the subprocess exit code and never reads this CLI's
    stdout (see `_log_failed_groups_diagnostic`'s own docstring). Never
    raises; a diagnostics-write failure must not break the CLI's own exit
    path.

    DR-227 -- advisory ONLY, same as `_log_excluded_diagnostic`. This never
    changes `main`'s exit code and must never be wired to do so: a dropped
    group means the caller named a path outside its own session's computed
    scope, not a failure of this module's own computation -- see
    `AutoCommitReport.dropped_groups`'s own docstring. Do not promote this to
    a gate.
    """
    if not dropped_groups:
        return
    try:
        from coordinator_core.lifecycle import git_common_dir

        common_dir = git_common_dir(Path(worktree_root))
        log_dir = common_dir / "coordinator-sessions" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines = [
            "[%s] safe-commit-offer: %d caller-supplied group(s) partially or "
            "fully dropped for session %s (named path(s) outside the "
            "computed scope -- advisory only, exit code unaffected):"
            % (stamp, len(dropped_groups), session_id)
        ]
        preview = dropped_groups[:_DROPPED_GROUPS_PREVIEW_COUNT]
        for dg in preview:
            lines.append(
                "  - %s — named %d paths, %d matched"
                % (dg["message"], dg["named"], dg["matched"])
            )
        remaining = len(dropped_groups) - len(preview)
        if remaining > 0:
            lines.append("  ... and %d more group(s)" % remaining)
        with (log_dir / "sessionend-auto-commit-diagnostics.log").open(
            "a", encoding="utf-8"
        ) as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception:
        return


#: How many of a group's own paths get named inline before the rest collapse
#: into a "... and N more" tail. Bounds the report the same way
#: `_EXCLUDED_LOG_PREVIEW_COUNT` bounds the diagnostics-log entry; the full
#: list is always one `--json` away, and for a LANDED group also in the commit
#: body `_default_groups` composes.
_REPORT_PATH_PREVIEW_COUNT = 8

#: How many sample paths get named inline per residue class before the rest
#: collapse into a "... and N more" tail (C3). Mirrors the reasoning behind
#: `_REPORT_PATH_PREVIEW_COUNT` and `_EXCLUDED_LOG_PREVIEW_COUNT`: a per-class
#: COUNT is always shown regardless, this only bounds the sample listed next
#: to it -- a residue class with hundreds of paths (the exact shape the three
#: hand-typed bulk sweeps left behind) must never turn into a per-path dump.
_RESIDUE_CLASS_SAMPLE_COUNT = 3

#: How many residue CLASSES (not paths within a class) get their own line
#: before the rest collapse into a "... and N more class(es)" tail. Review:
#: code-reviewer (Finding 1) -- `_RESIDUE_CLASS_SAMPLE_COUNT` above only
#: bounds the sample listed WITHIN one class; it does not bound the number
#: of classes themselves. `_residue_class` returns the bare top-level
#: segment for a repo-root file with no directory, so a pile of repo-root
#: orphan files (e.g. many `orphan_NNN.py` siblings) each become their own
#: class and the render loop below emitted one line per file -- reproducing
#: the exact "1938-line stdout for a one-file commit" incident this
#: module's docstring says the report exists to retire, for the residue
#: section specifically. Mirrors `_RESIDUE_CLASS_SAMPLE_COUNT`'s reasoning:
#: the per-class COUNT is still shown in the summary line above regardless,
#: this only bounds how many per-class detail lines get printed inline.
_RESIDUE_CLASS_PREVIEW_COUNT = 8

#: How many `dropped_groups` entries (handoff item 1, 2026-08-03
#: touched-path-bookkeeping) get their own line -- in `_render_report`'s
#: stdout and in `_log_dropped_groups_diagnostic`'s log sink alike -- before
#: the rest collapse into a "... and N more group(s)" tail. Same idiom as
#: `_RESIDUE_CLASS_PREVIEW_COUNT`: one line per GROUP, never one line per
#: path -- a caller that named hundreds of groups in one call must not
#: reproduce the 1938-line `excluded` incident this module's docstring
#: already retired once.
_DROPPED_GROUPS_PREVIEW_COUNT = 8


def _commit_changed_count(sha: Optional[str], worktree_root: Optional[str]) -> Optional[int]:
    """How many files the commit ``sha`` actually changed, or ``None`` when
    that cannot be determined.

    Reporting seam only — the number that belongs next to a landed commit is
    what the commit changed, which is NOT the breadth of the pathspec it was
    handed (a path can be in scope with nothing to commit). Uses the same
    ``git show --name-only --format=`` shape as
    ``coordinator_core.session_attribution``'s trailerless-commit classifier
    rather than a second dialect.

    Negative spec: never raises (this runs inside an unattended stop-hook's
    report path), and never runs git against an unknown working directory — a
    missing ``worktree_root`` or ``sha`` is ``None``, not a probe of whatever
    cwd the process happens to be in. An empty diff (a merge commit, or a
    genuinely empty commit) is also ``None``: "0 files changed" asserted
    against a commit that landed is a worse report than an honest
    "unavailable", and the caller's degraded form says so in words.
    """
    if not sha or not worktree_root:
        return None
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotepath=false", "show", "--name-only", "--format=", sha],
            capture_output=True,
            text=True,
            cwd=worktree_root,
            **no_console_creationflags(),
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    count = sum(1 for line in result.stdout.splitlines() if line.strip())
    return count or None


#: Emitted FIRST when `scope.normalize_diagnostic_fired()` is set. First, not
#: last, because `_render_report`'s outcome-last property is pinned by
#: `coordinator/tests/test_safe_commit_offer_outcome_signal.py` — and bounded
#: output means the head of the report is a few lines above the verdict, not
#: buried.
#:
#: The wording tracks EXACTLY what a set latch now implies, no more. Since
#: `scope._ls_files_failure_is_benign` landed (2026-08-05), the latch no longer
#: fires for a path outside this repo — the routine case, which the relpath
#: fallback handles correctly and which used to put this banner at the head of
#: most reports on a non-condition, a false alarm in the top line of the exact
#: report the same commit was fixing for being falsely alarming. What remains
#: is an UNCLASSIFIED normalization failure, whose consequence for this report
#: is precisely: an entry may have been dropped (a path this session wrote is
#: absent from the pathspec) or mis-normalized (present but naming the wrong
#: file) — NOT that anything was mis-committed, and NOT that the entries are
#: known-bad.
_DEGRADED_SCOPE_NOTICE = (
    "DEGRADED INPUT — touched.txt was written this process after an "
    "unexpected path-normalization failure (see stderr: normalize_touch_path); "
    "entries may have been dropped or mis-normalized, so the scope below may "
    "be incomplete or name the wrong path. Routine out-of-repo paths do not "
    "raise this."
)


def _render_report(report: AutoCommitReport, worktree_root: Optional[str] = None) -> str:
    """Render the operator-facing report: detail first, VERDICT LAST.

    Two properties are load-bearing, both from the 2026-08-03 live incident
    (`coordinator/tests/test_safe_commit_offer_outcome_signal.py`):

    Outcome last. Every per-group verdict line is emitted AFTER that group's
    own path detail, and the excluded summary is emitted before any of it.
    Previously the excluded dump came last, so the verdict an operator
    actually needs was buried under it and only findable by grep.

    Bounded output. Nothing here grows one line per excluded path — `excluded`
    held 1938 entries in the live run (a 1938-line stdout for a one-file
    commit). The withholding POLICY is unchanged and correct: it is what stops
    one session's stop-event commit sweeping a peer's work. Only its rendering
    is aggregated — a count, split by reason class, plus a pointer at the
    full list. The `residue` section carries the same bound on TWO axes, not
    one: `_RESIDUE_CLASS_SAMPLE_COUNT` bounds the sample paths listed inside
    one class, and `_RESIDUE_CLASS_PREVIEW_COUNT` (Review: code-reviewer,
    Finding 1) separately bounds how many CLASSES get their own inline line
    before the rest collapse into a "... and N more class(es)" tail — a repo
    with many repo-root residue files (each its own class, per
    `_residue_class`) previously emitted one line per file here, the same
    unbounded shape this paragraph already forbids for `excluded`.
    `dropped_groups` (handoff item 1, 2026-08-03 touched-path-bookkeeping)
    carries the same bound via `_DROPPED_GROUPS_PREVIEW_COUNT` — one line
    per GROUP, never per path.

    A THIRD property, from the example-cockpit-repo-em memo of 2026-08-05: every
    file count is labelled as either the commit's own change count or the
    breadth of the pathspec it was handed, never a bare "N file(s)" readable
    as both. Live, a one-file commit was reported as "14 file(s)" because the
    other 13 in-scope paths had nothing to commit — an inflated number on a
    shared branch raises exactly the alarm ("did this sweep a peer's work?")
    this module exists to retire, settleable only by the `git show --stat` the
    report was meant to save.

    ``worktree_root`` is where the change count is read from; without it (a
    caller rendering a report detached from its repo) the landed-commit line
    degrades to the labelled scope-only form. It never degrades to the old
    ambiguous wording, and never raises.
    """
    lines: List[str] = []
    groups = report["groups"]
    excluded = report["excluded"]

    if scope_module.normalize_diagnostic_fired():
        lines.append(_DEGRADED_SCOPE_NOTICE)

    if excluded:
        owned = sum(1 for e in excluded if e["reason"].startswith("owned by session"))
        untouched = len(excluded) - owned
        lines.append(
            "Excluded %d file(s) — %d claimed by another session, %d untouched by "
            "this session; left uncommitted on purpose (run `safe-commit-offer "
            "--dry-run --json` for the full list)." % (len(excluded), owned, untouched)
        )

    dropped_groups = report.get("dropped_groups") or []
    if dropped_groups:
        # Handoff item 1 (2026-08-03, touched-path-bookkeeping) -- a
        # caller-supplied group whose named paths fell wholly or partly
        # outside this session's computed `safe_paths` is advisory-only
        # (DR-227, same as `excluded` above), never a gate: the group is
        # simply smaller (or absent) from `groups` below, not a failure.
        lines.append(
            "Dropped %d caller-supplied group(s) — named path(s) outside "
            "this session's computed scope, left uncommitted on purpose "
            "(run `safe-commit-offer --dry-run --json` for the full list):"
            % len(dropped_groups)
        )
        shown = dropped_groups[:_DROPPED_GROUPS_PREVIEW_COUNT]
        for dg in shown:
            lines.append(
                "  %s — named %d paths, %d matched" % (dg["message"], dg["named"], dg["matched"])
            )
        remaining = len(dropped_groups) - len(shown)
        if remaining > 0:
            lines.append("  ... and %d more group(s)" % remaining)

    residue = report.get("residue") or {}
    if residue:
        total_residue = sum(len(paths) for paths in residue.values())
        lines.append(
            "Residue: %d file(s) still dirty after this commit, not this "
            "session's peer-owned/committed work, across %d class(es) "
            "(report-only -- nothing staged or blocked; see "
            "`safe-commit-offer --dry-run --json` or `git status` for the "
            "full list):" % (total_residue, len(residue))
        )
        residue_items = list(residue.items())
        shown_classes = residue_items[:_RESIDUE_CLASS_PREVIEW_COUNT]
        for cls, paths in shown_classes:
            sample = paths[:_RESIDUE_CLASS_SAMPLE_COUNT]
            tail = ""
            remaining = len(paths) - len(sample)
            if remaining > 0:
                tail = " (+%d more)" % remaining
            lines.append(
                "  %s: %d file(s) — e.g. %s%s"
                % (cls, len(paths), ", ".join(sample), tail)
            )
        remaining_classes = len(residue_items) - len(shown_classes)
        if remaining_classes > 0:
            lines.append(
                "  ... and %d more class(es) (see `safe-commit-offer "
                "--dry-run --json` or `git status` for the full list)"
                % remaining_classes
            )

    if not groups:
        lines.append("Nothing to commit for session %s." % report["session_id"])

    for g in groups:
        preview = g["paths"][:_REPORT_PATH_PREVIEW_COUNT]
        lines.extend("  %s" % p for p in preview)
        remaining = len(g["paths"]) - len(preview)
        if remaining > 0:
            lines.append("  ... and %d more file(s)" % remaining)

        if g["committed"]:
            sha = (g["sha"] or "?")[:12]
            push = g.get("push_state") or "?"
            scope_count = len(g["paths"])
            changed = _commit_changed_count(g.get("sha"), worktree_root)
            if changed is None:
                tail = "%d file(s) in scope (changed-file count unavailable)" % scope_count
            else:
                tail = "%d file(s) changed (%d in scope)" % (changed, scope_count)
            lines.append(
                "committed %s (%s) — %s — %s" % (sha, push, g["message"], tail)
            )
        elif g["commit_failed"]:
            detail = g.get("error") or "commit failed"
            lines.append("NOT committed — %s — %s" % (g["message"], detail))
        else:
            # The benign no-op. "NOT committed" is reserved for a genuine
            # failure and must never appear here: an operator (or an automated
            # caller) reading it over paths a previous invocation already
            # landed retries or hand-commits a duplicate — the live incident.
            # The count here is pathspec breadth and is labelled as such: no
            # commit happened on this call, so no change count may be attached
            # to it (example-cockpit-repo-em memo, 2026-08-05).
            lines.append(
                "already committed — %s — %d file(s) in scope, nothing new to commit (%s)"
                % (g["message"], len(g["paths"]), g.get("reason") or "no-op")
            )

    return "\n".join(lines)


def main(argv: List[str]) -> int:
    """CLI: ``safe-commit-offer [--session <id>] [--root <path>] [--json]
    [--message <subject>] [--groups-json <file>] [--dry-run]``.

    No confirmation step of any kind — this is the mutating auto-commit
    entrypoint, meant to be called from an unattended trigger (a SessionEnd
    hook) as well as an EM ceremony. ``--dry-run`` computes and prints
    without committing, for inspection/testing only — never gate a real
    invocation behind it.

    ``--message <subject>`` — single group, all of ``safe_paths``, under the
    given subject (a caller that already decided on ONE description).
    ``--groups-json <file>`` — a JSON list of ``{"paths": [...], "message":
    "..."}`` objects for a caller (e.g. an EM ceremony) with real per-group
    judgment. Neither given — the mechanical `_default_groups` fallback.

    Exit codes: 0 — ran (an empty result is itself a valid "nothing to
    commit" outcome, INCLUDING the benign already-committed no-op — that
    shape must never be conflated with 4, see the module's wolf-crying
    constraint). 1 — session id unresolvable. 2 — usage error. 4 — one or
    more groups genuinely failed to commit (``report["failed_groups"]``
    non-empty). The SessionEnd hook (`sessionend-auto-commit.py`) already
    logs a diagnostic for any exit code outside ``{0, 1}``, so 4 surfaces
    there with no change needed on that side (Review: code-reviewer,
    Finding 1).
    """
    explicit_session: Optional[str] = None
    explicit_root: Optional[str] = None
    as_json = False
    dry_run = False
    message: Optional[str] = None
    groups_json_path: Optional[str] = None

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--session" and i + 1 < len(argv):
            explicit_session = argv[i + 1]
            i += 2
        elif tok == "--root" and i + 1 < len(argv):
            explicit_root = argv[i + 1]
            i += 2
        elif tok == "--message" and i + 1 < len(argv):
            message = argv[i + 1]
            i += 2
        elif tok == "--groups-json" and i + 1 < len(argv):
            groups_json_path = argv[i + 1]
            i += 2
        elif tok == "--json":
            as_json = True
            i += 1
        elif tok == "--dry-run":
            dry_run = True
            i += 1
        elif tok in ("-h", "--help"):
            print(
                "usage: safe-commit-offer [--session <id>] [--root <path>] [--json]\n"
                "                          [--message <subject>] [--groups-json <file>]\n"
                "                          [--dry-run]",
                file=sys.stderr,
            )
            return 2
        else:
            print("safe-commit-offer: unrecognized argument %r" % tok, file=sys.stderr)
            return 2

    session_id = explicit_session or core.resolve_session_id(explicit_root)
    if not session_id:
        print(
            "safe-commit-offer: could not resolve a session id unambiguously "
            "(pass --session <id> explicitly).",
            file=sys.stderr,
        )
        return 1

    if message is not None and groups_json_path is not None:
        print(
            "safe-commit-offer: --message and --groups-json are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    if dry_run:
        offer = compute_offer(session_id, explicit_root)
        if as_json:
            print(json.dumps(offer, indent=2))
        else:
            print(
                "DRY RUN — safe_paths: %d, excluded: %d"
                % (len(offer["safe_paths"]), len(offer["excluded"]))
            )
            for p in offer["safe_paths"]:
                print("  %s" % p)
        return 0

    groups: Optional[List[CommitGroup]] = None
    if message is not None:
        offer = compute_offer(session_id, explicit_root)
        groups = [{"paths": offer["safe_paths"], "message": message}] if offer["safe_paths"] else []
    elif groups_json_path is not None:
        try:
            with open(groups_json_path, "r", encoding="utf-8") as fh:
                groups = json.load(fh)
        except (OSError, ValueError) as exc:
            print("safe-commit-offer: cannot read --groups-json: %s" % exc, file=sys.stderr)
            return 2

    report = auto_commit_session(session_id, explicit_root, groups)
    worktree_root = core.git_root(explicit_root) or explicit_root or "."

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(_render_report(report, worktree_root))

    _log_excluded_diagnostic(worktree_root, session_id, report["excluded"])
    _log_dropped_groups_diagnostic(worktree_root, session_id, report["dropped_groups"])

    failed_groups = report["failed_groups"]
    if failed_groups:
        _log_failed_groups_diagnostic(worktree_root, session_id, failed_groups)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
