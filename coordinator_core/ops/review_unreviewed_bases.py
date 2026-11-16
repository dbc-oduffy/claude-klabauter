"""
coordinator_core.ops.review_unreviewed_bases -- ``unreviewed_introducer_bases``: per declared path,
the first parent of the newest commit at or before ``base`` that touched it, when that commit is
unreviewed.

A commit is reviewed when its message carries an ``Inline-Review:`` trailer or its sha is in the
materialized reviewed set (DR-377, a file read, no git). A grind ``review: post-landing`` trailer is
no review: it promises one. A path whose newest toucher is reviewed is omitted and keeps the run
base. The lookup is one ``git log`` spawn, pathspec-scoped like ``review_peer_bases``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from coordinator_core.git.literal_pathspec import git_pathspec, is_magic_pathspec
from coordinator_core.git.run import LOCAL_PLUMBING_BUDGET_SECS, run_git
from coordinator_core.ops.review_peer_bases import (
    _FIELD_SEP,
    _COMMIT_MARK,
    _declared_matcher,
    _has_object_store,
    _scope_pathspecs,
)
from coordinator_core.ops.review_stamp import _BODY_INLINE_REVIEW_RE

EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Bounds the walk when a declared path was last touched long ago: git stops after this many
# pathspec-matching commits, so cost stays flat. A path with no toucher inside the cap keeps the
# run base and stays no-delta.
_INTRODUCER_COMMIT_CAP = 200

_GIT_TIMEOUT_SECS = LOCAL_PLUMBING_BUDGET_SECS
_FORMAT = f"--format={_COMMIT_MARK}%H{_FIELD_SEP}%P{_FIELD_SEP}%B{_FIELD_SEP}"


def _carries_review_trailer(body: str) -> bool:
    return any(_BODY_INLINE_REVIEW_RE.match(line.strip()) for line in body.splitlines())


def unreviewed_introducer_bases(repo_root: Path, base: str, paths: List[str]) -> Dict[str, str]:
    """Declared ``paths`` -> first-parent sha (``EMPTY_TREE_SHA`` for a root commit) of the newest
    commit reachable from ``base`` that touched the path, only when that commit is unreviewed.
    One ``git log`` spawn; none when ``paths`` is empty. A git failure or timeout returns ``{}``
    (every path keeps the run base). A path with no toucher inside ``_INTRODUCER_COMMIT_CAP``
    commits is omitted."""
    if not paths or not _has_object_store(repo_root):
        return {}
    in_python = not any(is_magic_pathspec(p) for p in paths)
    argv = [
        "-C", str(repo_root), "log", "-n", str(_INTRODUCER_COMMIT_CAP), "--full-history",
        "--no-renames", "--name-only", _FORMAT, base,
    ]
    scoped = _scope_pathspecs(paths) if in_python else [git_pathspec(p) for p in paths]
    if scoped:
        argv += ["--", *scoped]
    proc = run_git(argv, timeout=_GIT_TIMEOUT_SECS)
    if proc.returncode != 0:
        return {}
    matches = _declared_matcher(paths) if in_python else (lambda _name: True)
    newest: Dict[str, tuple] = {}
    listed: set = set()
    for chunk in proc.stdout.split(_COMMIT_MARK):
        parts = chunk.split(_FIELD_SEP, 3)
        if len(parts) != 4 or not parts[0].strip():
            continue
        sha = parts[0].strip()
        parents = parts[1].split()
        listed.add(sha)
        carries = _carries_review_trailer(parts[2])
        for name in parts[3].splitlines():
            name = name.strip()
            if name and matches(name) and name not in newest:
                newest[name] = (sha, parents[0] if parents else EMPTY_TREE_SHA, carries)
    candidates = {n: v for n, v in newest.items() if not v[2]}
    if not candidates:
        return {}
    from coordinator_core.ops.review_coverage_core import build_reviewed_set

    reviewed = build_reviewed_set(listed, str(repo_root))
    return {n: parent for n, (sha, parent, _c) in candidates.items() if sha not in reviewed}
