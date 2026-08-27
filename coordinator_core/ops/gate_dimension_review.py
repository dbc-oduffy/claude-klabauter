"""
coordinator_core.ops.gate_dimension_review — the "review" dimension for
`gate.validate_invocable` (C5, docs/plans/2026-07-20-merge-gate-dod-engine-
enforced.md § C5).

Purpose: wire the seam's `"review"` slot (see
`coordinator_core.ops.gate_validate_invocable.register_dimension`) to a real
assertion over `state/review-trail/` — the changed-file set's underlying
commits must each carry a review-trail stamp, or the dimension reports
`uncovered`. This module is a CONSUMER of the reviewed-set store
(`coordinator_core.review_trail.reviewed_set`/`backfill.py`); it does not
build or resolve any review-trail credit rule itself, and it does not
modify either of those primitives.

Verdict vocabulary, mapped onto the seam's tri-state `DimensionResult`:
    covered      -> Verdict.PASS   — every commit touching changed_files
                                      under `diff_base` has a review-trail
                                      stamp.
    uncovered    -> Verdict.FAIL   — at least one such commit has none.
    UNAVAILABLE  -> Verdict.UNAVAILABLE — `diff_base`/`repo_root` absent, the
                                      commit range could not be resolved, or
                                      the review-trail corpus itself could
                                      not be read. Never a silent PASS.

`diff_base` is NEVER defaulted (no `origin/main` fallback baked in here) —
same restraint the plan's C4 entry names for the tests dimension: this
repo's long-lived shared `work/*` branches make a merge-base-with-
`origin/main` default wrong for a local advisory call, and the caller (not
this module) owns range resolution. An absent `diff_base` reports
UNAVAILABLE, never a guess.

Freshness rule (plan's "name the freshness rule against the frozen head sha,
`state/review-trail/diffs/*.head.sha`"): this dimension does not re-derive
freshness itself — it inherits the ALREADY-LANDED freshness guard baked into
`coordinator_core.coverage._record_range_has_stored_head`, applied at FOLD
time (not read time — see below) by
`coordinator_core.review_trail.backfill.resolve_and_fold`. A trail record
whose `sha_range` stores a literal symbolic `HEAD` endpoint (rather than a
concrete SHA) is excluded from the reviewed-set store entirely: such a
record would otherwise silently re-resolve against WHATEVER commit is HEAD
at gate-run time — on a shared branch, every commit landed since the record
was written, none of which any reviewer actually opened (state/improvement-
queue/2026-06-30-review-coverage-gate-false-covered-on-tr.yaml).
`review.freeze_diff` (`review_freeze_diff.py`) is the sanctioned way a
caller pins a concrete anchor instead: it writes the exact HEAD sha at
freeze time to `state/review-trail/diffs/<slice-id>.head.sha` alongside the
frozen `.diff`, so a review-trail record scoped against that freeze cites a
fixed commit, not a moving ref.

Freshness rule, resident-set edition (docs/plans/2026-08-27-the-reviewed-
set-is-a-file-not-a-computation.md): this module no longer resolves any
trail record itself. It reads `coordinator_core.review_trail.reviewed_set
.read_reviewed_set`, a RESIDENT, append-only set of already-credited commit
SHAs, revalidated per call via a single `os.stat` (mtime_ns + size) against
the on-disk store — zero added git spawns, never a subprocess. A resident,
append-only set IS A CACHE, and this cache does not observe DELETION: once
a SHA is folded into the store it stays there for the life of the clone,
even if the trail record(s) that credited it are later removed from disk.
This is deliberately asymmetric — new review-trail records are picked up
the next time they are folded (write-time, or the next `run_backfill`
pass), but a record's removal is never retroactively un-credited. That
asymmetry is safe ONLY because nothing in this repo currently deletes or
rewrites a creditable `state/review-trail/*.json` (or `archive/review-
trail/*.json`) record: `fleet.reap_unintegrated_findings` /
`fleet.reap_integrated_findings` (DR-218) delete `state/review-trail/
findings/*.md` sidecars, never the `*.json` records this set reads.
`test_reap_findings_scope_excludes_json_trail_records` below is the guard
that keeps that true going forward — a future reaper widened to touch the
`*.json` corpus directly would silently defeat this module's monotonicity
assumption, and the guard fails loudly instead.

DR-243 (PM-vouch relaxation) — the mechanism DR-243 created
(`coordinator_core.session.review_trail_vouch`,
`coordinator/bin/review-trail-vouch-cli`, `state/review-trail/pm-vouches/`)
was superseded wholesale 2026-08-08 (`pln-the-gate-stops-asking-to-be-to-
8db50b`) and deleted outright — see `coordinator_core.coverage`'s own
`_narrow_foreign_session_scope` docstring ("the PM-vouch waiver source ...
has been deleted outright"). There is therefore nothing left for this
dimension to special-case: the reviewed-set store folds foreign-session
narrowing in at fold time (`backfill.resolve_and_fold`'s rule 4), and this
module reads the already-folded result — no waiver source to consult here
either.

Spec backlink: docs/plans/2026-07-20-merge-gate-dod-engine-enforced.md § C5
docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C3
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.git.argv_batch import _chunk_paths
from coordinator_core.ops.gate_validate_invocable import (
    DimensionResult,
    Verdict,
    register_dimension,
)
from coordinator_core.review_trail.reviewed_set import read_reviewed_set
from coordinator_core.win_portability import no_console_creationflags

_GIT_TIMEOUT_SECS = 60
_CREATIONFLAGS = no_console_creationflags()


def _run_git(args: List[str], cwd: str) -> "tuple[int, str, str]":
    """Run `git <args>` in `cwd`; never raises — a spawn failure or timeout
    degrades to a non-zero rc + diagnostic stderr, same shape any other
    dimension's git call in this package uses."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        return result.returncode, result.stdout.strip(), result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"command timed out after {_GIT_TIMEOUT_SECS}s: git {' '.join(args)}"
    except OSError as exc:
        print(f"skip: gate_dimension_review._run_git failed: {exc}", file=sys.stderr)
        return 1, "", str(exc)


