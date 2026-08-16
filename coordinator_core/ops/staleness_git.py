"""
coordinator_core.ops.staleness_git — shared git plumbing for staleness predicates.

Purpose: `_git_root` and the `git log --since` shell-out are hand-copied
across `check_arch_audit_staleness`, `check_weekly_staleness`, and
`check_import_budget_staleness`, each maintaining its own never-raises
discipline; commit `e698f2a04` closed three ALWAYS-0 escapes and a false-FRESH
path in the newest of those copies. This module is the one tested home for
that plumbing so a fourth caller (C3/C6's generator-output staleness
detector) doesn't hand-copy it a third time into the module whose entire job
is catching copies that drift from their source.

`Verdict` is the shared vocabulary — STALE, FRESH, UNSTAMPED, UNDECLARED,
WRITE_TARGET_UNRESOLVED, INDETERMINATE, MUTATES_DECLARED — so callers import
the enum rather than each spelling the strings themselves. UNDECLARED
asserts a resolved repo-artifact write with no declaration;
WRITE_TARGET_UNRESOLVED covers a discovered generator that writes through a
path expression the sweep could not resolve, so whether it emits a repo
artifact is unknown; MUTATES_DECLARED covers a module that declares a
`MUTATES` pathspec — a corpus mutator with a data-dependent output set and
no fixed artifact, so it carries no staleness contract at all.

`commits_touching_since` is the AC9 seam: one comparison function taking
`(repo_root, sources, since_point)`, where `since_point` is EITHER an
ISO-8601 timestamp (the local leg's stamp form) OR a commit-ish (the vendored
leg's `generated_from_sha` form) — both reduce to "commits touching `sources`
after point P" and MUST yield the same verdict for equivalent inputs. Shape
discrimination: a 7-40 char hex token that `git rev-parse --verify` resolves
is treated as a commit-ish; anything else is parsed as a timestamp. An
unresolvable/unparseable `since_point` is INDETERMINATE, never FRESH.

`artifact_path`, when given, excludes from the range any commit that touches
BOTH `sources` and `artifact_path` — that commit is a REGENERATION, not
evidence of drift. Without this exclusion, the fix-and-regenerate-in-one-
commit workflow reads STALE forever, which is the exact workflow this
detector family exists to reward.

Never-raises is this module's contract, not each caller's: every helper
returns a typed result. A `git` failure, an OSError, a non-repo cwd, or an
unusable `since_point` sets `SinceRange.indeterminate=True` with a `detail`
string, and `verdict_from_range` maps that to `Verdict.INDETERMINATE`.
INDETERMINATE is a value, never an exception and never FRESH.

An EMPTY since-range is distinguishable from a FAILED one — that conflation
is the ALWAYS-0 escape class `e698f2a04` closed in the sibling predicate.
`commits=()` with `indeterminate=False` is FRESH; `commits=()` with
`indeterminate=True` is INDETERMINATE.

Negative-spec:
    - Does NOT migrate any of the five existing staleness predicates onto
      this module. That is spine row C4, ruled `wont_do` — they keep their
      own copies for now.
    - Does NOT resolve `check_weekly_staleness._resolve_state_root` (a
      different directory, owned by a live sibling plan) or reach into
      `schema_drift_watch.py`. Resolves the repo root self-contained.
    - Does NOT extract a stamp from any artifact or generator. Stamp
      EXTRACTION stays with each caller (C3/C6); this module only compares
      an already-extracted `since_point` against `sources`.
    - Does NOT modify any file or run any destructive git operation.

Spec backlink: docs/plans/2026-08-13-generator-output-staleness-detector.md § C0
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.git.repo_root import show_toplevel

_COMMIT_ISH_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _is_parseable_timestamp(value: str) -> bool:
    """Whether *value* parses as an ISO-8601 timestamp. `git log --since`
    silently ignores a garbage date string rather than erroring (it matches
    everything), so this module validates shape itself rather than trusting
    git's leniency — an unparseable `since_point` must read INDETERMINATE,
    never fall through to "every commit matches"."""
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
        return True
    except ValueError:
        return False


class Verdict(str, Enum):
    STALE = "STALE"
    FRESH = "FRESH"
    UNSTAMPED = "UNSTAMPED"
    UNDECLARED = "UNDECLARED"
    WRITE_TARGET_UNRESOLVED = "WRITE_TARGET_UNRESOLVED"
    INDETERMINATE = "INDETERMINATE"
    MUTATES_DECLARED = "MUTATES_DECLARED"


def git_root(cwd: Optional[str] = None) -> Optional[Path]:
    """Resolve a git repo root from *cwd* (or the process cwd), or None if
    not inside a repo / git is unusable. Never raises."""
    root = show_toplevel(cwd)
    return Path(root) if root else None


@dataclass(frozen=True)
class SinceRange:
    commits: tuple[str, ...]
    indeterminate: bool
    detail: str


def _is_commit_ish(repo_root: Path, token: str) -> bool:
    if not _COMMIT_ISH_RE.match(token):
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{token}^{{commit}}"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            **no_console_creationflags(),
        )
    except OSError:
        return False
    return result.returncode == 0


def _run_git_log(repo_root: Path, args: list[str]) -> tuple[Optional[list[str]], str]:
    """Returns `(commits, stderr_detail)`. On success `stderr_detail` is
    empty; on failure `commits` is None and `stderr_detail` carries the
    failed command's stderr for the caller's `SinceRange.detail`."""
    try:
        result = subprocess.run(
            ["git", "log", "--format=%H", *args],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            **no_console_creationflags(),
        )
    except OSError:
        detail = str(sys.exc_info()[1])
        print(f"skip: _run_git_log: subprocess.run failed: {detail}", file=sys.stderr)
        return None, detail
    if result.returncode != 0:
        detail = result.stderr.strip()
        print(f"skip: _run_git_log: git log failed: {detail}", file=sys.stderr)
        return None, detail
    stripped = result.stdout.strip()
    return (stripped.splitlines() if stripped else []), ""


def _touches_artifact(repo_root: Path, commit: str, artifact_path: str) -> tuple[Optional[bool], str]:
    """Returns `(touches, stderr_detail)`. On success `stderr_detail` is
    empty; on failure `touches` is None and `stderr_detail` carries the
    failed command's stderr for the caller's `SinceRange.detail`."""
    try:
        result = subprocess.run(
            # --root: without it, a parentless (root) commit diffs against
            # nothing rather than the empty tree, so a root commit that
            # touches artifact_path would silently read as not-touching it
            # and escape this exclusion filter — Review: coordinator:code-reviewer
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit, "--", artifact_path],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            **no_console_creationflags(),
        )
    except OSError:
        detail = str(sys.exc_info()[1])
        print(f"skip: _touches_artifact: subprocess.run failed: {detail}", file=sys.stderr)
        return None, detail
    if result.returncode != 0:
        detail = result.stderr.strip()
        print(f"skip: _touches_artifact: git diff-tree failed for {commit!r}: {detail}", file=sys.stderr)
        return None, detail
    return bool(result.stdout.strip()), ""


def commits_touching_since(
    repo_root: Path,
    sources: Sequence[str],
    since_point: str,
    *,
    artifact_path: Optional[str] = None,
) -> SinceRange:
    """Commits touching *sources* strictly after *since_point*.

    *since_point* is EITHER an ISO-8601 timestamp OR a commit-ish
    (7-40 char hex that `git rev-parse --verify` resolves). Both forms
    reduce to the same comparison and MUST yield the same verdict for
    equivalent inputs. When *artifact_path* is given, any commit that
    touches both *sources* and *artifact_path* is excluded — it is a
    regeneration, not evidence of drift.

    Never raises: a git failure or an unusable *since_point* returns
    `indeterminate=True` with a `detail` string. `commits=()` with
    `indeterminate=False` is a genuinely empty (FRESH) range, distinct from
    a failed/indeterminate query.
    """
    if not sources:
        return SinceRange(commits=(), indeterminate=True, detail="no sources given")

    repo_root = Path(repo_root)
    if not repo_root.is_dir():
        return SinceRange(commits=(), indeterminate=True, detail=f"repo_root not a directory: {repo_root}")

    if not since_point or not isinstance(since_point, str):
        return SinceRange(commits=(), indeterminate=True, detail="since_point missing or not a string")

    if _is_commit_ish(repo_root, since_point):
        range_args = [f"{since_point}..HEAD", "--", *sources]
    elif _is_parseable_timestamp(since_point):
        range_args = [f"--since={since_point}", "--", *sources]
    else:
        return SinceRange(
            commits=(),
            indeterminate=True,
            detail=f"since_point is neither a resolvable commit-ish nor a parseable timestamp: {since_point!r}",
        )

    commits, log_error = _run_git_log(repo_root, range_args)
    if commits is None:
        detail = f"git log query failed for since_point={since_point!r}"
        if log_error:
            detail = f"{detail}: {log_error}"
        return SinceRange(commits=(), indeterminate=True, detail=detail)

    if artifact_path:
        filtered: list[str] = []
        for commit in commits:
            touches, touch_error = _touches_artifact(repo_root, commit, artifact_path)
            if touches is None:
                detail = f"git diff-tree query failed for commit {commit!r}"
                if touch_error:
                    detail = f"{detail}: {touch_error}"
                return SinceRange(
                    commits=(),
                    indeterminate=True,
                    detail=detail,
                )
            if not touches:
                filtered.append(commit)
        commits = filtered

    return SinceRange(commits=tuple(commits), indeterminate=False, detail="")


def verdict_from_range(rng: SinceRange) -> Verdict:
    """Map a `SinceRange` to STALE / FRESH / INDETERMINATE.

    INDETERMINATE takes precedence over an empty range: `rng.indeterminate`
    is checked first, so a failed query never reads FRESH.
    """
    if rng.indeterminate:
        return Verdict.INDETERMINATE
    return Verdict.STALE if rng.commits else Verdict.FRESH
