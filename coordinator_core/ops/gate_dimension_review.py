"""
coordinator_core.ops.gate_dimension_review — the "review" dimension for
`gate.validate_invocable` (C5, docs/plans/2026-07-20-merge-gate-dod-engine-
enforced.md § C5).

Purpose: wire the seam's `"review"` slot (see
`coordinator_core.ops.gate_validate_invocable.register_dimension`) to a real
assertion over the review evidence on disk — the changed-file set's
underlying commits must each carry review evidence, or the dimension reports
`uncovered`. This module is a CONSUMER of two credit sources; it builds and
resolves no credit rule itself, and modifies neither primitive:

  1. the reviewed-set store (`coordinator_core.review_trail.reviewed_set` /
     `backfill.py`), fed by `state/review-trail/*.json` at fold time;
  2. the reviewer sidecar receipt
     (`coordinator_core.review_trail.receipt_credit`), fed by the
     `review_receipt:` block the dispatch seam stamps.

Source 2 is not a redundant belt on source 1 — it is the only live one.
Source 1's corpus is FROZEN: `review_trail.write`'s in-process wiring was
removed 2026-08-23 by PM ruling and stays removed (DR-372, DR-374), so the
store credits nothing written since. Measured in this clone 2026-08-28: the
newest covered commit sat 486 commits behind HEAD and none of the last 400
commits were members, i.e. this dimension answered FAIL for every recent
chain regardless of whether review happened. Source 1 is kept because it
still holds real credit for everything folded before the freeze, and reading
it costs one `os.stat`.

Verdict vocabulary, mapped onto the seam's tri-state `DimensionResult`:
    covered      -> Verdict.PASS   — every commit touching changed_files
                                      under `diff_base` carries EITHER a
                                      review-trail stamp (the resident store)
                                      or a reviewer sidecar receipt
                                      (`review_trail.receipt_credit`).
    uncovered    -> Verdict.FAIL   — at least one such commit carries neither.
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

import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.ops.gate_validate_invocable import (
    DimensionResult,
    Verdict,
    register_dimension,
)
from coordinator_core.review_trail.receipt_credit import receipt_credited_shas
from coordinator_core.review_trail.reviewed_set import read_reviewed_set
from coordinator_core.win_portability import no_console_creationflags

_GIT_TIMEOUT_SECS = 60
_CREATIONFLAGS = no_console_creationflags()

#: `\x01` cannot occur in a path (git rejects NUL and this repo's paths are
#: ordinary text), so prefixing the sha in `--pretty=format:` makes the two
#: token classes structurally distinguishable — a 40-hex *path* line never
#: starts with it, so it can no longer be misread as a commit header the way
#: a bare-shape regex match would.
_SHA_HEADER_PREFIX = "\x01"
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Field separator INSIDE the `\x01` commit-header line. The header carries
#: three fields — sha, committer date, `Session-Id` trailer — because the
#: receipt credit source below needs the latter two and this is the `git log`
#: that is already being spawned. `separator=%x20` on the trailers atom is
#: load-bearing: without it git terminates each trailer with a newline, the
#: value lands on its own line, and the path-classification loop below reads
#: it as a touched path. A commit with no trailer yields an empty third field.
_HEADER_FIELD_SEP = "\x1f"
_COMMIT_HEADER_FORMAT = (
    f"{_SHA_HEADER_PREFIX}%H{_HEADER_FIELD_SEP}%cI{_HEADER_FIELD_SEP}"
    "%(trailers:key=Session-Id,valueonly=true,unfold=true,separator=%x20)"
)


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

    # ONE spawn, invariant in len(changed_files).
    #
    # `changed_files` cannot go into argv at all: above ~1400 paths it
    # overflows the Windows cap (WinError 206) and the whole check used to
    # fail open with UNAVAILABLE. Pathspec-batching that argv (the first fix)
    # closed the fail-open but bought a cost linear in the changeset: 2000
    # paths measured 718.75ms across 55 processes, over the DR-344 500ms
    # brightline, and the bulk changesets on this branch run past 26,000
    # files. `git log` has no `--pathspec-from-file`, so the pathspec is
    # taken out of the argument list entirely: ask git once for the range's
    # own commits-and-touched-paths and intersect in process. Building
    # `wanted` is O(len(changed_files)), linear and cheap but real; the
    # git-output scan against it below is the part that's free, O(paths in
    # range) and independent of how many paths the caller asked about.
    #
    # Set membership, not a scan.
    #
    # `-z` because a non-ASCII path is otherwise quoted and would not match
    # the caller's own spelling. Separators are normalised on both sides so a
    # Windows caller passing backslashes still matches git's forward slashes.
    wanted = {p.replace("\\", "/").strip() for p in changed_files if p.strip()}

    # `--diff-merges=first-parent` makes a merge commit report the paths it
    # actually brought in (diffed against its first parent), the same
    # first-parent simplification `git log <base> -- <paths>` applied by
    # default before the pathspec left argv. Without it `--name-only` prints
    # NO path lines for a merge commit at all, so a merge that genuinely
    # touches a reviewed path would silently drop out of commit_shas below
    # and never be required to carry a review-trail stamp.
    rc, out, err = _run_git(
        [
            "log",
            f"--pretty=format:{_COMMIT_HEADER_FORMAT}",
            "--name-only",
            "--diff-merges=first-parent",
            "-z",
            diff_base,
        ],
        cwd=repo_root_str,
    )
    if rc != 0:
        last_err = err.strip().splitlines()[-1] if err.strip() else "git log failed"
        return DimensionResult(
            dimension="review",
            verdict=Verdict.UNAVAILABLE,
            detail=f"could not resolve commits for diff_base={diff_base!r}: {last_err}",
        )

    # Records are NUL-separated; a commit's sha arrives newline-joined to the
    # first of its paths. Classify per line by the `\x01` prefix, not by
    # 40-hex shape: a path that happens to be exactly 40 lowercase hex
    # characters (content-addressed asset, git-lfs pointer, generated hash
    # filename) is otherwise indistinguishable from a sha and would silently
    # re-anchor current_sha onto a bogus value, dropping the real commit.
    commit_sha_set: "set[str]" = set()
    #: sha -> (committer date, Session-Id trailer), harvested from the same
    #: header line. Only consulted if the resident store leaves something
    #: uncovered, but collected unconditionally — it is already parsed.
    commit_provenance: "dict[str, tuple[str, str]]" = {}
    current_sha: Optional[str] = None
    for field in out.split("\0"):
        for line in field.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith(_SHA_HEADER_PREFIX):
                header = line[len(_SHA_HEADER_PREFIX):]
                sha, _, rest = header.partition(_HEADER_FIELD_SEP)
                if _FULL_SHA_RE.match(sha):
                    current_sha = sha
                    committed_at, _, session_id = rest.partition(_HEADER_FIELD_SEP)
                    commit_provenance[sha] = (committed_at.strip(), session_id.strip())
            elif current_sha is not None and line.replace("\\", "/") in wanted:
                commit_sha_set.add(current_sha)

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

    # SECOND CREDIT SOURCE — the reviewer sidecar receipt.
    #
    # The store above is fed only by `state/review-trail/*.json` folded in at
    # write time, and that corpus is frozen: `review_trail.write`'s in-process
    # wiring was removed 2026-08-23 (PM ruling; DR-372, DR-374) and no
    # production call site resolves the op. Measured in this clone
    # 2026-08-28, the store's newest covered commit was 486 commits behind
    # HEAD and none of the last 400 commits were members — so this dimension
    # returned a confident FAIL for every recent chain whether or not review
    # had happened. Reviews now land on the reviewer's own sidecar receipt,
    # which is what `receipt_credited_shas` reads.
    #
    # Ordering matters and is enforced there, not here: a reviewer dispatched
    # at T cannot have read a commit authored after T. Crediting on receipt
    # existence alone would have turned 42% of its credits into false
    # coverage, which is strictly worse than the stale negative it replaces —
    # a FAIL nobody trusts costs one redundant review, a wrong PASS costs the
    # review itself.
    if uncovered:
        credited = receipt_credited_shas(
            repo_root_str,
            ((sha, *commit_provenance.get(sha, ("", ""))) for sha in uncovered),
        )
        if credited:
            uncovered = [sha for sha in uncovered if sha not in credited]

    if uncovered:
        return DimensionResult(
            dimension="review",
            verdict=Verdict.FAIL,
            detail=(
                f"uncovered: {len(uncovered)}/{len(commit_shas)} commit(s) touching "
                f"changed_files carry neither a review-trail stamp nor a reviewer "
                f"sidecar receipt (e.g. {uncovered[0][:12]})"
            ),
        )

    return DimensionResult(
        dimension="review",
        verdict=Verdict.PASS,
        detail=(
            f"covered: all {len(commit_shas)} commit(s) touching changed_files "
            "carry a review-trail stamp or a reviewer sidecar receipt"
        ),
    )


register_dimension("review", _review_dimension_check)