def _review_dimension_check(
    changed_files: List[str], diff_base: Optional[str], repo_root: Optional[Path]
) -> DimensionResult:
    """The `"review"` dimension's `DimensionCheck` — see module docstring for
    the full verdict/freshness contract."""
    if not diff_base:
        return DimensionResult(
            dimension="review",
            verdict=Verdict.UNAVAILABLE,
            detail=(
                "diff_base not provided; the review dimension requires an "
                "explicit range (never defaulted to origin/main, per the "
                "plan's C4 shared-branch-topology rule)"
            ),
        )
    if repo_root is None:
        return DimensionResult(
            dimension="review",
            verdict=Verdict.UNAVAILABLE,
            detail="repo_root not provided; cannot resolve git history",
        )
    if not changed_files:
        return DimensionResult(
            dimension="review",
            verdict=Verdict.PASS,
            detail="covered: changed_files is empty, nothing to review-stamp",
        )

    repo_root_str = str(repo_root)

    # `changed_files` can exceed the Windows argv cap on a large changeset
    # (~1400+ paths) if splatted into a single `git log` call unbounded;
    # batch through the promoted `_chunk_paths` primitive and union the
    # resulting commit lists rather than raising the budget or truncating
    # the pathspec (either of which would under-report touched commits and
    # fail open in a quieter way).
    commit_sha_set: "set[str]" = set()
    for path_chunk in _chunk_paths(changed_files):
        rc, out, err = _run_git(
            ["log", "--pretty=%H", diff_base, "--", *path_chunk], cwd=repo_root_str
        )
        if rc != 0:
            last_err = err.strip().splitlines()[-1] if err.strip() else "git log failed"
            return DimensionResult(
                dimension="review",
                verdict=Verdict.UNAVAILABLE,
                detail=f"could not resolve commits for diff_base={diff_base!r}: {last_err}",
            )
        commit_sha_set.update(line for line in out.splitlines() if line)

    commit_shas = list(commit_sha_set)
    if not commit_shas:
        return DimensionResult(
            dimension="review",
            verdict=Verdict.PASS,
            detail=f"covered: no commits in diff_base={diff_base!r} touch changed_files",
        )

    # The reviewed-set store (docs/plans/2026-08-27-the-reviewed-set-is-a-
    # file-not-a-computation.md): a resident, append-only set of already-
    # credited commit SHAs, revalidated with a single `os.stat` and never
    # spawning a subprocess. All resolution (verdict filter, HEAD exclusion,
    # kind partitioning, foreign-session narrowing) already happened at
    # fold time (`review_trail.backfill.resolve_and_fold`) — this call is a
    # pure membership read.
    reviewed = read_reviewed_set(repo_root_str)

    uncovered = [sha for sha in commit_shas if sha not in reviewed]
    if uncovered:
        return DimensionResult(
            dimension="review",
            verdict=Verdict.FAIL,
            detail=(
                f"uncovered: {len(uncovered)}/{len(commit_shas)} commit(s) touching "
                f"changed_files have no review-trail stamp (e.g. {uncovered[0][:12]})"
            ),
        )

    return DimensionResult(
        dimension="review",
        verdict=Verdict.PASS,
        detail=(
            f"covered: all {len(commit_shas)} commit(s) touching changed_files "
            "are review-trail stamped"
        ),
    )


register_dimension("review", _review_dimension_check)
