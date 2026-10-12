"""Release state detector: how far an earlier workweek-complete run got.

Reads the tree, the tags and `state/week-changelog/`; writes nothing and starts no remote
call (the spec fetches `origin/main` first).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.git.run import git_out
from coordinator_core.merge_assemble import _parse_version_tag
from coordinator_core.workweek_complete.release.contract import (
    DAILY_BLOCK_RE,
    PENDING_RELEASE_SUFFIX,
    ReleaseState,
)
from coordinator_core.workweek_complete.release.declared import declared_version
from coordinator_core.workweek_complete.release.version_rule import local_release_tags

_WEEK_DIR = "state/week-changelog"
_WEEK_START_RE = re.compile(r"^\*\*Week starting:\*\*\s*(\d{4}-\d{2}-\d{2})", re.M)
_PRIOR_RE = re.compile(r"^\*\*Prior week released:\*\*(.*)$", re.M)
_NOTES_GLOB = ":(glob)archive/release-notes/*-v{version}.md"


def _version_key(version: Optional[str], prefix: str) -> Optional[Tuple[int, int, int]]:
    return _parse_version_tag(f"{prefix}{version}", prefix) if version else None


def _week_activity(repo_root: Path) -> Tuple[bool, str]:
    """(daily block or pending entry present for the header's week, header text)."""
    week_dir = repo_root / _WEEK_DIR
    try:
        header = (week_dir / "HEADER.md").read_text(encoding="utf-8")
    except OSError:
        header = ""
    m = _WEEK_START_RE.search(header)
    start = m.group(1) if m else ""
    try:
        names = sorted(p.name for p in week_dir.iterdir() if p.is_file())
    except OSError:
        return False, header
    for name in names:
        if name.endswith(PENDING_RELEASE_SUFFIX):
            return True, header
        d = DAILY_BLOCK_RE.match(name)
        if d and d.group(1) >= start:
            return True, header
    return False, header


def _rev(repo_root: Path, args: List[str]) -> str:
    return git_out(["-C", str(repo_root), *args]) or ""


def detect(repo_root, *, tag_prefix: str, release_mode: str) -> ReleaseState:
    """Classify the release as one of `RELEASE_STATES`, in the order the plan states.

    At most seven git children: the tag list (1), `declared_version` at `HEAD` (2) and at
    `origin/main` (2), the tag peel (1), the first-parent notes log (1). With `release_mode`
    `none` only `unstamped` or `nothing-to-release` is returned and only the tag list and the
    `HEAD` read are spent.
    """
    root = Path(repo_root)
    tags = local_release_tags(root, tag_prefix)
    latest = next((t for t in tags if _parse_version_tag(t, tag_prefix)), "")
    latest_key = _parse_version_tag(latest, tag_prefix) if latest else None

    def newer(version: Optional[str]) -> bool:
        key = _version_key(version, tag_prefix)
        return key is not None and (latest_key is None or key > latest_key)

    head_v = declared_version(root, treeish="HEAD")
    active, header = _week_activity(root)

    if not active and not newer(head_v):
        return ReleaseState("nothing-to-release")
    if release_mode == "none":
        return ReleaseState("unstamped")

    main_v = declared_version(root, treeish="origin/main")
    tag_set = set(tags)

    def merge_sha_of(version: str) -> str:
        return _rev(
            root,
            ["log", "--first-parent", "-1", "--format=%H", "origin/main", "--",
             _NOTES_GLOB.format(version=version)],
        )

    if main_v and _version_key(main_v, tag_prefix) is not None:
        tag = f"{tag_prefix}{main_v}"
        if tag in tag_set:
            merge_sha = merge_sha_of(main_v)
            peeled = _rev(root, ["rev-parse", f"{tag}^{{commit}}"]) if merge_sha else ""
            if merge_sha and peeled == merge_sha:
                prior = _PRIOR_RE.search(header)
                named = prior and re.search(
                    rf"(?<![\w.]){re.escape(tag)}(?![\w.])", prior.group(1)
                )
                if not named:
                    return ReleaseState("published-unarchived", main_v, tag, merge_sha)
        elif newer(main_v):
            return ReleaseState("merged-unpublished", main_v, tag, merge_sha_of(main_v))

    if newer(head_v) and f"{tag_prefix}{head_v}" not in tag_set and main_v != head_v:
        return ReleaseState("stamped", head_v, f"{tag_prefix}{head_v}")
    return ReleaseState("unstamped")
