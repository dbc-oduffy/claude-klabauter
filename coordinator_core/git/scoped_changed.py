"""coordinator_core.git.scoped_changed -- the in-process answer to "which
tracked paths in this scope differ from HEAD, and which untracked paths are
present", without spawning `git status`.

`scoped_changed(root, literals, roots, glob_dirs)` returns
`(changed, untracked_candidates)` or `None`. `None` means "a shape this
module will not guess at": the caller spawns `git status` for the WHOLE
call, never a mix. The error direction is always `None`.

The scope mirrors the `git status` pathspecs a snapshot passes:
  - `literals`: exact paths (`:(literal)p`);
  - `roots`: directories reported recursively;
  - `glob_dirs`: directories reported one level (`:(glob)d/*`); `""` is the
    repository root. Ignored SUBDIRECTORIES at any depth under a glob dir
    are reported by git, so they are surfaced as directory candidates.

`changed` holds only TRACKED paths that differ: index-vs-HEAD (staged add,
modify, delete, mode change) or worktree-vs-index (stat mismatch settled by
`content_hash.content_matches_index_sha`, deletion). Untracked paths are
never in `changed`: ignore matching is the caller's, so they are returned as
`untracked_candidates`. A candidate ending in `/` is an untracked directory,
anything else an untracked file.

A tracked file whose size differs from a nonzero index size is `changed`
without hashing: git's `ie_modified` returns on a size change before any
content comparison, so a CRLF rewrite that grows the file reads modified even
under `core.autocrlf=true`.

Returns `None` for: an index that cannot be walked (v4, truncated, unmerged
entry in scope), assume-valid / skip-worktree / intent-to-add or a
symlink / gitlink entry in scope, a nested `.git`, a symlink or non-regular
worktree file in scope, a literal naming a directory, an executable-bit
disagreement between index and worktree (core.filemode is not read), a case
-only disagreement between worktree and index names, an unreadable HEAD, a
racily-clean entry (entry mtime not older than the index file), a stat
mismatch `content_hash` declines to settle, and a directory walk beyond
`max_dirs`.

Negative-spec: no process-lifetime cache; no `git` spawn; no second index
parser (`git_index.parse_index_identity` does the walk).
"""

from __future__ import annotations

import os
import stat as _stat
import sys
from pathlib import Path
from typing import Callable, Dict, FrozenSet, List, NamedTuple, Optional, Sequence, Set, Tuple

from coordinator_core.git import content_hash
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.git_index import (
    IndexFlaggedIdentity,
    IndexParseError,
    parse_index_identity,
)
from coordinator_core.git.git_objects import _read_object
from coordinator_core.git.git_state import _parse_tree_entries, read_tree_spine

__all__ = ["ScopedChange", "scoped_changed", "scoped_changed_detail"]

_TREE = 0o40000
_GITLINK = 0o160000
_SYMLINK = 0o120000
_REGULAR = (0o100644, 0o100755)
DEFAULT_MAX_DIRS = 1000


class ScopedChange(NamedTuple):
    """`changed`: tracked paths differing from HEAD or the index.
    `candidates`: untracked files, and untracked directories ending in `/`.
    `tracked_dirs`: every directory holding a loaded index entry, so the
    caller can refuse an ignored directory that holds tracked files."""

    changed: Set[str]
    candidates: List[str]
    tracked_dirs: FrozenSet[str]


class _Gap(Exception):
    pass


def _clean_dir(p: str, *, allow_root: bool) -> str:
    p = p.rstrip("/")
    if not p:
        if allow_root:
            return ""
        raise _Gap("empty path")
    _check_path(p)
    return p


def _check_path(p: str) -> None:
    if p.startswith("/") or "\\" in p or (len(p) > 1 and p[1] == ":"):
        raise _Gap(f"non-relative path {p!r}")
    for part in p.split("/"):
        if part in ("", ".", "..", ".git"):
            raise _Gap(f"unsupported path component in {p!r}")


def _join(d: str, name: str) -> str:
    return f"{d}/{name}" if d else name


