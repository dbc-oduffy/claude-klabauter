"""Doc staleness: a registered human-facing doc is stale when `commits_since >= C AND days_since >= D`, both measured from its newest genuine authored touch.

A touch is genuine unless its patch to the doc is whitespace-only or link-only, or it is a sweep
drive-by (`files_touched > K AND lines_changed_in_doc < L`). There is no initial-add exemption and
no `--follow`: a rename or move is an add under the new path, so it counts as a touch.
`days_since` counts whole days from the touch's committer date to `today`.

Lever: one path-limited `log -p` over every doc (widened only for docs it leaves unanswered), one
`diff-tree --stdin` over the small candidate patches, one `rev-list --parents` over the range from HEAD
past the oldest touch with every count taken in process; 3 spawns when the first round answers every
doc, at most 8 after widening, whatever the doc count. The walk is cheap only on a repo with a commit-graph (the fleet's
daily maintenance tier writes one); the op reads and never writes the caller's git dir.
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

from coordinator_core.git.run import run_git

DEFAULT_SWEEP_FILES_THRESHOLD = 10
DEFAULT_SWEEP_LINES_THRESHOLD = 15

_RECORD_SEP = "\x1e"
_FIELD_SEP = "\x1f"
_LOG_FORMAT = f"--format={_RECORD_SEP}%H{_FIELD_SEP}%cI"
_DIFF_HEADER = "diff --git "
_GIT_BASE = ("--literal-pathspecs", "-c", "core.quotepath=false")

# `log -n` ends the walk at the newest touches, which almost always answer; the last
# round is unbounded.
_DEPTH_PER_DOC = (4, 32, 0)

_WHITESPACE_RE = re.compile(r"\s+")
_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)|\(https?://[^)\s]+\)|https?://\S+")


class _Patch(NamedTuple):
    """One commit's patch to one doc: its identity and the lines it added and removed."""

    sha: str
    date: str
    added: list[str]
    removed: list[str]


def _git(repo_root: Path, *args: str, stdin: Optional[bytes] = None) -> Optional[str]:
    """Stdout of `git <args>`, or None when the read failed or timed out."""
    result = run_git([*_GIT_BASE, *args], cwd=str(repo_root), input=stdin)
    if result.timed_out or result.returncode != 0:
        detail = "timed out" if result.timed_out else (result.stderr or "").strip()
        print(f"doc_staleness: git {args[0]} failed: {detail}", file=sys.stderr)
        return None
    return result.stdout


def _squash(lines: list[str], *, strip_links: bool = False) -> str:
    text = "".join(lines)
    if strip_links:
        text = _LINK_RE.sub("", text)
    return _WHITESPACE_RE.sub("", text)


def _is_content_change(patch: _Patch) -> bool:
    """True when the patch changes more than whitespace or link targets."""
    if not patch.added and not patch.removed:
        return False
    if _squash(patch.added) == _squash(patch.removed):
        return False
    return _squash(patch.added, strip_links=True) != _squash(patch.removed, strip_links=True)


