"""
coordinator_core.ops.review_named_base -- ``named_review_base``: the commit an EM named in a live
revise note, as a per-path review base.

A named commit that touched a declared path is diffed from its first parent; one that did not is the
base itself. Resolution reads the object store in process: no spawn.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Collection, Dict, List, Optional

from coordinator_core.ops.review_unreviewed_bases import EMPTY_TREE_SHA

_HEX_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{7,40}(?![0-9A-Za-z])")


def _resolve(common_dir: Path, token: str, known_shas: Collection[str]) -> Optional[dict]:
    """``{sha, parents, tree}`` for a token naming a commit, else None. A full sha resolves in the
    object store; an abbreviation resolves only to a unique prefix of ``known_shas``."""
    from coordinator_core.git.commit_walk import commit_meta

    token = token.lower()
    sha = token
    if len(token) != 40:
        hits = {k.lower() for k in known_shas if k.lower().startswith(token)}
        if len(hits) != 1:
            return None
        sha = next(iter(hits))
    meta = commit_meta(common_dir, sha)
    if meta is None:
        return None
    return {"sha": sha, "parents": meta["parents"], "tree": meta["tree"]}


def _entry_sha(common_dir: Path, tree: Optional[str], path: str) -> Optional[str]:
    """Object sha at ``path`` inside ``tree``, None when absent or unreadable."""
    from coordinator_core.git.git_objects import read_object

    cur = tree
    sha = None
    for name in [p for p in path.replace("\\", "/").strip("/").split("/") if p]:
        obj = read_object(common_dir, cur) if cur else None
        if obj is None or obj[0] != "tree":
            return None
        m = re.match(
            rb"(?:\d+ [^\0]*\0.{20})*?\d+ " + re.escape(name.encode("utf-8", "surrogateescape")) + rb"\0(.{20})",
            obj[1], re.S,
        )
        if m is None:
            return None
        sha = cur = m.group(1).hex()
    return sha


def _touched(common_dir: Path, commit: dict, path: str) -> bool:
    parent_tree = None
    if commit["parents"]:
        from coordinator_core.git.commit_walk import commit_meta

        parent_meta = commit_meta(common_dir, commit["parents"][0])
        parent_tree = parent_meta["tree"] if parent_meta else None
    return _entry_sha(common_dir, commit["tree"], path) != _entry_sha(common_dir, parent_tree, path)


def named_review_base(
    repo_root: Path, run_id: str, paths: List[str], known_shas: Collection[str] = (),
) -> Dict[str, str]:
    """Declared ``paths`` -> review base from the newest live revise note of ``run_id`` (and its
    resumed-from predecessor) holding a hex token (7-40 chars) that names a commit. Newest note
    first, tokens left to right. ``{}`` when no note names one. No process is spawned."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.ops.dispatch_emit import op
    from coordinator_core.ops.dispatch_emit.requirements_ledger import LedgerRefused, read_run, run_dir

    if not paths or not run_id:
        return {}
    try:
        prior = (read_run(run_dir(repo_root, run_id)) or {}).get("resumed_from")
        notes = op._live_carried_notes(repo_root, run_id, prior if isinstance(prior, str) else None)
        common_dir = resolve_git_common_dir(repo_root)
    except (LedgerRefused, ValueError, OSError):
        return {}
    for entry in reversed(notes):
        for token in _HEX_TOKEN_RE.findall(entry["note"]):
            commit = _resolve(common_dir, token, known_shas)
            if commit is None:
                continue
            first_parent = commit["parents"][0] if commit["parents"] else EMPTY_TREE_SHA
            return {
                p: first_parent if _touched(common_dir, commit, p) else commit["sha"]
                for p in paths
            }
    return {}