def _head_scope(
    root: Path, literals: Sequence[str], roots: Sequence[str], glob_dirs: Sequence[str]
) -> Dict[str, Tuple[int, str]]:
    spine_paths = list(literals)
    spine_paths += [_join(g, "x") for g in glob_dirs]
    spine_paths += [_join(r, "x") for r in roots]
    spine = read_tree_spine(root, spine_paths)
    if spine is None:
        raise _Gap("HEAD unreadable")
    out: Dict[str, Tuple[int, str]] = {}
    for lit in literals:
        d, _, leaf = lit.rpartition("/")
        e = spine.get(d, {}).get(leaf)
        if e is None:
            continue
        if e[0] == _TREE:
            raise _Gap("literal is a directory in HEAD")
        out[lit] = e
    for g in glob_dirs:
        for name, (mode, sha) in spine.get(g, {}).items():
            if mode != _TREE:
                out[_join(g, name)] = (mode, sha)
    common = resolve_git_common_dir(root)
    for r in roots:
        d, _, leaf = r.rpartition("/")
        e = spine.get(d, {}).get(leaf)
        if e is None:
            continue
        if e[0] != _TREE:
            raise _Gap("root is a file in HEAD")
        stack = [(r, e[1])]
        while stack:
            prefix, sha = stack.pop()
            obj = _read_object(common, sha)
            if obj is None or obj[0] != "tree":
                raise _Gap("HEAD tree unreadable")
            entries = _parse_tree_entries(obj[1])
            if entries is None:
                raise _Gap("HEAD tree corrupt")
            for name, (mode, esha) in entries.items():
                path = f"{prefix}/{name}"
                if mode == _TREE:
                    stack.append((path, esha))
                else:
                    out[path] = (mode, esha)
    for mode, _ in out.values():
        if mode in (_GITLINK, _SYMLINK):
            raise _Gap("symlink or gitlink in HEAD scope")
    return out


def scoped_changed_detail(
    root,
    literals: Sequence[str],
    roots: Sequence[str],
    glob_dirs: Sequence[str],
    *,
    extra_prefixes: Sequence[str] = (),
    descend: Optional[Callable[[str], bool]] = None,
    max_dirs: int = DEFAULT_MAX_DIRS,
) -> Optional[ScopedChange]:
    """`scoped_changed` plus the tracked-directory set, or `None` on a gap.

    `extra_prefixes` loads index entries under those directories into
    `tracked_dirs` without scoping them (the caller's ignored-directory
    tops). `descend(rel_dir)` is asked before the walk enters an UNTRACKED
    directory; `False` emits the `dir/` candidate and does not enter it, so
    an ignored directory is never enumerated. Tracked directories are always
    entered."""
    try:
        return _scoped(
            Path(root), literals, roots, glob_dirs, extra_prefixes, descend, max_dirs
        )
    except (_Gap, IndexParseError, OSError):
        return None


def scoped_changed(
    root,
    literals: Sequence[str],
    roots: Sequence[str],
    glob_dirs: Sequence[str],
) -> Optional[Tuple[Set[str], List[str]]]:
    """`(changed, untracked_candidates)` for the scope, or `None` when any
    gap is hit (the caller spawns `git status`)."""
    res = scoped_changed_detail(root, literals, roots, glob_dirs)
    return None if res is None else (res.changed, res.candidates)


