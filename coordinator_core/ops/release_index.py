"""Release index: one file per version tag naming the completion entries it first shipped.

Index shape, ``archive/releases/<tag>.yaml``::

    tag: v0.6.9
    sha: <40-hex commit sha the tag names, peeled>
    date: YYYY-MM-DD          # the tagged commit's author date
    entries:                  # repo-relative POSIX paths, sorted
      - archive/completed/2026-10/<entry>.md

A tag covers the entry files under ``archive/completed/*/`` in the tagged commit's tree whose
file name no existing index lists (join by file name, so a moved entry stays released). Entry
files are never opened. An existing index is never rewritten; one whose ``sha`` differs from the
ref refuses the whole op as ``retargeted`` before any write.

The file is emitted by hand and read by a line parser (``yaml.safe_load`` reads it too); a path
outside the plain-scalar character set is written double-quoted.

Callees: ``coordinator_core.git.run.run_git`` (one ref read), ``coordinator_core.git.git_objects``
and ``git_state`` (in-process tree reads), ``coordinator_core.locked_write.locked_rmw`` (one
write per tag), ``coordinator_core.session.declared_writes.declare_write``.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import _parse_tree_entries, read_commit
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import locked_rmw
from coordinator_core.session.declared_writes import declare_write

INDEX_ROOT = "archive/releases"
ENTRY_ROOT = "archive/completed"

_VERSION_TAIL = re.compile(r"v(\d+)\.(\d+)\.(\d+)$")
_PLAIN = re.compile(r"^[A-Za-z0-9_./-]+$")
_NTFS_BAD = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_NTFS_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.IGNORECASE)
_TREE_MODE = 0o40000
_REF_FORMAT = "%(refname:strip=2)%09%(*objectname)%09%(objectname)%09%(*authordate:short)%09%(authordate:short)"


class ReleaseIndexError(Exception):
    """A refusal of the whole op. ``reason`` is one of ``bad-params``, ``git-failed``,
    ``tree-unreadable``, ``retargeted``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _version_key(tag: str) -> tuple[int, int, int]:
    m = _VERSION_TAIL.search(tag)
    assert m is not None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _bad_tag(tag: str) -> bool:
    if "/" in tag:
        return True
    return bool(_NTFS_BAD.search(tag) or tag.endswith((" ", ".")) or _NTFS_RESERVED.match(tag))


def _scalar(value: str) -> str:
    return value if _PLAIN.match(value) else json.dumps(value)


