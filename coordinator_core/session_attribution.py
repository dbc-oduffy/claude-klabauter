"""
coordinator_core.session_attribution

One home for the SHA -> Session-Id attribution classification previously
duplicated across coordinator_core.coverage and
coordinator_core.ops.ceremony.wsc_resolve.

Two signals, composed:
  1. Trailer signal — a commit's own `Session-Id:` git trailer, when present,
     is authoritative: it names the session that authored the commit.
  2. Touched-path scope-membership signal — for a TRAILERLESS commit, whether
     every path it touches falls outside a caller-supplied known-scope path
     set. Used only as a fallback when signal 1 is silent.

Three call shapes are exposed:
  - `trailer_foreign_shas` — the trailer-only fast path, ported verbatim from
    coverage.py's former `_foreign_session_shas`. Used by coverage.py's
    scope="session" attribution.
  - `detect_foreign_commits` + `range_is_contiguous_suffix` — the full
    two-signal classifier (trailer match, then touched-path fallback, then
    contiguity), ported verbatim from wsc_resolve.py's former
    `_detect_foreign_commits` / `_range_is_contiguous_suffix`. Used by
    wsc_resolve.py's session-scoping analysis, and by downstream chunks
    (C2/C3/C7) of docs/plans/2026-07-27-review-trail-scope-guard.md.
  - `bulk_trailer_session_map` — a whole-window sibling of
    `trailer_foreign_shas` (same `git log --no-merges` trailer-scan shape, one
    walk over an entire range rather than per sub-range), added by C7 so a
    caller with MANY (sub-range, own_session_id) pairs drawn from the same
    window can precompute the trailer answer once and derive each pair's
    foreign-set by in-memory set math. Used by coverage.py's
    `_reviewed_via_graph_walk` to prime `_narrow_foreign_session_scope`'s
    `session_cache`.

Failure posture is deliberately NOT unified here: this module raises
`GitLogFailed` verbatim on a git subprocess failure in the trailer path, and
returns a graceful-empty `[]` on a git failure in the two-signal path — each
mirrors what its pre-extraction caller already relied on. Each caller wraps
the raw result in its OWN existing posture (coverage.py: fail-closed via its
own `_ForeignSessionLookupError`; wsc_resolve.py: fail-empty, "could not
determine"). This module does not decide which posture is correct for a
caller — see each function's docstring for the git-failure contract it
promises.

Explicitly OUT of scope for this module (verified, not absorbed):
  - coordinator_core/archive_stamp.py's `_commit_session_id` /
    `_SESSION_ID_UUID_RE` — an explicit, dated in-code decision against this
    exact coupling. Untouched.
  - coordinator_core/ops/ceremony/wsc_commit.py's `_derive_session_sha_range`
    — the INVERSE mapping (session-id -> SHAs via `--grep`), not a duplicate
    of the SHA -> session-id classification this module performs.
  - coordinator_core/coverage.py's `_UUID_RE` — gates `_derive_dag_chain_set`
    only; this module's touched-path classifier does no `--grep`
    interpolation, so there is nothing here for it to gate.

Spec backlink: pln-review-trail-scope-guard-refus-d6e42c § C1.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from coordinator_core.win_portability import no_console_creationflags

log = logging.getLogger(__name__)

#: Signature of a "never raises, returns (returncode, stdout, stderr)" git
#: runner — the contract coverage.py's own `_run` helper makes, and the one
#: `trailer_foreign_shas` requires from its caller (dependency-injected
#: rather than owned here, so coverage.py's existing test-time monkeypatch of
#: its own `_run` keeps working unchanged after extraction).
GitRunner = Callable[[List[str], Optional[str]], Tuple[int, str, str]]


def default_git_runner(args: List[str], cwd: Optional[str]) -> Tuple[int, str, str]:
    """Never-raises git-invocation helper conforming to `GitRunner`.

    Moved verbatim (C2b, state/dispatch-briefs/2026-08-29-the-gravestoned-
    review-trail-surface-is-deleted/C2b.md) from
    `coordinator_core.ops.review_trail_write._git_runner` — that module is
    gravestoned per DR-374, but this module already DEFINES the `GitRunner`
    type this helper conforms to, so its true home was here, not a new
    single-function module.

    ``args`` already includes the leading ``"git"`` token (the contract this
    module's own callers invoke their injected ``run`` with) — this helper
    does not prepend it again.

    Windows-safe: suppresses the console window a bare subprocess.run would
    otherwise flash on Windows (CREATE_NO_WINDOW) AND pins stdin=DEVNULL —
    CREATE_NO_WINDOW alone hangs on Windows when stdin is inherited/invalid
    (see coverage.py._run's pairing), matching this module's other subprocess
    call sites.
    """
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            # stdin=DEVNULL paired with CREATE_NO_WINDOW, matching this
            # module's other subprocess call sites (`_git_run`) — CREATE_NO_WINDOW
            # alone hangs on Windows when stdin is inherited/invalid; this is a
            # LIVE git-invocation path, so it must not be left half-fixed.
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


class GitLogFailed(RuntimeError):
    """Raised by `trailer_foreign_shas` when its backing `git log` subprocess
    fails (non-zero returncode).

    Deliberately NOT swallowed to an empty result — an empty result reads to
    every caller as "no foreign commits found", which for a scope="session"
    record's `shas - foreign` computation means FULL-WIDTH crediting, exactly
    the over-crediting bug this classifier exists to close. Callers decide
    their own posture on catching this (see module docstring); coverage.py
    re-raises it as its own `_ForeignSessionLookupError` to preserve its
    pre-extraction fail-closed contract unchanged.
    """


def trailer_foreign_shas(
    sha_range: str,
    own_session_id: Optional[str],
    cwd: str,
    cache: Dict[Tuple[str, Optional[str]], FrozenSet[str]],
    run: GitRunner,
) -> FrozenSet[str]:
    """Within `sha_range`, return the commits whose OWN Session-Id git trailer is
    set and names a DIFFERENT session than `own_session_id` — i.e. commits
    provably NOT authored under the reviewing session.

    Deliberately exclusion-based, not inclusion-based: a commit with NO
    Session-Id trailer at all (untrailered authoring, or synthetic/legacy
    history) is left credited exactly as before — only a commit AFFIRMATIVELY
    attributed to a different session is stripped out.

    Cached per (sha_range, own_session_id) in the caller-supplied `cache` —
    many trail records commonly share a range or session_id.

    `run` is the caller's own "never raises, returns (rc, stdout, stderr)"
    git-invocation helper (coverage.py passes its own `_run`) — injected
    rather than owned by this module so the caller's existing subprocess
    conventions (Windows CREATE_NO_WINDOW, stdin=DEVNULL, etc.) and its
    existing test-time monkeypatch hook keep working unchanged.

    Raises `GitLogFailed` if the backing `git log` subprocess fails (non-zero
    returncode). Not cached on failure — a transient failure should not pin a
    wrong answer for the rest of the cache's lifetime.
    """
    key = (sha_range, own_session_id)
    if key in cache:
        return cache[key]
    rc, out, err = run(
        [
            "git", "log", "--no-merges",
            "--format=%H%x1f%(trailers:key=Session-Id,valueonly)",
            sha_range,
        ],
        cwd,
    )
    if rc != 0:
        raise GitLogFailed(
            f"git log failed while resolving foreign-session commits for "
            f"sha_range={sha_range!r}: {err.strip() or 'unknown error'}"
        )
    foreign: Set[str] = set()
    for line in out.splitlines():
        if "\x1f" not in line:
            continue
        sha, trailer = line.split("\x1f", 1)
        sha = sha.strip()
        trailer = trailer.strip()
        if sha and trailer and trailer != own_session_id:
            foreign.add(sha)
    result_set: FrozenSet[str] = frozenset(foreign)
    cache[key] = result_set
    return result_set


def bulk_trailer_session_map(
    range_str: str,
    cwd: str,
    run: GitRunner,
    include_merges: bool = False,
) -> Dict[str, str]:
    """Return {sha: session_id} for every commit within `range_str` that carries
    its own Session-Id git trailer, via ONE `git log` walk.

    Sibling of `trailer_foreign_shas`, not a replacement — same git invocation
    shape (same trailers format string), but over a WHOLE window rather than
    one caller-supplied sub-range, so a caller that needs the foreign-set
    answer for MANY (sub-range, own_session_id) pairs drawn from the same
    window can compute each answer by cheap in-memory set math afterward
    instead of one `git log` per pair. See coverage.py's
    `_reviewed_via_graph_walk`, which primes `_narrow_foreign_session_scope`'s
    own `session_cache` from this map rather than calling `trailer_foreign_shas`
    once per distinct sha_range — the equivalence holds because every sha that
    can ever reach that function's `shas - foreign` computation is already
    known to lie within this same window (see that call site's own comment for
    the argument).

    `include_merges` defaults to `False`, which walks `--no-merges` — BYTE-
    IDENTICAL to this function's pre-existing behaviour, preserving every
    existing caller (coverage.py's `_reviewed_via_graph_walk`,
    `review_trail_readjudication_report.py`) unchanged. Pass `True` only when
    a merge commit's own Session-Id trailer must also be visible — e.g.
    `directives_commit_tail._committed_paths_for_sids`, which needs a peer's
    merge commits attributed (docs/plans/2026-08-10-commit-event-5s-cap-and-
    the-silent-tail.md, AC2 — the exact defect that reverted C18 of
    docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-
    gate.md: dropping merges here silently UNDER-excludes a live peer's
    touched paths).

    A commit with no Session-Id trailer is simply absent from the returned map
    — same exclusion-based posture as `trailer_foreign_shas` (untrailered
    commits are never treated as foreign).

    Raises `GitLogFailed` on a non-zero `git log` returncode — same fail-closed
    contract as `trailer_foreign_shas`; not swallowed to an empty map, since an
    empty map reads as "nothing has a trailer," which would make every
    (sub-range, own_session_id) pair derived from it compute an empty foreign
    set — exactly the over-crediting defect the trailer classifier exists to
    close.
    """
    args = ["git", "log"]
    if not include_merges:
        args.append("--no-merges")
    args += [
        "--format=%H%x1f%(trailers:key=Session-Id,valueonly)",
        range_str,
    ]
    rc, out, err = run(args, cwd)
    if rc != 0:
        raise GitLogFailed(
            f"git log failed while bulk-resolving Session-Id trailers for "
            f"range={range_str!r}: {err.strip() or 'unknown error'}"
        )
    result: Dict[str, str] = {}
    for line in out.splitlines():
        if "\x1f" not in line:
            continue
        sha, trailer = line.split("\x1f", 1)
        sha = sha.strip()
        trailer = trailer.strip()
        if sha and trailer:
            result[sha] = trailer
    return result


def _git_run(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git subcommand, suppressing exceptions; return the CompletedProcess.

    Ported verbatim from wsc_resolve.py's own `_git_run` — scoped to this
    module's two-signal classifier so it carries no dependency on
    wsc_resolve.py's broader helper surface.
    """
    try:
        return subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,  # nt: inherited invalid stdin + CREATE_NO_WINDOW hangs _execute_child
            **no_console_creationflags(),
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # Review: code-reviewer — Finding 1: dropped during the "ported
        # verbatim" extraction from wsc_resolve.py's own `_git_run`, which
        # logs this exact spawn failure. This is precisely the class of
        # failure detect_foreign_commits/range_is_contiguous_suffix fail-empty
        # on ("could not determine"); losing the log line made that outcome
        # silent where it used to be observable.
        log.warning("session_attribution: git %s failed: %s", args[0] if args else "?", exc)
        proc = subprocess.CompletedProcess(args=["git"] + args, returncode=2)
        proc.stdout = ""
        proc.stderr = str(exc)
        return proc


def _range_log(
    worktree_root: Path,
    candidate_range: str,
    log_format: str,
    *,
    reverse: bool = False,
) -> subprocess.CompletedProcess:
    """Run `git log --format=<log_format> <candidate_range>`, root-commit-safe.

    candidate_range is produced by wsc_resolve's `_started_at_candidate_range`
    in the form "<first_sha>^..HEAD". When first_sha is the repo's root
    commit (no parent), "<first_sha>^" is an invalid revision and git fails
    the whole range with a non-zero exit — every consumer of candidate_range
    would otherwise silently degrade to graceful-empty on any session whose
    very first tracked commit predates started_at. On that specific failure
    this retries with the plain two-dot form "<first_sha>..HEAD" plus
    first_sha's own entry re-prepended (oldest-first when reverse=True) or
    re-appended (newest-first when reverse=False), since a two-dot range
    excludes its left endpoint — first_sha itself must be re-included when it
    has no parent to exclude it via.
    """
    args = ["log", f"--format={log_format}"]
    if reverse:
        args.append("--reverse")
    result = _git_run(args + [candidate_range], cwd=worktree_root)
    if result.returncode == 0:
        return result

    first_sha, sep, _rest = candidate_range.partition("^..")
    if not sep:
        return result

    root_check = _git_run(["rev-parse", "--verify", "-q", f"{first_sha}^"], cwd=worktree_root)
    if root_check.returncode == 0:
        # Not a root-commit issue — some other git failure; propagate as-is.
        return result

    tail_args = ["log", f"--format={log_format}"]
    if reverse:
        tail_args.append("--reverse")
    tail = _git_run(tail_args + [f"{first_sha}..HEAD"], cwd=worktree_root)
    if tail.returncode != 0:
        return tail
    root_entry = _git_run(
        ["log", "-1", f"--format={log_format}", first_sha], cwd=worktree_root
    )
    if root_entry.returncode != 0:
        return tail
    stdout = (
        root_entry.stdout + tail.stdout if reverse else tail.stdout + root_entry.stdout
    )
    combined = subprocess.CompletedProcess(
        args=tail.args, returncode=0, stdout=stdout, stderr="",
    )
    return combined


def detect_foreign_commits(
    worktree_root: Path,
    sid: str,
    candidate_range: str,
    known_scope_paths: FrozenSet[str],
) -> List[str]:
    """Return the SHAs within candidate_range NOT attributable to sid.

    Two detection rules, applied per-commit over candidate_range:
      1. Commit carries a ``Session-Id:`` trailer for a DIFFERENT sid => foreign.
      2. Commit carries NO ``Session-Id:`` trailer (trailerless) => foreign IFF
         every path it touches falls OUTSIDE known_scope_paths (the union of
         consumed-handoff scope, plan-added paths, and the caller's own
         existing touched_paths — supplied by the caller). A trailerless
         commit that touches at least one known-scope path is treated as
         attributable (not foreign) — the whole point of the contiguous-range
         fallback is to recover trailerless-but-legible sessions.

    Returns [] when candidate_range is empty/invalid, or on any git failure
    (graceful-empty — caller treats absence of a foreign_count signal as
    "could not determine", not "zero foreign commits").
    """
    if not candidate_range:
        return []

    log_result = _range_log(worktree_root, candidate_range, "%H%x01%B%x02")
    if log_result.returncode != 0:
        return []

    trailer_re = re.compile(r"^Session-Id:\s*(\S+)\s*$", re.MULTILINE)
    foreign_shas: List[str] = []
    trailerless_shas: List[str] = []
    for entry in log_result.stdout.split("\x02"):
        entry = entry.strip("\n")
        if not entry or "\x01" not in entry:
            continue
        sha, _sep, body = entry.partition("\x01")
        sha = sha.strip()
        if not sha:
            continue

        m = trailer_re.search(body)
        if m:
            if m.group(1) != sid:
                foreign_shas.append(sha)
            continue

        # Trailerless — defer to a single batched touched-path lookup below,
        # rather than one `git show` spawn per commit.
        trailerless_shas.append(sha)

    if trailerless_shas:
        touched_by_sha = _batch_touched_paths(worktree_root, trailerless_shas)
        for sha in trailerless_shas:
            touched = touched_by_sha.get(sha)
            # Review: code-reviewer — Finding 1 (P2, fix-on-principle): an empty
            # touched-set (a merge commit's `--name-only` diff is empty, or a
            # genuinely empty commit) must be treated as "cannot place in scope"
            # (foreign), matching the conservative posture already taken for an
            # unreadable/unresolvable commit — NOT as "safe by default".
            # `_range_log`'s backing `git log` deliberately still walks merges
            # here (no `--no-merges`) so they reach this classifier at all; a
            # trailerless merge with nothing to check against scope must not be
            # silently waved through just because there was nothing to examine.
            if touched is None:
                # Cannot determine touched paths (batch call failed, or this
                # sha never resolved in the batch output) — conservatively
                # treat as foreign rather than silently attributing an
                # unreadable commit.
                foreign_shas.append(sha)
                continue
            if not touched or not any(p in known_scope_paths for p in touched):
                foreign_shas.append(sha)

    return foreign_shas


def _batch_touched_paths(worktree_root: Path, shas: List[str]) -> Dict[str, List[str]]:
    """One `git log --no-walk` spawn resolving touched paths for every sha in
    `shas`, replacing a `git show --name-only` call per sha.

    `--no-walk` preserves the given commit ORDER verbatim (it does not sort by
    date/topology), so each `\x02`-delimited output segment corresponds
    positionally to `shas[i]` — required for correct per-commit attribution.
    Returns {} for every sha (via a missing key) on a whole-batch git failure,
    mirroring the per-item `returncode != 0` fail-closed path this replaces.
    """
    if not shas:
        return {}
    # Leading delimiter (not trailing): a trailing "%H\x02" splits the NEXT
    # commit's hash into the CURRENT commit's segment (the blank separator
    # line + --name-only paths land between one commit's \x02 and the next
    # commit's hash), misattributing every touched-path list by one commit.
    # A leading "\x02%H" makes each split segment self-contained: sha first,
    # then its own paths, with nothing bleeding across the boundary.
    result = _git_run(
        ["log", "--no-walk", "--name-only", "--format=\x02%H", *shas],
        cwd=worktree_root,
    )
    if result.returncode != 0:
        return {}
    out_by_sha: Dict[str, List[str]] = {}
    for segment in result.stdout.split("\x02"):
        if not segment.strip():
            continue
        lines = segment.splitlines()
        if not lines:
            continue
        seg_sha = lines[0].strip()
        if not seg_sha:
            continue
        out_by_sha[seg_sha] = [ln.strip() for ln in lines[1:] if ln.strip()]
    return out_by_sha


def range_is_contiguous_suffix(
    worktree_root: Path,
    candidate_range: str,
    foreign_shas: List[str],
) -> bool:
    """Return True iff no foreign commit is interleaved within candidate_range.

    Contiguous means: every foreign SHA in candidate_range, if any, occupies a
    position such that the TRUE session commit set forms an unbroken suffix —
    i.e. no foreign commit sits BETWEEN two session commits or between the
    last session commit and HEAD. Concretely: candidate_range is contiguous
    iff foreign_shas is empty, OR every foreign SHA sits at (or before) the
    OLDEST end of candidate_range with no session commit older than it in
    candidate_range's history-order.

    Implemented via the same --reverse commit ordering `_range_log` uses:
    walk the range oldest-first; the leading run of foreign shas (possibly
    empty) is tolerated, but any foreign sha found AFTER that leading run has
    ended marks interleaving and returns False.

    Returns True (vacuously contiguous) when candidate_range is empty or
    foreign_shas is empty. Returns False on any git failure — an unverifiable
    range is NOT treated as contiguous (ambiguity is unsafe by default).
    """
    if not candidate_range or not foreign_shas:
        return True

    result = _range_log(worktree_root, candidate_range, "%H", reverse=True)
    if result.returncode != 0:
        return False

    ordered_shas = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    foreign_set = set(foreign_shas)

    # The leading run (oldest-first) of foreign commits is tolerated — it sits
    # BEFORE the session's true set, not interleaved within it. Any foreign
    # sha found after that leading run has ended marks interleaving.
    leading_run_ended = False
    for sha in ordered_shas:
        is_foreign = sha in foreign_set
        if not leading_run_ended:
            if not is_foreign:
                leading_run_ended = True
            continue
        if is_foreign:
            return False
    return True
