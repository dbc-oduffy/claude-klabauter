"""
coordinator_core.ops.ceremony.commit_reconcile -- "did our commit land
despite a reported failure" reconciliation, extracted from the dying
`commit_pipeline.py` (C4 of `docs/plans/2026-08-29-the-push-subsystem-leaves-
and-then-the-pipeline-can-go.md`) into its own module -- the same shape C1
gave the push subsystem (`ops/ceremony/push.py`) before that predecessor
module's own delete.

`_reconcile_landed_despite_failure` is a bounded `git log` search that
answers "did the commit land despite the reported failure", not a committer
itself -- it spawns git by construction, which is why it does NOT live in
`commit_v2.py` (the zero-spawn replacement for the op `run_commit_pipeline`
was killed on process cost) and does NOT live in `commit_gates.py` either
(pre-commit gating, not post-hoc reconciliation). Its surviving production
consumer is `coordinator/bin/coordinator-safe-commit.py`.

Spec backlink: docs/plans/2026-08-29-the-push-subsystem-leaves-and-then-the-pipeline-can-go.md § C4.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.ops.ceremony import git_native

#: Timeout for the unfiltered `git rev-list` base-resolution spawn on the
#: no-`pre_sha` fallback path -- mirrors the divergence-check budget this
#: constant was ported from in `commit_pipeline.py` (a short, bounded probe,
#: not a full commit-shaped op).
_DIVERGENCE_CHECK_TIMEOUT_SECS = 5.0

#: Same one-chunk argv bound as the success path's own search reused here --
#: see `git_native._chunk_paths`'s own docstring for the packer itself.
_chunk_paths = git_native._chunk_paths

#: Commits searched backwards from HEAD when `commit()` has no `pre_sha` to
#: bound the range with -- i.e. when the pre-commit `git rev-parse HEAD` itself
#: failed, which at this repo's load norm (CLAUDE.md, 50-70 concurrent LLM
#: sessions) is a timeout, not a broken repo. Declining the reconcile there
#: silences it exactly when the box is loaded enough to need it. The window is
#: a BOUND, not a correctness input: the `Commit-Token:` search key is
#: collision-free by construction (see `_reconcile_landed_despite_failure`'s
#: own SAFETY paragraph), so a match inside the window is ours no matter how
#: wide the window is, and a peer's commit can never match however many of
#: theirs it spans. Sized to cover the peer traffic one slow commit can sit
#: behind on a shared branch, not tuned.
#:
#: `git log -n <N> --grep=... HEAD` does NOT actually bound the walk to this
#: many commits -- `-n`/`--max-count` on `git log` caps the OUTPUT count,
#: never the commit graph WALK, whenever a filter (`--grep` and/or a
#: pathspec) is present (measured on this repo: `git log -n 5 --grep=<no-
#: match> HEAD`, no pathspec at all, took 1.13s and walked the full
#: ~20,067-commit history). `git rev-list --max-count=<N>`, UNFILTERED, is
#: the one form where `--max-count` is a true walk bound (measured 0.6s on
#: the same repo) -- see `_reconcile_landed_despite_failure`'s fallback path,
#: which uses this constant to size that call, not a `git log -n` call.
_RECONCILE_FALLBACK_WINDOW_COMMITS = 200


@dataclass(frozen=True)
class ReconcileProbe:
    """Why `_reconcile_landed_despite_failure` answered as it did.

    Exists because the reconcile's silence is indistinguishable from its
    absence at the operator's end: both render as `committed: false` over a
    commit that exists. A live occurrence (2026-08-19, four instances across
    two sessions in one day) cost a whole session to narrow to "the reconcile
    did not execute" and still could not say WHY, because the function's
    `Optional[str]` return threw away every decline reason on the way out.
    This type is that reason, carried to the response so the NEXT occurrence
    self-diagnoses instead of costing another investigation.

    Fields:
        sha -- the reconciled commit sha, or `None` on every decline.
        decline -- "" when `sha` is set; otherwise a short machine-readable
            tag naming which precondition answered: `"log-grep-raised"`,
            `"log-grep-failed"`, `"no-candidate"` (the search ran and matched
            nothing -- the genuinely-did-not-land shape), or
            `"ambiguous-candidates:<n>"`.
        range_spec -- the revision range that produced this answer
            (`"<pre>..HEAD"` on the `pre_sha`-present path, or `"<base>..HEAD"`
            / `"HEAD"` on the fallback path -- see
            `_reconcile_landed_despite_failure`'s own docstring), so a reader
            can tell which range answered without re-deriving it.
    """

    sha: Optional[str] = None
    decline: str = ""
    range_spec: str = ""


def _reconcile_landed_despite_failure(
    root: Path,
    token_trailer: str,
    pre_sha: Optional[str],
    commit_paths: Sequence[str],
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
    same key, over the same `pre_sha..HEAD` range, with the same
    `--full-history` merge-pruning guard, that the SUCCESS path one screen
    down already uses to name its sha -- deliberately reused rather than
    re-derived, so both paths agree on what "this call's commit" means. A
    bare `rev-parse HEAD` fallback is NOT used and must never be added here:
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
      strings <pre_sha>..HEAD -- <pathspec>` call, a real revision range and
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
    # Same one-chunk argv bound as the success path's own search, and the
    # same reasoning: this call's commit touched every path in `commit_
    # paths`, so it touched every path in any non-empty subset too.
    chunks = _chunk_paths(list(commit_paths)) if commit_paths else []
    pathspec = ["--", *chunks[0]] if (chunks and chunks[0]) else []

    def _search(pattern: str, range_args: Sequence[str], *, literal: bool):
        """One `git log --grep` pass. Returns `(status, candidates)`, status
        being "ok", "raised" or "failed"."""
        extra_args = [
            "--fixed-strings" if literal else "--extended-regexp",
            "--format=%H",
            "--full-history",
            *range_args,
            *pathspec,
        ]
        try:
            match_result = git_native.log_grep(root, pattern, extra_args=extra_args)
        except Exception:
            return "raised", []
        if not match_result.ok:
            return "failed", []
        return "ok", [line for line in match_result.stdout.splitlines() if line]

    def _resolve(status, candidates, range_spec):
        """Maps one pass's outcome onto a probe, or `None` for "matched nothing
        -- the caller may keep looking"."""
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
        # A real revision range is a true walk bound (see
        # `_RECONCILE_FALLBACK_WINDOW_COMMITS`'s own comment for why a
        # filtered `-n`/`--grep` combination is NOT), so this is the whole
        # search on this path -- exactly one `git log`, never a second,
        # wider pass. There used to be one (see this function's own
        # docstring for why: the shape it defended against was this call's
        # own commit landing OUTSIDE its own `pre_sha..HEAD` range, which
        # was never an ordering fault in `commit()` -- it was the warm-
        # engine client re-executing an already-delivered mutation, fixed at
        # the root in `coordinator_core/warm/client.py` this session). With
        # one execution per invocation, `pre_sha` is an ancestor of this
        # call's own commit by construction, so a miss here is a genuine
        # "nothing of ours landed" -- the ordinary failed-commit case.
        bounded_spec = f"{pre_sha}..HEAD"
        status, candidates = _search(token_trailer, [bounded_spec], literal=True)
        probe = _resolve(status, candidates, bounded_spec)
        if probe is not None:
            return probe
        return ReconcileProbe(decline="no-candidate", range_spec=bounded_spec)

    # FALLBACK: no `pre_sha` to build a real range from (the pre-commit
    # `git rev-parse HEAD` itself timed out). `-n`/`--max-count` on a
    # FILTERED `git log --grep` call does not bound the walk (see
    # `_RECONCILE_FALLBACK_WINDOW_COMMITS`'s own comment) -- so a real range
    # is resolved first via an UNFILTERED `git rev-list --max-count`, where
    # `--max-count` genuinely is a walk bound, then the token search runs
    # over that real range exactly like the `pre_sha`-present path above.
    # Two spawns, acceptable here because this path is rare by construction.
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
        # the same window size the old `-n <N>` call named, just as a real
        # range instead of an output cap.
        window_spec = f"{base_lines[-1]}..HEAD"
    else:
        # History shorter than the window, an unborn branch, or the
        # `rev-list` call itself failed -- decline-safely to the unbounded
        # range rather than refusing outright: the ANCHORED token match
        # below is what makes even an unbounded search safe, so there is no
        # correctness reason to refuse just because a bound could not be
        # established.
        window_spec = "HEAD"

    # The wider (or unbounded) range admits one thing a tight range does
    # not: a commit whose message QUOTES a token in prose rather than
    # carrying it as its own trailer (this defect's own investigation notes
    # do, repeatedly) -- so this pass drops `--fixed-strings` for an
    # ANCHORED trailer match: the token must be the whole line, exactly as
    # `commit()` appends it. Strictly tighter matching than the bounded
    # path's plain substring match, not looser.
    status, candidates = _search(f"^{token_trailer}$", [window_spec], literal=False)
    probe = _resolve(status, candidates, window_spec)
    if probe is not None:
        return probe
    return ReconcileProbe(decline="no-candidate", range_spec=window_spec)