def _unscalar(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return str(json.loads(raw))
        except ValueError:
            return raw
    return raw


def _emit(tag: str, sha: str, date: str, entries: list[str]) -> str:
    lines = [f"tag: {_scalar(tag)}", f"sha: {sha}", f"date: {date}"]
    if entries:
        lines.append("entries:")
        lines.extend(f"  - {_scalar(p)}" for p in entries)
    else:
        lines.append("entries: []")
    return "\n".join(lines) + "\n"


def _parse_index(text: str) -> tuple[str, str, list[str]]:
    """Return ``(tag, sha, entries)`` from an index body."""
    tag = sha = ""
    entries: list[str] = []
    for line in text.splitlines():
        if line.startswith("  - "):
            entries.append(_unscalar(line[4:]))
        elif line.startswith("tag:"):
            tag = _unscalar(line[4:])
        elif line.startswith("sha:"):
            sha = line[4:].strip()
    return tag, sha, entries


def _read_indices(worktree_root: Path) -> dict[str, tuple[str, list[str]]]:
    """Map tag -> ``(sha, entries)`` for every index file present."""
    index_dir = worktree_root / INDEX_ROOT
    out: dict[str, tuple[str, list[str]]] = {}
    try:
        files = [p for p in index_dir.iterdir() if p.suffix == ".yaml"]
    except OSError:
        return out
    for path in files:
        tag, sha, entries = _parse_index(path.read_text(encoding="utf-8"))
        out[tag or path.stem] = (sha, entries)
    return out


def _tree_entries(common_dir: Path, sha: str) -> dict[str, tuple[int, str]]:
    obj = read_object(common_dir, sha)
    parsed = _parse_tree_entries(obj[1]) if obj is not None and obj[0] == "tree" else None
    if parsed is None:
        raise ReleaseIndexError("tree-unreadable", sha)
    return parsed


def _entry_paths(common_dir: Path, tree_sha: str, month_cache: dict[str, list[str]]) -> list[str]:
    """Every ``archive/completed/<month>/<name>.md`` path the root tree ``tree_sha`` holds."""
    node = _tree_entries(common_dir, tree_sha)
    for part in ENTRY_ROOT.split("/"):
        hit = node.get(part)
        if hit is None or hit[0] != _TREE_MODE:
            return []
        node = _tree_entries(common_dir, hit[1])
    paths: list[str] = []
    for month, (mode, sha) in node.items():
        if mode != _TREE_MODE:
            continue
        names = month_cache.get(sha)
        if names is None:
            names = [n for n in _tree_entries(common_dir, sha) if n.endswith(".md")]
            month_cache[sha] = names
        paths.extend(f"{ENTRY_ROOT}/{month}/{n}" for n in names)
    return paths


def _list_tags(worktree_root: Path, tag_prefix: str) -> list[tuple[str, str, str]]:
    """Version tags ``<tag_prefix>v<int>.<int>.<int>`` as ``(name, commit sha, date)``, in version order."""
    res = run_git(
        ["for-each-ref", f"--format={_REF_FORMAT}", f"refs/tags/{tag_prefix}v*"],
        cwd=str(worktree_root),
    )
    if not res.ok:
        raise ReleaseIndexError("git-failed", (res.stderr or "").strip()[:200])
    tags: list[tuple[str, str, str]] = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        name, peeled, direct, peeled_date, direct_date = parts
        if not name.startswith(tag_prefix) or not re.fullmatch(r"v\d+\.\d+\.\d+", name[len(tag_prefix):]):
            continue
        tags.append((name, peeled or direct, peeled_date or direct_date))
    tags.sort(key=lambda t: _version_key(t[0]))
    return tags


def write_release_index(worktree_root: Path, *, tag_prefix: str = "", report: bool = False) -> dict:
    """Write ``archive/releases/<tag>.yaml`` for every version tag ``<tag_prefix>v<int>.<int>.<int>``
    lacking one, in version order.

    Contract: one ref read (one git child), tree reads in process, one locked write plus
    ``declare_write`` per tag, no commit. ``report=True`` writes nothing and returns the same
    result. Returns ``{report, tags_considered, written: [{path, tag, entries}], skipped:
    [{tag, reason}]}`` where ``entries`` is the sorted list of repo-relative paths the index
    lists. A tag with ``/`` or an NTFS-illegal component is listed in ``skipped`` with reason
    ``bad-tag``. Raises ``ReleaseIndexError`` on refusal.
    """
    if not isinstance(tag_prefix, str) or not isinstance(report, bool):
        raise ReleaseIndexError("bad-params", "tag_prefix must be str, report bool")
    worktree_root = Path(worktree_root)
    tags = _list_tags(worktree_root, tag_prefix)
    existing = _read_indices(worktree_root)

    for name, sha, _date in tags:
        if name in existing and existing[name][0] != sha:
            raise ReleaseIndexError("retargeted", f"{name}: index {existing[name][0]} != ref {sha}")

    covered = {p.rpartition("/")[2] for _sha, entries in existing.values() for p in entries}
    common_dir = resolve_git_common_dir(worktree_root)
    month_cache: dict[str, list[str]] = {}
    written: list[dict] = []
    skipped: list[dict] = []
    for name, sha, date in tags:
        if name in existing:
            continue
        if _bad_tag(name):
            skipped.append({"tag": name, "reason": "bad-tag"})
            continue
        commit = read_commit(worktree_root, sha)
        if commit is None:
            raise ReleaseIndexError("tree-unreadable", f"{name}: commit {sha}")
        fresh = sorted(
            p for p in _entry_paths(common_dir, commit.tree, month_cache) if p.rpartition("/")[2] not in covered
        )
        covered.update(p.rpartition("/")[2] for p in fresh)
        rel = f"{INDEX_ROOT}/{name}.yaml"
        if not report:
            _write_one(worktree_root, rel, _emit(name, sha, date, fresh))
        written.append({"path": rel, "tag": name, "entries": fresh})
    return {"report": report, "tags_considered": len(tags), "written": written, "skipped": skipped}


def _write_one(worktree_root: Path, rel: str, text: str) -> None:
    target = worktree_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    locked_rmw(target, lambda old: old or text, repo_root=worktree_root, missing_ok=True)
    declare_write(target)


def released_names(worktree_root: Path) -> dict[str, str]:
    """Map each entry file name to the tag whose index lists it.

    Contract: one read per index file under ``archive/releases/``; opens no entry file; a missing
    index directory yields ``{}``. "Not yet in a release" is the complement of this mapping. A
    name listed by several indices maps to the earliest version tag.
    """
    indices = _read_indices(Path(worktree_root))
    out: dict[str, str] = {}
    for tag in sorted(indices, key=lambda t: _version_key(t) if _VERSION_TAIL.search(t) else (-1, 0, 0)):
        for path in indices[tag][1]:
            out.setdefault(path.rpartition("/")[2], tag)
    return out


def _refusal(reason: str, detail: str) -> dict:
    return {"error": f"{reason}: {detail}", "no_op": True, "reason": reason}


@register_op("release.write_index")
async def _write_index_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``release.write_index`` handler (MUTATING).

    params: {tag_prefix: str = "", report: bool = False, repo_root: str (optional; wins over the
    IPC-injected root)}
    -> the ``write_release_index`` result, or ``{error, no_op: True, reason}`` on refusal.
    """
    tag_prefix = params.get("tag_prefix", "")
    report = params.get("report", False)
    target = params.get("repo_root") or repo_root
    if target is None:
        return _refusal("bad-params", "no repo_root")
    worktree = main_worktree_root(Path(target))
    try:
        return await asyncio.to_thread(write_release_index, worktree, tag_prefix=tag_prefix, report=report)
    except ReleaseIndexError as exc:
        return _refusal(exc.reason, exc.detail)
