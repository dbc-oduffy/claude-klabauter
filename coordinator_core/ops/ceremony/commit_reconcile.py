
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.ops.ceremony import git_native

_DIVERGENCE_CHECK_TIMEOUT_SECS = 5.0

#: ~20,067-commit history). `git rev-list --max-count=<N>`, UNFILTERED, is
_RECONCILE_FALLBACK_WINDOW_COMMITS = 200


@dataclass(frozen=True)
class ReconcileProbe:

    sha: Optional[str] = None
    decline: str = ""
    range_spec: str = ""


def _reconcile_landed_despite_failure(
    root: Path,
    token_trailer: str,
    pre_sha: Optional[str],
) -> ReconcileProbe:
    """The sha this call's own commit landed under DESPITE `commit_scoped()`
    reporting failure, as a `ReconcileProbe` whose `sha` is `None` -- with a
    `decline` tag naming why -- when nothing of ours is found.

    Exists because a reported failure is not proof no commit was created, and
    on this machine the common case is not a crash but a CLOCK. `git_native.
    _git` synthesizes `GitResult(returncode=-1)` for `subprocess.
    TimeoutExpired`, so a `git commit` that merely ran LONG -- entirely
    ordinary at this repo's stated load norm of 50-70 concurrent LLM
    sessions, with a Python pre-commit hook in the path -- returns
    `result.ok == False` while git itself goes on to create the commit. The
    timeout kills the wrapper, never the work: project CLAUDE.md § Load norm
    states it outright ("A timeout here is a slow op, not a hung one -- and
    it does NOT stop the engine, so reconcile before retrying"). This
    function is that reconcile, performed once at the seam instead of left
    to every operator.

    Downstream damage when it is skipped: `landed=False` reaches
    `scoped_git_commit`'s `committed` predicate as False, which falls through
    to `_classify_uncommitted`, which probes `git status`, finds the tree
    clean BECAUSE the commit landed, and reports the benign
    `reason="empty-commit-set"`. The operator is told "no commit landed"
    about a commit that exists -- and the natural next move, re-running, is
    how a duplicate commit or a swept peer file happens on a shared branch.
    Live incident: peer session 1021e7bf, 26ce6a671 (2026-08-19), reported
    against a tree that already carried the earlier W3/W3b predicate fixes --
    those widened what counts as landed, but could not help a
    `CommitOutcome` that says `landed=False` in the first place, which is why
    the repair belongs HERE and not one layer up.

    SAFETY -- why this cannot adopt a peer's commit on a shared branch. The
    search key is this call's own `Commit-Token:` trailer, whose match
    `_FULL_SHA_RE`'s own docstring already establishes as collision-free by
    construction: no peer can author this exact token string. That is the
    same key, over the same `pre_sha..HEAD` range, that the SUCCESS path one
    screen down already uses to name its sha -- deliberately reused rather
    than re-derived, so both paths agree on what "this call's commit" means.
    A bare `rev-parse HEAD` fallback is NOT used and must never be added here:
    HEAD moves under concurrent peers, and adopting whatever sits there is
    precisely the misattribution the token search exists to prevent.

    Returns a probe with `sha=None` -- leaving the caller's failure return
    untouched -- on every uncertain shape: a failed `git log`, or a
    zero/ambiguous candidate count. Never raises; a reconcile that cannot
    answer must degrade to today's behaviour, since wrongly claiming a commit
    landed is worse than the reporting defect it repairs. It no longer
    declines merely for want of `pre_sha`, which is the one decline the
    2026-08-19 investigation could not rule out and the one that fires
    precisely under the load that produces the defect -- a missing `pre_sha`
    means the pre-commit `git rev-parse HEAD` itself timed out, not that
    there is no history. That case is the FALLBACK path below; the token
    bound is what makes either path's answer safe, the range only ever makes
    it cheaper.

    Two shapes, two costs:

      `pre_sha` present -- exactly ONE `git log --grep=<token> --fixed-
      strings <pre_sha>..HEAD` call, a real revision range and
      therefore a true walk bound (unlike a filtered `-n`/`--max-count`,
      which bounds OUTPUT, never the WALK -- see `_RECONCILE_FALLBACK_
      WINDOW_COMMITS`'s own comment for the measurement). A miss here is
      `"no-candidate"`, full stop -- there used to be a second, WIDENED pass
      here (`-n <N> HEAD`, no lower bound) for a shape observed live
      2026-08-19: this call's own commit sitting OUTSIDE its own `pre_sha..
      HEAD` range, because `pre_sha` named a PEER commit six seconds newer
      than the one this call had just landed. That observation was real, but
      its cause was never an ordering fault in `commit()` -- `rev_parse_
      head()` genuinely does run before `commit_scoped()`, in that order,
      every time. The cause was the warm-engine client re-executing an
      already-delivered mutation: a SECOND execution of this same call read
      `pre_sha` AFTER a FIRST execution had already committed, so the
      "peer" commit ahead of `pre_sha` was this call's own prior execution.
      That root cause is fixed at the client (`coordinator_core/warm/
      client.py`, this session) -- with one execution per invocation,
      `pre_sha` is an ancestor of this call's own commit by construction,
      and the widened pass was defending against a shape that can no longer
      occur, at the cost of an unbounded-by-filter `git log` on the
      COMMONEST failure-path outcome there is (the ordinary already-
      committed no-op). Removed, not merely disabled -- this is retiring a
      workaround whose defect was fixed at the root, not stripping
      defensive depth.

      `pre_sha` absent (a timed-out pre-commit `git rev-parse HEAD`, not an
      empty history) -- TWO spawns, because `-n`/`--grep` cannot supply its
      own bound here (no `pre_sha` to build a real range from). First,
      `git rev-list --max-count=<N+1> HEAD`, UNFILTERED, to resolve a real
      base commit -- `--max-count` on an unfiltered `rev-list` genuinely
      bounds the walk, unlike the `git log -n --grep` shape above. Its last
      line becomes an EXCLUSIVE lower bound (`<base>..HEAD` spans exactly N
      commits), and the token search runs over that real range. When the
      base cannot be resolved (history shorter than N, an unborn branch, or
      the `rev-list` call itself failing) this does NOT refuse -- it falls
      back to searching `HEAD` with no lower bound at all, through the same
      decline-safely `_search`/`_resolve` machinery as every other case:
      the token is what makes the match safe, not the range, so an
      unbounded range here still cannot adopt a peer's commit. Two spawns
      are acceptable on this path because it is rare by construction (it
      only fires when the pre-commit HEAD read itself timed out).

    The fallback path's wider, filter-only-bounded search keeps the ANCHORED
    trailer match (`^<token_trailer>$`, `--extended-regexp`) rather than the
    bounded path's plain `--fixed-strings` substring match: a commit whose
    message merely QUOTES a token in prose (this defect's own investigation
    notes do, repeatedly) must not be adopted as a match once the search is
    no longer confined to a tight, freshly-opened range."""
    def _search(pattern: str, range_args: Sequence[str], *, literal: bool):
        extra_args = [
            "--fixed-strings" if literal else "--extended-regexp",
            "--format=%H",
            *range_args,
        ]
        try:
            match_result = git_native.log_grep(root, pattern, extra_args=extra_args)
        except Exception:
            return "raised", []
        if not match_result.ok:
            return "failed", []
        return "ok", [line for line in match_result.stdout.splitlines() if line]

    def _resolve(status, candidates, range_spec):
        if status == "raised":
            return ReconcileProbe(decline="log-grep-raised", range_spec=range_spec)
        if status == "failed":
            return ReconcileProbe(decline="log-grep-failed", range_spec=range_spec)
        if len(candidates) > 1:
            return ReconcileProbe(
                decline=f"ambiguous-candidates:{len(candidates)}", range_spec=range_spec
            )
        if candidates:
            return ReconcileProbe(sha=candidates[0], range_spec=range_spec)
        return None

    if pre_sha:
        # `_RECONCILE_FALLBACK_WINDOW_COMMITS`'s own comment for why a
        bounded_spec = f"{pre_sha}..HEAD"
        status, candidates = _search(token_trailer, [bounded_spec], literal=True)
        probe = _resolve(status, candidates, bounded_spec)
        if probe is not None:
            return probe
        return ReconcileProbe(decline="no-candidate", range_spec=bounded_spec)

    # FALLBACK: no `pre_sha` to build a real range from (the pre-commit
    # FILTERED `git log --grep` call does not bound the walk (see
    # `_RECONCILE_FALLBACK_WINDOW_COMMITS`'s own comment) -- so a real range
    # is resolved first via an UNFILTERED `git rev-list --max-count`, where
    rev_list_result = git_native._git(
        ["rev-list", f"--max-count={_RECONCILE_FALLBACK_WINDOW_COMMITS + 1}", "HEAD"],
        cwd=root,
        timeout=_DIVERGENCE_CHECK_TIMEOUT_SECS,
    )
    base_lines = (
        [line for line in rev_list_result.stdout.splitlines() if line]
        if rev_list_result.ok
        else []
    )
    if len(base_lines) > _RECONCILE_FALLBACK_WINDOW_COMMITS:
        # The (N+1)th-oldest line is an EXCLUSIVE lower bound -- `base..HEAD`
        # then spans exactly `_RECONCILE_FALLBACK_WINDOW_COMMITS` commits,
        window_spec = f"{base_lines[-1]}..HEAD"
    else:
        # range rather than refusing outright: the ANCHORED token match
        window_spec = "HEAD"

    # ANCHORED trailer match: the token must be the whole line, exactly as
    status, candidates = _search(f"^{token_trailer}$", [window_spec], literal=False)
    probe = _resolve(status, candidates, window_spec)
    if probe is not None:
        return probe
    return ReconcileProbe(decline="no-candidate", range_spec=window_spec)