def _scoped(
    root: Path,
    literals_in: Sequence[str],
    roots_in: Sequence[str],
    globs_in: Sequence[str],
    extra_prefixes: Sequence[str],
    descend: Optional[Callable[[str], bool]],
    max_dirs: int,
) -> ScopedChange:
    literals = list(dict.fromkeys(literals_in))
    for lit in literals:
        _check_path(lit)
    roots = [_clean_dir(r, allow_root=False) for r in dict.fromkeys(roots_in)]
    globs = [_clean_dir(g, allow_root=True) for g in dict.fromkeys(globs_in)]
    extras = [_clean_dir(x, allow_root=False) for x in extra_prefixes]

    roots_slash = tuple(r + "/" for r in roots)
    literal_set = set(literals)
    glob_set = set(globs)
    # A glob dir inside a root adds nothing the recursive root walk lacks.
    walk_globs = [g for g in globs if not (g and (g in roots or g.startswith(roots_slash)))]

    def in_scope(p: str) -> bool:
        return p in literal_set or p.startswith(roots_slash) or p.rpartition("/")[0] in glob_set

    index_path = resolve_git_dir(root) / "index"
    index_mtime_ns = index_path.stat().st_mtime_ns
    prefixes = roots + globs + extras + [lit for lit in literals if not lit.startswith(roots_slash)]
    entries = parse_index_identity(root, prefixes=prefixes, with_flags=True)

    scoped: Dict[str, IndexFlaggedIdentity] = {p: e for p, e in entries.items() if in_scope(p)}
    literal_dirs = tuple(lit + "/" for lit in literals if not lit.startswith(roots_slash))
    for p, e in entries.items():
        if p in scoped:
            continue
        if literal_dirs and p.startswith(literal_dirs):
            raise _Gap("literal is a directory in the index")
    for e in scoped.values():
        if e.assume_valid or e.skip_worktree or e.intent_to_add:
            raise _Gap("assume-valid, skip-worktree or intent-to-add entry")
        if e.mode not in _REGULAR:
            raise _Gap("symlink, gitlink or special-mode entry")

    tracked_dirs: Set[str] = set()
    for p in entries:
        q = p
        while "/" in q:
            q = q.rpartition("/")[0]
            if q in tracked_dirs:
                break
            tracked_dirs.add(q)

    head = _head_scope(root, literals, roots, globs)
    changed: Set[str] = set()
    for p, e in scoped.items():
        h = head.get(p)
        if h is None or h != (e.mode, e.sha):
            changed.add(p)
    for p in head:
        if p not in scoped:
            changed.add(p)

    candidates: Dict[str, None] = {}
    seen: Dict[str, os.stat_result] = {}
    lower_files: Optional[Set[str]] = None
    lower_dirs: Optional[Set[str]] = None
    budget = [max_dirs]

    def untracked_file(rel: str) -> None:
        nonlocal lower_files
        if lower_files is None:
            lower_files = {p.lower() for p in entries}
        if rel.lower() in lower_files:
            raise _Gap("case-only name disagreement with the index")
        candidates[rel] = None

    def untracked_dir(rel: str) -> None:
        nonlocal lower_dirs
        if lower_dirs is None:
            lower_dirs = {d.lower() for d in tracked_dirs}
        if rel.lower() in lower_dirs:
            raise _Gap("case-only directory disagreement with the index")
        candidates[rel + "/"] = None

    def scan(d: str):
        budget[0] -= 1
        if budget[0] < 0:
            raise _Gap("directory walk budget")
        try:
            return list(os.scandir(root / d if d else root))
        except FileNotFoundError:
            return []

    def classify(de: "os.DirEntry", rel: str, d: str) -> Optional[bool]:
        """True directory, False file; raises on shapes that are gaps."""
        if de.name == ".git":
            if d == "":
                return None
            raise _Gap("nested .git")
        if de.is_symlink() or getattr(de, "is_junction", lambda: False)():
            raise _Gap("symlink or junction in the worktree")
        return de.is_dir(follow_symlinks=False)

    def walk(start: str, files_too: bool) -> None:
        stack = [start]
        while stack:
            d = stack.pop()
            for de in scan(d):
                rel = _join(d, de.name)
                kind = classify(de, rel, d)
                if kind is None:
                    continue
                if kind:
                    if rel in tracked_dirs:
                        stack.append(rel)
                        continue
                    untracked_dir(rel)
                    if descend is None or descend(rel):
                        stack.append(rel)
                elif rel in entries:
                    if rel in scoped:
                        seen[rel] = de.stat(follow_symlinks=False)
                elif files_too:
                    untracked_file(rel)

    for r in roots:
        walk(r, True)
    for g in walk_globs:
        for de in scan(g):
            rel = _join(g, de.name)
            kind = classify(de, rel, g)
            if kind is None:
                continue
            if kind:
                if rel in tracked_dirs:
                    walk(rel, False)
                    continue
                untracked_dir(rel)
                if descend is None or descend(rel):
                    walk(rel, False)
            elif rel in entries:
                if rel in scoped:
                    seen[rel] = de.stat(follow_symlinks=False)
            else:
                untracked_file(rel)

    for lit in literals:
        if lit in scoped:
            continue
        try:
            st = os.lstat(root / lit)
        except FileNotFoundError:
            continue
        if _stat.S_ISDIR(st.st_mode):
            raise _Gap("literal is a directory in the worktree")
        if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
            raise _Gap("literal is a symlink or special file")
        untracked_file(lit)

    for p, e in scoped.items():
        st = seen.get(p)
        if st is None:
            try:
                st = os.lstat(root / p)
            except (FileNotFoundError, NotADirectoryError):
                changed.add(p)
                continue
        if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
            raise _Gap("symlink or non-regular worktree file")
        if sys.platform == "win32":
            if e.mode & 0o111:
                raise _Gap("executable bit unreadable on Windows; core.filemode is not read")
        elif bool(e.mode & 0o111) != bool(st.st_mode & 0o111):
            raise _Gap("executable bit disagrees; core.filemode is not read")
        if e.size != (st.st_size & 0xFFFFFFFF) and e.size != 0:
            changed.add(p)
            continue
        mtime_ns = st.st_mtime_ns
        stat_match = e.mtime == mtime_ns // 1_000_000_000
        if stat_match and e.mtime_nsec:
            stat_match = e.mtime_nsec == mtime_ns % 1_000_000_000
        if stat_match and e.size == (st.st_size & 0xFFFFFFFF):
            if e.mtime * 1_000_000_000 + e.mtime_nsec >= index_mtime_ns:
                raise _Gap("racily clean entry")
            continue
        verdict = content_hash.content_matches_index_sha(root, p, e.sha)
        if verdict is None:
            raise _Gap("content_hash declined")
        if not verdict:
            changed.add(p)

    return ScopedChange(changed, list(candidates), frozenset(tracked_dirs))
