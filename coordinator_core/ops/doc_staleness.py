from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from coordinator_core.win_portability import no_console_creationflags

DEFAULT_SWEEP_FILES_THRESHOLD = 10
DEFAULT_SWEEP_LINES_THRESHOLD = 15

# DEFAULT_THRESHOLD_COMMITS/DAYS used to

_COMMIT_RECORD_SEP = "\x1e"
_COMMIT_FIELD_SEP = "\x1f"

_WHITESPACE_RE = re.compile(r"\s+")
_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)|\(https?://[^)\s]+\)|https?://\S+")


def _run_git(repo_root: Path, *args: str) -> str:
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
            continue
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
    from coordinator_core.ops.doc_registry import resolve_doc_registry_config

    registry = resolve_doc_registry_config(str(repo_root))
    doc_paths = registry.human_facing_docs or []
    threshold_commits = registry.doc_staleness_commits
    threshold_days = registry.doc_staleness_days
    return build_doc_staleness_report(
        repo_root, doc_paths, threshold_commits, threshold_days, **kwargs
    )
