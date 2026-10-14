"""Conflict manifest for a script-fired reconcile merge, plus the pinned
interface (outcome strings, CLI name, marker predicate) the start op, the
conclude op, the pre-Edit guard leg and the workday-start rule table share.

The manifest is one JSON file in the worktree-private gitdir, so it is never
tracked and never committed. `read` is fail-closed: anything but a well-formed
manifest whose `merge_head` equals the live MERGE_HEAD returns None. No git
spawn here; MERGE_HEAD is a file read.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Union

from coordinator_core.git.git_dir import resolve_git_dir

SCHEMA = 1

OUTCOME_CONFLICT_OPEN = "RECONCILE-CONFLICT-OPEN"
OUTCOME_CONCLUDED = "RECONCILED-MERGE-CONCLUDED"

REFUSAL_NO_MERGE = "RECONCILE-CONCLUDE-NO-MERGE"
REFUSAL_MANIFEST_STALE = "RECONCILE-CONCLUDE-MANIFEST-STALE"
REFUSAL_MARKERS = "RECONCILE-CONCLUDE-MARKERS"
REFUSAL_UNMERGED = "RECONCILE-CONCLUDE-UNMERGED"
REFUSAL_FOREIGN_STAGED = "RECONCILE-CONCLUDE-FOREIGN-STAGED"

CONCLUDE_CLI = "reconcile-merge-conclude"

_MARKER = re.compile(r"^(?:<{7} .*|={7}|>{7} .*)$", re.MULTILINE)

RepoRoot = Union[str, Path]


@dataclass(frozen=True)
class Manifest:
    merge_head: str
    branch: str
    paths: tuple[str, ...]


def manifest_path(repo_root: RepoRoot) -> Path:
    return resolve_git_dir(repo_root) / "coordinator-reconcile" / "conflict-manifest.json"


def live_merge_head(repo_root: RepoRoot) -> str | None:
    """The sha in MERGE_HEAD, or None when no merge is open or it is unreadable."""
    try:
        text = (resolve_git_dir(repo_root) / "MERGE_HEAD").read_text(encoding="utf-8")
    except OSError:
        return None
    sha = text.strip()
    return sha or None


def write(
    repo_root: RepoRoot, *, merge_head: str, branch: str, paths: Iterable[str]
) -> Path:
    """Write the manifest atomically; paths are normalised to sorted, unique,
    `/`-separated repo-relative strings."""
    norm = sorted({str(p).replace("\\", "/") for p in paths})
    target = manifest_path(repo_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "merge_head": merge_head, "branch": branch, "paths": norm}
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def read(repo_root: RepoRoot) -> Manifest | None:
    """The manifest, or None when absent, malformed, of another schema, or
    bound to a MERGE_HEAD other than the live one (including none)."""
    try:
        data = json.loads(manifest_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    merge_head = data.get("merge_head")
    branch = data.get("branch")
    paths = data.get("paths")
    if not isinstance(merge_head, str) or not merge_head:
        return None
    if not isinstance(branch, str):
        return None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return None
    if live_merge_head(repo_root) != merge_head:
        return None
    return Manifest(merge_head=merge_head, branch=branch, paths=tuple(paths))


def remove(repo_root: RepoRoot) -> None:
    """Delete the manifest; absent is not an error."""
    try:
        manifest_path(repo_root).unlink()
    except FileNotFoundError:
        pass


def has_marker(text: str) -> bool:
    """True when any line is a conflict marker: `<<<<<<< ` or `>>>>>>> `
    prefixed, or `=======` alone on the line."""
    return _MARKER.search(text.replace("\r\n", "\n")) is not None
