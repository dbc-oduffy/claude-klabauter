"""Version-bump rule for workweek-complete: the level recommendation and the `version-bump` rule.

No branch yields `major`: the completion-entry schema defines no breaking marker.
`recommend_level` runs on the workweek-complete brief path; its module top imports neither
`merge_assemble` nor `records_query`.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.git.run import git_out
from coordinator_core.workweek_complete.release.contract import ReleaseState, RuleResult

_STAMPED_OR_LATER = ("stamped", "merged-unpublished", "published-unarchived")
_LEVELS = ("patch", "minor")

# Pinned equal to workweek_start.header.WEEK_DIR / _BARE_DATE_RE by a test; importing that
# module pulls ops.check_weekly_staleness onto the brief path.
_WEEK_DIR = Path("state") / "week-changelog"
_BARE_DATE_RE = re.compile(r"^\*\*Week starting:\*\*[ \t]*(\d{4}-\d{2}-\d{2})")

_HEAD_BYTES = 2048
_WANTED = ("created", "nature", "status")


def local_release_tags(repo_root: Path, tag_prefix: str) -> list[str]:
    """Local `<tag_prefix>*` tags, highest version first; one git child."""
    out = git_out(["-C", str(repo_root), "tag", "--list", f"{tag_prefix}*", "--sort=-v:refname"])
    return [t.strip() for t in (out or "").splitlines() if t.strip()]


def _week_start(repo_root: Path) -> Optional[str]:
    try:
        lines = (repo_root / _WEEK_DIR / "HEADER.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        m = _BARE_DATE_RE.match(line)
        if m:
            return m.group(1)
    return None


def _scalar(raw: str) -> str:
    """A top-level frontmatter scalar: quotes stripped, a trailing ` # comment` dropped."""
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    cut = raw.find(" #")
    return (raw if cut < 0 else raw[:cut]).strip()


def _head_fields(path: Path) -> dict[str, str]:
    """`created`/`nature`/`status` from the top-level frontmatter in the file's first 2 KB."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return {}
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key in _WANTED:
            out[key] = _scalar(value)
    return out


def recommend_level(repo_root: Path) -> Optional[dict[str, str]]:
    """`{"disposition": "patch"|"minor", "rationale": ...}` from the week's pending-release
    entries; `None` when there is none. A `roadmap` entry gives `minor`, else `patch`.

    An entry counts when `status` is `pending-release` and, with a week start known,
    `created` is on or after it. An entry whose `status` line lies past the first 2 KB is not
    counted."""
    root = Path(repo_root)
    since = _week_start(root)
    completed = root / "archive" / "completed"
    try:
        months = sorted(d for d in completed.iterdir() if d.is_dir())
    except OSError:
        return None
    counts: Counter[str] = Counter()
    total = 0
    for month in months:
        if since and month.name < since[:7]:
            continue
        for entry in month.glob("*.md"):
            fields = _head_fields(entry)
            if fields.get("status") != "pending-release":
                continue
            if since and not (fields.get("created") and fields["created"] >= since):
                continue
            counts[fields.get("nature", "unknown")] += 1
            total += 1
    if not total:
        return None
    level = "minor" if counts.get("roadmap") else "patch"
    by_nature = ", ".join(f"{n}={c}" for n, c in sorted(counts.items()))
    return {
        "disposition": level,
        "rationale": f"{total} pending-release entries ({by_nature}); "
        f"{'a roadmap entry ships new capability' if level == 'minor' else 'no roadmap entry'}.",
    }


def _bump(parts: tuple[int, int, int], level: str) -> tuple[int, int, int]:
    major, minor, patch = parts
    if level == "minor":
        return (major, minor + 1, 0)
    return (major, minor, patch + 1)


def _fail(reason: str, **evidence: object) -> RuleResult:
    return RuleResult("version-bump", False, reason, dict(evidence))


def evaluate(
    repo_root: Path, *, point: Mapping[str, Any], tag_prefix: str, state: ReleaseState
) -> RuleResult:
    """Rule `version-bump`. A `stamped`-or-later state reuses `state.version` and never bumps."""
    from coordinator_core import merge_assemble

    if state.state in _STAMPED_OR_LATER:
        return RuleResult(
            "version-bump", True, evidence={"version": state.version, "reused": True}
        )

    recommendation = point.get("recommendation") or {}
    level = recommendation.get("disposition")
    rationale = str(recommendation.get("rationale", ""))
    if level not in _LEVELS:
        return _fail("no-recommendation")

    repo_root = Path(repo_root)
    tags = local_release_tags(repo_root, tag_prefix)
    latest = tags[0] if tags else None
    current = merge_assemble._parse_version_tag(latest, tag_prefix) if latest else None
    current = current or (0, 0, 0)
    new = _bump(current, level)
    version = ".".join(str(n) for n in new)
    tag = f"{tag_prefix}{version}"
    evidence: dict[str, object] = {
        "latest_tag": latest or "",
        "level": level,
        "rationale": rationale,
        "version": version,
        "tag": tag,
    }
    if new <= current:
        return _fail("not-an-increase", **evidence)
    if tag in tags or tag in merge_assemble.origin_tag_names(repo_root):
        return _fail("tag-exists", **evidence)
    return RuleResult("version-bump", True, evidence=evidence)
