"""Own-hunk staging: build the blob that carries only a run's hunks of a shared path.

`land_run` commits through the in-process `git.commit.commit_paths`, which has no scoped index, so
staging is a blob: HEAD bytes with only the run's hunks applied, written with
`git_objects.write_object` and handed over as `commit_paths(supplied_blobs={path: sha})`. No git
spawn, no index write. Hunks are in HEAD (old-side) coordinates, `git diff -U0` convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from coordinator_core.git import commit as _commit
from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_state import head_blobs
from coordinator_core.ops.dispatch_emit.run_hunk_claims import Hunk, changed_opcodes


class OwnHunkRefused(Exception):
    """Own-hunk staging cannot proceed safely; the caller holds the path whole."""


@dataclass(frozen=True)
class PathDiff:
    """One shared path: its HEAD blob (`head_sha`, `head_bytes`) and the hunks of the worktree
    against it. `head_sha` is the AC8 token the caller re-checks before `commit_paths`."""

    head_sha: str
    head_bytes: bytes
    hunks: tuple[Hunk, ...]


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _worktree_lines(root: Path, path: str) -> list[bytes]:
    try:
        data = (Path(root) / path).read_bytes()
        return _commit._checkin_bytes(Path(root), path, data).splitlines(keepends=True)
    except (OSError, _commit.FilterUnsupported) as exc:
        raise OwnHunkRefused(f"{path}: worktree bytes unreadable as a checkin blob ({exc})") from exc


def _hunks_of(path: str, head_lines: list[bytes], new_lines: list[bytes]) -> list[Hunk]:
    out: list[Hunk] = []
    for _tag, i1, i2, j1, j2 in changed_opcodes(head_lines, new_lines):
        old_len, new_len = i2 - i1, j2 - j1
        out.append(
            Hunk(
                path,
                i1 if old_len == 0 else i1 + 1,
                old_len,
                j1 if new_len == 0 else j1 + 1,
                new_len,
            )
        )
    return out


def read_diff(root: Path, paths: Iterable[str]) -> dict[str, PathDiff]:
    """`{path: PathDiff}` for each of `paths` tracked in HEAD. Zero spawn: HEAD blob sha from the
    in-process tree walk, bytes via `git_objects.read_object`, worktree bytes from disk (checkin
    normalised), hunks by `difflib`. A path absent from HEAD is omitted (no HEAD side to split).
    Raises `OwnHunkRefused` on an unreadable HEAD blob or worktree file."""
    root = Path(root)
    wanted = [_norm(p) for p in paths]
    blobs = head_blobs(root, wanted)
    common = resolve_git_common_dir(root)
    out: dict[str, PathDiff] = {}
    for path in wanted:
        entry = blobs.get(path)
        if entry is None:
            continue
        sha = entry[1]
        got = git_objects.read_object(common, sha)
        if got is None or got[0] != "blob":
            raise OwnHunkRefused(f"{path}: HEAD blob {sha} unreadable")
        head_bytes = got[1]
        hunks = _hunks_of(path, head_bytes.splitlines(keepends=True), _worktree_lines(root, path))
        out[path] = PathDiff(sha, head_bytes, tuple(hunks))
    return out


def own_blob(root: Path, path: str, head_bytes: bytes, hunks: Iterable[Hunk]) -> str:
    """Blob sha of `head_bytes` with only `hunks` applied (worktree side taken from disk), written
    to the object store. Every hunk must be one the worktree currently differs by, else
    `OwnHunkRefused` (a stale hunk list is never guessed at). Other hunks stay out of the blob."""
    root = Path(root)
    path = _norm(path)
    head_lines = head_bytes.splitlines(keepends=True)
    new_lines = _worktree_lines(root, path)
    actual = {(h.old_start, h.old_len, h.new_start, h.new_len) for h in _hunks_of(path, head_lines, new_lines)}
    chosen = sorted({(h.old_start, h.old_len, h.new_start, h.new_len) for h in hunks})
    for h in chosen:
        if h not in actual:
            raise OwnHunkRefused(f"{path}: hunk {h} is not a current difference from HEAD")
    out: list[bytes] = []
    pos = 0
    for old_start, old_len, new_start, new_len in chosen:
        i1 = old_start if old_len == 0 else old_start - 1
        if i1 < pos:
            raise OwnHunkRefused(f"{path}: overlapping hunks")
        out.extend(head_lines[pos:i1])
        if new_len:
            j1 = new_start - 1
            out.extend(new_lines[j1 : j1 + new_len])
        pos = i1 + old_len
    out.extend(head_lines[pos:])
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", b"".join(out))


def check_head_unchanged(root: Path, diffs: dict[str, PathDiff]) -> Optional[str]:
    """None when every path's HEAD blob still equals the one `read_diff` saw; else the first
    path that moved. Call immediately before `commit_paths` and refuse on a path (AC8)."""
    now = head_blobs(Path(root), list(diffs))
    for path, d in diffs.items():
        entry = now.get(path)
        if entry is None or entry[1] != d.head_sha:
            return path
    return None