def _parse_patches(out: str, docs: set[str]) -> dict[str, list[_Patch]]:
    """Per-doc patches of a `log -p` read, newest commit first.

    A merge commit carries no patch, so it never appears. Lines outside a hunk (file headers)
    are skipped; inside a hunk every `+`/`-` line counts, so a `---` rule line is a removal.
    """
    found: dict[str, list[_Patch]] = {d: [] for d in docs}
    for chunk in out.split(_RECORD_SEP):
        header, _, body = chunk.partition("\n")
        sha, _, commit_date = header.partition(_FIELD_SEP)
        sha, commit_date = sha.strip(), commit_date.strip()
        if not sha:
            continue
        current: Optional[tuple[list[str], list[str]]] = None
        in_hunk = False
        for line in body.splitlines():
            if line.startswith(_DIFF_HEADER):
                rest = line[len(_DIFF_HEADER) + 2 :]
                path = rest[: (len(rest) - 2) // 2]
                current = ([], [])  # the stored _Patch aliases these lists; append, never rebind
                in_hunk = False
                if path in docs:
                    found[path].append(_Patch(sha, commit_date, *current))
                else:
                    current = None
            elif current is None:
                continue
            elif line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line[:1] == "+":
                current[0].append(line[1:])
            elif in_hunk and line[:1] == "-":
                current[1].append(line[1:])
    return found


def _file_counts(repo_root: Path, shas: list[str]) -> dict[str, int]:
    """Paths touched per commit; a rename is two paths. A failed read counts 0."""
    counts = dict.fromkeys(shas, 0)
    out = _git(
        repo_root,
        "diff-tree",
        "--root",
        "-r",
        "--no-renames",
        "--name-only",
        "--stdin",
        stdin=("\n".join(shas) + "\n").encode("ascii"),
    )
    current: Optional[str] = None
    for line in (out or "").splitlines():
        line = line.strip()
        if line in counts:
            current = line
        elif line and current is not None:
            counts[current] += 1
    return counts


def _newest_touches(
    repo_root: Path,
    docs: list[str],
    *,
    sweep_files_threshold: int,
    sweep_lines_threshold: int,
) -> Optional[dict[str, Optional[_Patch]]]:
    """Each doc's newest genuine touch, or None for a doc with none; None overall when the log read fails."""
    answers: dict[str, Optional[_Patch]] = {}
    files: dict[str, int] = {}
    pending = list(docs)
    for per_doc in _DEPTH_PER_DOC:
        limit = [f"-n{per_doc * len(pending)}"] if per_doc else []
        out = _git(
            repo_root, "log", _LOG_FORMAT, *limit, "-p", "--no-renames", "--", *pending
        )
        if out is None:
            return None
        truncated = bool(limit) and out.count(_RECORD_SEP) >= per_doc * len(pending)
        candidates = {
            doc: [p for p in patches if _is_content_change(p)]
            for doc, patches in _parse_patches(out, set(pending)).items()
        }
        # A patch of sweep_lines_threshold lines or more is never a sweep, so only smaller
        # patches need a file count.
        small = {
            p.sha
            for patches in candidates.values()
            for p in patches
            if len(p.added) + len(p.removed) < sweep_lines_threshold
        }
        unseen = sorted(small - files.keys())
        if unseen:
            files.update(_file_counts(repo_root, unseen))
        for doc in pending:
            for patch in candidates[doc]:
                lines = len(patch.added) + len(patch.removed)
                if not (lines < sweep_lines_threshold and files[patch.sha] > sweep_files_threshold):
                    answers[doc] = patch
                    break
            else:
                if not truncated:
                    answers[doc] = None
        pending = [d for d in pending if d not in answers]
        if not pending:
            break
    return answers


def _commits_since(repo_root: Path, touches: dict[str, str]) -> dict[str, int]:
    """Exact `rev-list --count <sha>..HEAD` per sha of `touches` (sha -> committer date), from one spawn.

    The read is `rev-list --parents HEAD ^<oldest touch>`; each count is the range size minus the
    commits reachable from that touch inside the range. That is exact only when the oldest touch
    is an ancestor of every other touch; a touch that does not reach it widens the read to the
    whole history (a second spawn, still independent of the doc count). A sha the read fails
    to resolve is absent from the result.
    """
    shas = list(dict.fromkeys(touches))
    base = min(shas, key=lambda s: datetime.fromisoformat(touches[s]))
    for bounded in (True, False):
        out = _git(repo_root, "rev-list", "--parents", "HEAD", *([f"^{base}"] if bounded else []))
        if out is None:
            return {}
        parents = {}
        for line in out.splitlines():
            sha, *rest = line.split()
            parents[sha] = rest
        counts: dict[str, int] = {}
        exact = True
        for sha in shas:
            if bounded and sha == base:
                counts[sha] = len(parents)
                continue
            if sha not in parents:
                if bounded:
                    exact = False
                    break
                continue
            seen = {sha}
            stack = [sha]
            reached_base = False
            while stack:
                for parent in parents[stack.pop()]:
                    if parent in parents:
                        if parent not in seen:
                            seen.add(parent)
                            stack.append(parent)
                    elif parent == base:
                        reached_base = True
            if bounded and not reached_base:
                exact = False
                break
            counts[sha] = len(parents) - len(seen)
        if exact:
            return counts
    return {}


def _days_since(commit_date: str, today: date) -> int:
    return (today - datetime.fromisoformat(commit_date).date()).days


def build_doc_staleness_report(
    repo_root: str | Path,
    doc_paths: list[str],
    threshold_commits: int,
    threshold_days: int,
    *,
    sweep_files_threshold: int = DEFAULT_SWEEP_FILES_THRESHOLD,
    sweep_lines_threshold: int = DEFAULT_SWEEP_LINES_THRESHOLD,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Per-doc staleness for `doc_paths`, in order; every entry carries `path`, `status` and `stale`.

    `status` is `absent` (no such file), `no_content_modifying_history` (no genuine touch, or the
    history could not be read) or `ok`; only `ok` entries carry `commits_since`, `days_since`,
    `last_touch_sha`, `last_touch_date`, `changed_areas` (always `[]`) and both thresholds. A
    failed git read degrades the doc to `stale: False` and never raises.
    """
    repo_root = Path(repo_root)
    today = today or datetime.now(timezone.utc).date()
    present = list(dict.fromkeys(d for d in doc_paths if (repo_root / d).is_file()))
    touches = (
        _newest_touches(
            repo_root,
            present,
            sweep_files_threshold=sweep_files_threshold,
            sweep_lines_threshold=sweep_lines_threshold,
        )
        if present
        else {}
    ) or {}

    found = {t.sha: t.date for t in touches.values() if t is not None}
    counts = _commits_since(repo_root, found) if found else {}

    entries: list[dict[str, Any]] = []
    for doc in doc_paths:
        if doc not in present:
            entries.append({"path": doc, "status": "absent", "stale": False})
            continue
        touch = touches.get(doc)
        if touch is None:
            entries.append(
                {"path": doc, "status": "no_content_modifying_history", "stale": False}
            )
            continue
        commits_since = counts.get(touch.sha)
        if commits_since is None:
            entries.append(
                {"path": doc, "status": "no_content_modifying_history", "stale": False}
            )
            continue
        days_since = _days_since(touch.date, today)
        entries.append(
            {
                "path": doc,
                "status": "ok",
                "stale": commits_since >= threshold_commits
                and days_since >= threshold_days,
                "commits_since": commits_since,
                "days_since": days_since,
                "last_touch_sha": touch.sha,
                "last_touch_date": touch.date,
                "changed_areas": [],
                "threshold_commits": threshold_commits,
                "threshold_days": threshold_days,
            }
        )
    return {"docs": entries}


def build_doc_staleness_report_from_registry(
    repo_root: str | Path, only_doc: Optional[str] = None, **kwargs: Any
) -> dict[str, Any]:
    """`build_doc_staleness_report` over the repo's registered docs and thresholds.

    `only_doc` narrows the registered list (before the history walk) to that exact path.
    """
    from coordinator_core.ops.doc_registry import resolve_doc_registry_config

    registry = resolve_doc_registry_config(str(repo_root))
    docs = registry.human_facing_docs or []
    if only_doc is not None:
        docs = [d for d in docs if d == only_doc]
    return build_doc_staleness_report(
        repo_root,
        docs,
        registry.doc_staleness_commits,
        registry.doc_staleness_days,
        **kwargs,
    )
