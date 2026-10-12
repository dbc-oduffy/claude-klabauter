"""Version-bump rule for workweek-complete: the level recommendation and the `version-bump` rule.

No branch yields `major`: the completion-entry schema defines no breaking marker.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core import merge_assemble
from coordinator_core.git.run import git_out
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.workweek_complete.release.contract import ReleaseState, RuleResult
from coordinator_core.workweek_start.header import _BARE_DATE_RE, WEEK_DIR

_STAMPED_OR_LATER = ("stamped", "merged-unpublished", "published-unarchived")
_LEVELS = ("patch", "minor")


def local_release_tags(repo_root: Path, tag_prefix: str) -> list[str]:
    """Local `<tag_prefix>*` tags, highest version first; one git child."""
    out = git_out(["-C", str(repo_root), "tag", "--list", f"{tag_prefix}*", "--sort=-v:refname"])
    return [t.strip() for t in (out or "").splitlines() if t.strip()]


def _week_start(repo_root: Path) -> Optional[str]:
    try:
        lines = (repo_root / WEEK_DIR / "HEADER.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        m = _BARE_DATE_RE.match(line)
        if m:
            return m.group(1)
    return None


def recommend_level(repo_root: Path) -> Optional[dict[str, str]]:
    """`{"disposition": "patch"|"minor", "rationale": ...}` from the week's pending-release
    entries; `None` when there is none. A `roadmap` entry gives `minor`, else `patch`."""
    records = query_records(
        "completion",
        Path(repo_root),
        where="status=pending-release",
        since=_week_start(Path(repo_root)),
    )
    if not records:
        return None
    counts = Counter(str(r["frontmatter"].get("nature", "unknown")) for r in records)
    level = "minor" if counts.get("roadmap") else "patch"
    by_nature = ", ".join(f"{n}={c}" for n, c in sorted(counts.items()))
    return {
        "disposition": level,
        "rationale": f"{len(records)} pending-release entries ({by_nature}); "
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
