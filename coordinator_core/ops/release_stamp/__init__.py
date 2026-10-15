"""coordinator_core.ops.release_stamp - JSON-RPC "completion.stamp_releases".

Stamps every `pending-release` entry under archive/completed with the earliest version tag
whose tree held it (status, released_in, released_at, released_sha). Writes files, commits
nothing. Report mode returns the same result and writes nothing. One git child: the tag read.

Callees:
    attribution.attribute(worktree_root, tags) -> Attribution
    writer.stamp_entry(worktree_root, rel_path, tag) -> tuple[dict | None, str | None]
        exactly one of the pair is set: the stamped record {path, released_in, released_at,
        released_sha}, or a skip reason.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Optional

from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.release_stamp.model import ReleaseTag, StampError

_FORMAT = "%09".join(
    (
        "%(refname:short)",
        "%(objectname)",
        "%(*objectname)",
        "%(tree)",
        "%(*tree)",
        "%(authordate:short)",
        "%(*authordate:short)",
    )
)


def list_release_tags(worktree_root: Path, tag_prefix: str) -> list[ReleaseTag]:
    """Version tags `<tag_prefix>v<int>.<int>.<int>`, lowest version first, via one git child."""
    res = run_git(
        ["for-each-ref", f"--format={_FORMAT}", f"refs/tags/{tag_prefix}v*"],
        cwd=str(worktree_root),
    )
    if res.returncode != 0:
        first = (res.stderr.strip().splitlines() or ["git for-each-ref failed"])[0]
        raise StampError("git-failed", first)
    pattern = re.compile(re.escape(tag_prefix) + r"v(\d+)\.(\d+)\.(\d+)")
    tags: list[ReleaseTag] = []
    for line in res.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        name, obj, peeled_obj, tree, peeled_tree, date, peeled_date = fields
        m = pattern.fullmatch(name)
        if m is None:
            continue
        tags.append(
            ReleaseTag(
                name=name,
                version=(int(m[1]), int(m[2]), int(m[3])),
                commit_sha=peeled_obj or obj,
                tree_sha=peeled_tree or tree,
                date=peeled_date or date,
            )
        )
    tags.sort(key=lambda t: t.version)
    return tags


def stamp_releases(worktree_root: Path, *, tag_prefix: str = "", report: bool = False) -> dict:
    """Attribute every pending entry; stamp each unless `report`. Result shape per the plan."""
    from coordinator_core.ops.release_stamp import attribution, writer

    root = Path(worktree_root)
    tags = list_release_tags(root, tag_prefix)
    attributed = attribution.attribute(root, tags)
    stamped: list[dict] = []
    skipped: list[str] = []
    for path in sorted(attributed.stamps):
        tag = attributed.stamps[path]
        if report:
            stamped.append(tag.record(path))
            continue
        record, skip = writer.stamp_entry(root, path, tag)
        if record is not None:
            stamped.append(record)
        elif skip:
            skipped.append(skip)
    return {
        "report": report,
        "tags_considered": len(tags),
        "stamped": stamped,
        "unreleased": {
            "count": len(attributed.unreleased),
            "paths": list(attributed.unreleased),
        },
        "skipped": skipped,
    }


def _refusal(reason: str, detail: str) -> dict:
    return {"error": f"completion.stamp_releases: {reason}: {detail}", "no_op": True}


@register_op("completion.stamp_releases")
async def _stamp_releases_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """MUTATING: stamp pending completion entries with the tag whose tree first held them.

    Params: tag_prefix (str, default ""), report (bool, default False).
    Returns the `stamp_releases` result, or `{"error", "no_op": True}` on a refusal.
    """
    tag_prefix = params.get("tag_prefix", "")
    report = params.get("report", False)
    if not isinstance(tag_prefix, str):
        return _refusal("bad-params", "'tag_prefix' must be a string")
    if not isinstance(report, bool):
        return _refusal("bad-params", "'report' must be a boolean")
    if repo_root is None:
        return _refusal("no-repo-root", "handler called without socket-authoritative common_dir")
    worktree = main_worktree_root(repo_root)
    try:
        return await asyncio.to_thread(
            stamp_releases, worktree, tag_prefix=tag_prefix, report=report
        )
    except StampError as exc:
        return _refusal(exc.reason, exc.detail)
