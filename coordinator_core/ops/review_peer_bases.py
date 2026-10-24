"""
coordinator_core.ops.review_peer_bases — ``peer_path_bases``: per declared path, the newest peer
commit in ``base..head`` that precedes every own commit on that path.

A commit is a peer commit when it carries a ``Deliverable-Id`` other than ``own_deliverable`` or a
``Session-Id`` other than ``own_session``; a commit with neither trailer is own. Diffing a path from
its mapped sha leaves out the peer hunks and keeps the run's own. A path with a peer commit after an
own commit is omitted: it cannot be split without hunk surgery and keeps the run base.

Keys are changed file names, not declared paths: a declared directory is never itself a key. The
whole-diff freeze expands a directory to its keyed files (``review_prep._specs_by_base``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.git.literal_pathspec import git_pathspec
from coordinator_core.git.run import run_git

_GIT_TIMEOUT_SECS = 5.0
_COMMIT_MARK = "\x1e"
_FIELD_SEP = "\x1f"
_FORMAT = (
    f"--format={_COMMIT_MARK}%H{_FIELD_SEP}"
    f"%(trailers:key=Session-Id,valueonly=true,unfold=true,separator=%x1d){_FIELD_SEP}"
    f"%(trailers:key=Deliverable-Id,valueonly=true,unfold=true,separator=%x1d)"
)


def _is_peer(sessions: List[str], deliverables: List[str], own_session: str, own_deliverable: Optional[str]) -> bool:
    if any(s != own_session for s in sessions):
        return True
    return own_deliverable is not None and any(d != own_deliverable for d in deliverables)


def _values(field: str) -> List[str]:
    return [v.strip() for v in field.split("\x1d") if v.strip()]


def _has_object_store(repo_root: Path) -> bool:
    """False when ``repo_root`` holds no git object store, where ``git log`` can only fail."""
    dot_git = repo_root / ".git"
    return dot_git.is_file() or (dot_git / "objects").is_dir()


def peer_path_bases(
    repo_root: Path,
    base: str,
    head: Optional[str],
    paths: List[str],
    own_session: str,
    own_deliverable: Optional[str] = None,
) -> Dict[str, str]:
    """Declared ``paths`` -> sha of the newest peer commit in ``base..(head or HEAD)`` touching
    the path, only for paths where no own commit precedes it. One ``git log`` spawn; none when
    ``paths`` is empty. A git failure returns ``{}`` (every path keeps the run base)."""
    if not paths or not _has_object_store(repo_root):
        return {}
    proc = run_git(
        [
            "-C", str(repo_root), "log", "--reverse", "--name-only", _FORMAT,
            f"{base}..{head or 'HEAD'}", "--", *map(git_pathspec, paths),
        ],
        timeout=_GIT_TIMEOUT_SECS,
    )
    if proc.returncode != 0:
        return {}
    bases: Dict[str, str] = {}
    own_seen: set = set()
    for chunk in proc.stdout.split(_COMMIT_MARK):
        header, _, files = chunk.partition("\n")
        parts = header.split(_FIELD_SEP)
        if len(parts) != 3 or not parts[0].strip():
            continue
        sha = parts[0].strip()
        peer = _is_peer(_values(parts[1]), _values(parts[2]), own_session, own_deliverable)
        for name in files.splitlines():
            name = name.strip()
            if not name or name in own_seen:
                continue
            if peer:
                bases[name] = sha
            else:
                own_seen.add(name)
    return bases
