"""
coordinator_core.ops.doc_staleness — human-facing doc staleness detector.

Purpose: computes, per declared human-facing doc path (README.md, INSTALL.md,
CONTEXT.md, ...), whether the doc has gone stale — the repo moved on without
it. Staleness fires on `commits_since >= threshold_commits AND days_since >=
threshold_days` (AND, never OR): a quiet repo should not nag on the calendar
alone, and a doc touched yesterday should not fire mid-busy-week just because
many other commits landed. See docs/plans/2026-07-28-human-facing-doc-
staleness-detector.md § Calibration v2 for the AND rationale and the fleet
default thresholds (`C = 8000`, `D = 21`), owned by `doc_registry.py` (the
config-resolution module) — this module takes `threshold_commits`/
`threshold_days` as required positional arguments and does not itself carry
a default (AC4 per-repo override): every direct caller is expected to pass
its own thresholds explicitly, and `build_doc_staleness_report_from_registry`
resolves the fleet default via `doc_registry` for callers that don't.

Content-modifying discrimination (AC8) — the "last touch" a caller reasons
about is the last commit that plausibly represents a human reading and
revising the doc, not merely a commit whose diff happens to touch the path.
Two independent filters are applied, both required, on top of `git log
--follow` (the base commit list for the path, rename-aware):

    (a) whitespace-only / link-only diffs do not count — a diff whose added
        and removed lines are identical once whitespace is stripped (or,
        for link-only, once whitespace AND URL-looking tokens are stripped)
        does not represent someone reading and revising doc content.
    (b) "sweep drive-bys" do not count — a commit that touched more than
        `sweep_files_threshold` (K, default 10) files ACROSS THE WHOLE
        COMMIT while changing fewer than `sweep_lines_threshold` (L, default
        15) lines IN THIS DOC is a mechanical, repo-wide refactor pass that
        happened to graze the doc, not an authored edit to it. The AND
        between the two legs is load-bearing: a large commit that ALSO
        substantially rewrites the doc (many lines changed in the doc
        itself, even inside a huge commit) is correctly retained as
        authored — see Calibration v2's `0bfe32693` example (1,483 files
        touched, 282 lines changed in coordinator/README.md: authored, not
        swept).

        `L = 15` is not an independently-tuned value — it sits at the
        midpoint of a clean separation gap in this repo's own Calibration v2
        replay: every sweep-drive-by row tops out at 12 lines changed in the
        doc, every authored row (among commits that also cross the K=10
        files-touched floor) starts at 55, and any `L` in `13..20`
        reproduces the table's classifications identically — see the plan's
        Calibration v2 section for the row-by-row replay and the corrected
        robustness claim (it is the separation between the two classes that
        makes the predicate robust, not the specific value 15).

Both filters walk the SAME `git log --follow -p` stream that discovers
candidate commits in the first place, so rename-following stays consistent
between "which commits touched this doc" and "what changed in this doc in
each of them". This module relies on one specific `--follow` caveat:
`--follow` only tracks a single pathspec (multiple paths would silently
disable it), and its rename detection is similarity-threshold dependent —
a rewrite severe enough to fall below that threshold is seen as a delete
plus an unrelated add rather than a followed rename, and the history walk
would stop at that point. Every declared human-facing doc path is queried
individually (never as a multi-path pathspec) specifically to keep
`--follow` active for the whole call.

Missing declared path (AC1): a path absent from disk at the checked-out tree
is reported with `status: "absent"` — never an exception, never a stale
verdict. An empty list of doc paths produces a clean empty report — also
never an exception.

Negative-spec:
    - Does NOT write to the repo, does NOT mutate git state, does NOT halt
      or raise on a missing/absent doc — every per-doc failure mode reports
      through the per-doc `status` field.
    - Does NOT read `coordinator.local.md` or any registry file directly for
      the core per-doc/per-report computation — `compute_doc_staleness` and
      `build_doc_staleness_report` take repo_root/doc_paths/thresholds as
      plain arguments. `build_doc_staleness_report_from_registry` is the one
      convenience seam that reads `coordinator_core.ops.doc_registry`
      (landing concurrently, C4) — imported lazily inside the function body
      so this module's import never depends on that module's presence.
    - Does NOT attempt to interpret prose accuracy — that is content
      verification (`doc_content_verify.py`, C6b/C6c), a distinct op.

Spec backlink: docs/plans/2026-07-28-human-facing-doc-staleness-detector.md § C1
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from coordinator_core.win_portability import no_console_creationflags

DEFAULT_SWEEP_FILES_THRESHOLD = 10  # K
DEFAULT_SWEEP_LINES_THRESHOLD = 15  # L -- see module docstring, filter (b)

# Review: code-reviewer -- Finding 3. DEFAULT_THRESHOLD_COMMITS/DAYS used to
# be declared independently here AND in doc_registry.py (the actual
# registry-driven config-resolution module) -- two editable copies of the
# same tuned fleet default is a drift risk. doc_registry.py is now the
# single owner; import from there instead of re-declaring.

_COMMIT_RECORD_SEP = "\x1e"
_COMMIT_FIELD_SEP = "\x1f"

_WHITESPACE_RE = re.compile(r"\s+")
_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)|\(https?://[^)\s]+\)|https?://\S+")


def _run_git(repo_root: Path, *args: str) -> str:
    # Review: code-reviewer -- Finding 4. Explicit encoding, never the
    # platform locale default (commonly cp1252 on Windows, this fleet's P0
    # primary machine) -- a non-ASCII commit message/author name would
    # otherwise raise UnicodeDecodeError out of an uncaught path.
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **no_console_creationflags(),
    )
    if result.returncode != 0:
        # Review: code-reviewer -- Finding 5. Surface stderr on a genuine
        # git failure (not a repo, bad SHA, missing binary) so a
        # misconfigured repo_root is diagnosable rather than silently
        # indistinguishable from "this doc has no content-modifying
        # history" downstream.
        if result.stderr:
            print(
                f"doc_staleness: git {' '.join(args)} failed: {result.stderr.strip()}",
                file=sys.stderr,
            )
        return ""
    return result.stdout


def _normalize_lines(lines: list[str], *, strip_links: bool = False) -> str:
    text = "".join(lines)
    if strip_links:
        text = _LINK_RE.sub("", text)
    return _WHITESPACE_RE.sub("", text)


def _classify_doc_diff(added: list[str], removed: list[str]) -> str:
    """Classify one commit's diff hunks for a single doc path.

    Returns one of "no_change", "whitespace_only", "link_only", "content".
    "content" is the only classification that counts as a content-modifying
    touch under filter (a).
    """
    if not added and not removed:
        return "no_change"
    if _normalize_lines(added) == _normalize_lines(removed):
        return "whitespace_only"
    if _normalize_lines(added, strip_links=True) == _normalize_lines(
        removed, strip_links=True
    ):
        return "link_only"
    return "content"


def _log_follow_records(repo_root: Path, doc_path: str) -> list[dict[str, Any]]:
    """Candidate commits touching *doc_path*, newest first, rename-aware.

    Each record carries the raw added/removed line bodies for THIS path in
    THAT commit, parsed out of the same `--follow -p` patch stream that
    discovered the commit — see module docstring for why this is the base
    filter (a) and filter (b) both walk.
    """
    fmt = f"{_COMMIT_RECORD_SEP}%H{_COMMIT_FIELD_SEP}%cI"
    out = _run_git(
        repo_root, "log", "--follow", f"--format={fmt}", "-p", "--", doc_path
    )
    if not out:
        return []

    records: list[dict[str, Any]] = []
    for chunk in out.split(_COMMIT_RECORD_SEP):
        if not chunk.strip():
            continue
        header, _, patch = chunk.partition("\n")
        sha, _, committer_date = header.partition(_COMMIT_FIELD_SEP)
        sha = sha.strip()
        committer_date = committer_date.strip()
        if not sha:
            continue
        added: list[str] = []
        removed: list[str] = []
        for line in patch.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                continue
            if line.startswith("+"):
                added.append(line[1:])
            elif line.startswith("-"):
                removed.append(line[1:])
        records.append(
            {
                "sha": sha,
                "date": committer_date,
                "added": added,
                "removed": removed,
            }
        )
    return records


def _files_touched_in_commit(repo_root: Path, sha: str) -> int:
    """Count of files changed by *sha* across the WHOLE commit (no path
    restriction) — the filter-(b) sweep-drive-by denominator.

    Known, deliberate limitation (Review: code-reviewer -- Finding 7):
    `git diff-tree` on a merge commit shows no file list by default (no
    `-m`/`-c`/`--cc` flag here), so a merge commit always reports
    `files_touched == 0` and the sweep-drive-by filter (`files_touched >
    K`) can never fire for it, regardless of how many files the merge
    actually brought in. Not handled deliberately: `git log --follow`'s
    default history simplification (the base walk in `_log_follow_records`)
    already tends to skip merges that don't independently differ from
    their parents, and adding `-m`/`--first-parent` handling here would
    carry its own risk of changing which commits are considered
    "candidates" at all. If a merge commit is ever observed reaching this
    function with a large file count, the sweep filter is silently inert
    for it -- a recorded tradeoff, not an oversight.
    """
    out = _run_git(
        repo_root, "diff-tree", "--no-commit-id", "--name-only", "-r", sha
    )
    return len([line for line in out.splitlines() if line.strip()])


def _commits_since(repo_root: Path, sha: str) -> int:
    out = _run_git(repo_root, "rev-list", "--count", f"{sha}..HEAD")
    out = out.strip()
    return int(out) if out.isdigit() else 0


def _days_since(commit_date_iso: str, today: date) -> int:
    dt = datetime.fromisoformat(commit_date_iso)
    return (today - dt.date()).days


def _changed_areas(repo_root: Path, sha: str, *, limit: int) -> list[str]:
    """Top-level dirs ranked by touch count in the (sha, HEAD] window (AC6
    evidence) — a file directly at repo root is bucketed under "." """
    out = _run_git(
        repo_root, "log", "--name-only", "--format=", f"{sha}..HEAD"
    )
    counter: Counter[str] = Counter()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        top = line.split("/", 1)[0] if "/" in line else "."
        counter[top] += 1
    return [name for name, _count in counter.most_common(limit)]


def compute_doc_staleness(
    repo_root: str | Path,
    doc_path: str,
    threshold_commits: int,
    threshold_days: int,
    *,
    sweep_files_threshold: int = DEFAULT_SWEEP_FILES_THRESHOLD,
    sweep_lines_threshold: int = DEFAULT_SWEEP_LINES_THRESHOLD,
    today: Optional[date] = None,
    changed_areas_limit: int = 5,
) -> dict[str, Any]:
    """Compute the staleness verdict for one declared doc path.

    `today` is an injectable clock (defaults to `date.today()`) so tests can
    pin "now" against controlled commit dates rather than racing the wall
    clock.
    """
    repo_root = Path(repo_root)
    today = today or date.today()

    if not (repo_root / doc_path).is_file():
        return {"path": doc_path, "status": "absent"}

    last: Optional[dict[str, Any]] = None
    for record in _log_follow_records(repo_root, doc_path):
        classification = _classify_doc_diff(record["added"], record["removed"])
        if classification in ("no_change", "whitespace_only", "link_only"):
            continue
        lines_changed = len(record["added"]) + len(record["removed"])
        files_touched = _files_touched_in_commit(repo_root, record["sha"])
        if (
            files_touched > sweep_files_threshold
            and lines_changed < sweep_lines_threshold
        ):
            continue  # sweep drive-by (AC8 filter b) -- does not reset the clock
        last = {
            "sha": record["sha"],
            "date": record["date"],
            "lines_changed": lines_changed,
            "files_touched": files_touched,
        }
        break

    if last is None:
        return {
            "path": doc_path,
            "status": "no_content_modifying_history",
            "stale": False,
        }

    commits_since = _commits_since(repo_root, last["sha"])
    days_since = _days_since(last["date"], today)
    changed_areas = _changed_areas(
        repo_root, last["sha"], limit=changed_areas_limit
    )
    stale = commits_since >= threshold_commits and days_since >= threshold_days

    return {
        "path": doc_path,
        "status": "ok",
        "stale": stale,
        "commits_since": commits_since,
        "days_since": days_since,
        "last_touch_sha": last["sha"],
        "last_touch_date": last["date"],
        "changed_areas": changed_areas,
        "threshold_commits": threshold_commits,
        "threshold_days": threshold_days,
    }


def build_doc_staleness_report(
    repo_root: str | Path,
    doc_paths: list[str],
    threshold_commits: int,
    threshold_days: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Report over a list of declared doc paths. An empty list produces a
    clean, empty `docs: []` report — never an error (AC1)."""
    return {
        "docs": [
            compute_doc_staleness(
                repo_root, doc_path, threshold_commits, threshold_days, **kwargs
            )
            for doc_path in doc_paths
        ]
    }


def build_doc_staleness_report_from_registry(
    repo_root: str | Path, **kwargs: Any
) -> dict[str, Any]:
    """Convenience wrapper reading the per-repo doc registry
    (`coordinator_core.ops.doc_registry`, C4) for `human_facing_docs` and the
    per-repo threshold overrides, then delegating to
    `build_doc_staleness_report`.

    Imported lazily so this module stays importable (and its core functions
    callable/testable) without hard-depending on `doc_registry` at import time.

    Negative-spec: `doc_registry` returns a frozen `DocRegistryConfig`
    dataclass, not a dict — attribute access, never `.get()`. An earlier
    revision called a `read_doc_registry` name that does not exist and treated
    the result as a mapping; both halves raised at call time, and because this
    wrapper is what the workweek-complete brief consumes, the failure would
    have surfaced first at a live ceremony run rather than in a test.
    """
    from coordinator_core.ops.doc_registry import resolve_doc_registry_config

    registry = resolve_doc_registry_config(str(repo_root))
    doc_paths = registry.human_facing_docs or []
    threshold_commits = registry.doc_staleness_commits
    threshold_days = registry.doc_staleness_days
    return build_doc_staleness_report(
        repo_root, doc_paths, threshold_commits, threshold_days, **kwargs
    )
