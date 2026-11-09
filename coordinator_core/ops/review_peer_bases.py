"""
coordinator_core.ops.review_peer_bases — ``peer_path_bases``: per declared path, the newest peer
commit in ``base..head`` that precedes every own commit on that path.

A commit is a peer commit when it carries a ``Deliverable-Id`` other than ``own_deliverable`` or a
``Session-Id`` other than ``own_session``; a commit with neither trailer is own. Diffing a path from
its mapped sha leaves out the peer hunks and keeps the run's own. A path with a peer commit after an
own commit is omitted: it cannot be split without hunk surgery and keeps the run base.

Keys are changed file names, not declared paths: a declared directory is never itself a key. The
whole-diff freeze expands a directory to its keyed files (``review_prep._specs_by_base``).

The mapped sha is a stand-in for the path's content at its newest peer commit: on a linear log every
commit from that peer commit up to the commit before the path's first own commit holds the same
content, so keyed paths share the fewest such shas and the freeze and measure spawn once per
distinct sha, not once per peer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from coordinator_core.git.literal_pathspec import git_pathspec, is_magic_pathspec
from coordinator_core.git.run import run_git

_GIT_TIMEOUT_SECS = 5.0
_COMMIT_MARK = "\x1e"
_FIELD_SEP = "\x1f"
_FORMAT = (
    f"--format={_COMMIT_MARK}%H{_FIELD_SEP}"
    f"%(trailers:key=Session-Id,valueonly=true,unfold=true,separator=%x1d){_FIELD_SEP}"
    f"%(trailers:key=Deliverable-Id,valueonly=true,unfold=true,separator=%x1d){_FIELD_SEP}%P"
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


def _declared_matcher(paths: List[str]) -> Callable[[str], bool]:
    """Predicate: a changed file name is a declared file or sits under a declared directory.
    Replaces git's per-commit pathspec matching, which costs commits x pathspecs."""
    exact = set()
    prefixes = []
    for raw in paths:
        p = raw.strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        p = p.rstrip("/")
        if p in ("", "."):
            return lambda _name: True
        exact.add(p)
        prefixes.append(p + "/")
    under = tuple(prefixes)
    return lambda name: name in exact or name.startswith(under)


class PeerBases(Dict[str, str]):
    """``peer_path_bases`` result. ``changed`` is every declared-matching file name any commit in the
    range touched (own commits included), from the same log, so a caller needing that set spawns nothing."""

    changed: frozenset = frozenset()


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
    ``paths`` is empty. Declared paths are matched in Python, so git's cost is independent of
    their count; a glob or ``:`` magic entry falls back to git pathspecs. A git failure returns
    ``{}`` (every path keeps the run base)."""
    if not paths or not _has_object_store(repo_root):
        return {}
    in_python = not any(is_magic_pathspec(p) for p in paths)
    argv = [
        "-C", str(repo_root), "log", "--reverse", "--name-only", "--no-renames", _FORMAT,
        f"{base}..{head or 'HEAD'}",
    ]
    if not in_python:
        argv += ["--", *map(git_pathspec, paths)]
    proc = run_git(argv, timeout=_GIT_TIMEOUT_SECS)
    if proc.returncode != 0:
        return {}
    matches = _declared_matcher(paths) if in_python else (lambda _name: True)
    bases: Dict[str, str] = {}
    own_seen: Dict[str, int] = {}
    peer_at: Dict[str, int] = {}
    changed: set = set()
    order: List[str] = []
    linear = in_python
    for chunk in proc.stdout.split(_COMMIT_MARK):
        header, _, files = chunk.partition("\n")
        parts = header.split(_FIELD_SEP)
        if len(parts) != 4 or not parts[0].strip():
            continue
        sha = parts[0].strip()
        if order and parts[3].split() != [order[-1]]:
            linear = False
        order.append(sha)
        peer = _is_peer(_values(parts[1]), _values(parts[2]), own_session, own_deliverable)
        for name in files.splitlines():
            name = name.strip()
            if not name or not matches(name):
                continue
            changed.add(name)
            if name in own_seen:
                continue
            if peer:
                bases[name] = sha
                peer_at[name] = len(order) - 1
            else:
                own_seen[name] = len(order) - 1
    out = PeerBases(_collapse(bases, peer_at, own_seen, order) if linear else bases)
    out.changed = frozenset(changed)
    return out


def _collapse(bases: Dict[str, str], peer_at: Dict[str, int], own_seen: Dict[str, int], order: List[str]) -> Dict[str, str]:
    """Re-point each keyed path at a shared sha holding the same content (greedy interval stabbing:
    a path's span runs from its peer commit to the commit before its first own commit)."""
    spans = sorted(
        ((own_seen.get(name, len(order)) - 1, peer_at[name], name) for name in bases),
        key=lambda t: (t[0], t[1], t[2]),
    )
    out: Dict[str, str] = {}
    point = -1
    for right, left, name in spans:
        if point < left:
            point = right
        out[name] = order[point]
    return out
