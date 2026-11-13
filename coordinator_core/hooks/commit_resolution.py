"""Per-commit staged-set and toplevel resolution shared by every git hook body.

One commit runs several hook processes; each used to spawn its own
`git diff --cached`. `staged_raw` runs ONE `git diff --cached --raw -z -M` per
(index identity, HEAD) and persists it under the git dir, so later hooks of the
same commit read a file instead of spawning. Imported on the commit hot path:
beyond the stdlib it needs only `coordinator_core.git.run`, so a by-file-path
loader must put the checkout root on sys.path first.

Invariants:
- The index is `$GIT_INDEX_FILE` when set, else `<git_dir>/index`. A pathspec
  commit hands every hook a temporary index; never assume `.git/index`.
- The cache key carries the index's realpath, mtime_ns, size, inode, trailing
  20-byte checksum and the HEAD oid (read with no spawn). A restaging hook or a
  moved branch changes the key, so a stale read is impossible. An unresolvable
  HEAD or unreadable index is a cache miss (live spawn, nothing written).
- One cache file per index, so concurrent commits on different temporary
  indexes never overwrite each other.
- Any cache read/parse failure falls back to one live spawn; only a failed live
  spawn raises `ResolutionError`.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import NamedTuple, Optional

from coordinator_core.git.run import run_git

_CACHE_PREFIX = "coordinator-commit-resolution-"
_CACHE_SUFFIX = ".json"
_SCHEMA = 1


class ResolutionError(RuntimeError):
    """The live git spawn failed (non-zero exit, timeout, or git not runnable)."""


class RawRecord(NamedTuple):
    """One `git diff --cached --raw -z -M` record. `status` is git's full status
    token (`M`, `A`, `D`, `R100`, ...); `orig_path` is set only for `R`/`C`,
    where `path` is the destination."""

    status: str
    src_mode: str
    dst_mode: str
    src_oid: str
    dst_oid: str
    path: str
    orig_path: Optional[str]


def _run_git(args: "list[str]", timeout: Optional[float]) -> bytes:
    res = run_git(["--no-optional-locks", *args], timeout=timeout, binary=True)
    if res.timed_out:
        raise ResolutionError(f"git {args[0]}: timed out")
    if res.returncode != 0:
        raise ResolutionError(f"git {args[0]} rc={res.returncode}")
    return res.stdout_bytes


def _index_path(git_dir: str) -> str:
    env = os.environ.get("GIT_INDEX_FILE", "").strip()
    return os.path.realpath(env if env else os.path.join(git_dir, "index"))


def _common_dir(git_dir: str) -> str:
    try:
        with open(os.path.join(git_dir, "commondir"), encoding="utf-8") as fh:
            rel = fh.read().strip()
        if rel:
            return os.path.normpath(os.path.join(git_dir, rel))
    except OSError:
        pass
    return git_dir


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _head_oid(git_dir: str) -> Optional[str]:
    """HEAD's commit oid with no spawn, or None when it cannot be resolved."""
    head = _read_text(os.path.join(git_dir, "HEAD"))
    if not head:
        return None
    if not head.startswith("ref:"):
        return head
    ref = head[len("ref:"):].strip()
    common = _common_dir(git_dir)
    for base in (git_dir, common):
        loose = _read_text(os.path.join(base, *ref.split("/")))
        if loose:
            return loose
    packed = os.path.join(common, "packed-refs")
    try:
        with open(packed, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if line.endswith(" " + ref) and not line.startswith(("#", "^")):
                    return line.split(" ", 1)[0]
    except OSError:
        pass
    return None


def _cache_key(git_dir: str) -> "Optional[tuple[list, str]]":
    """`(key, index_realpath)` for the current commit state, or None on a miss."""
    index = _index_path(git_dir)
    try:
        st = os.stat(index)
        with open(index, "rb") as fh:
            if st.st_size < 20:
                return None
            fh.seek(-20, os.SEEK_END)
            checksum = fh.read(20).hex()
    except OSError:
        return None
    head = _head_oid(git_dir)
    if head is None:
        return None
    key = [
        os.path.realpath(git_dir),
        index,
        st.st_mtime_ns,
        st.st_size,
        st.st_ino,
        checksum,
        head,
    ]
    return key, index


def _cache_path(git_dir: str, index: str) -> str:
    digest = hashlib.sha1(index.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    return os.path.join(git_dir, f"{_CACHE_PREFIX}{digest}{_CACHE_SUFFIX}")


def _load(path: str, key: list) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            entry = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(entry, dict) or entry.get("v") != _SCHEMA or entry.get("key") != key:
        return {}
    return entry


def _prune_stale(git_dir: str, keep: str) -> None:
    try:
        names = os.listdir(git_dir)
    except OSError:
        return
    for name in names:
        if not (name.startswith(_CACHE_PREFIX) and name.endswith(_CACHE_SUFFIX)):
            continue
        full = os.path.join(git_dir, name)
        if full == keep:
            continue
        try:
            with open(full, encoding="utf-8") as fh:
                recorded = json.load(fh).get("index")
            if isinstance(recorded, str) and not os.path.exists(recorded):
                os.unlink(full)
        except (OSError, ValueError, AttributeError):
            continue


def _save(git_dir: str, path: str, key: list, index: str, entry: dict) -> None:
    payload = dict(entry, v=_SCHEMA, key=key, index=index)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return
    _prune_stale(git_dir, path)


def _parse(raw: str) -> "list[RawRecord]":
    fields = [f for f in raw.split("\x00") if f != ""]
    records: "list[RawRecord]" = []
    i = 0
    while i < len(fields):
        header = fields[i]
        i += 1
        if not header.startswith(":"):
            continue
        parts = header[1:].split(" ")
        if len(parts) < 5 or i >= len(fields):
            continue
        src_mode, dst_mode, src_oid, dst_oid, status = parts[:5]
        path = fields[i]
        i += 1
        orig = None
        if status[:1] in ("R", "C") and i < len(fields):
            orig, path = path, fields[i]
            i += 1
        records.append(RawRecord(status, src_mode, dst_mode, src_oid, dst_oid, path, orig))
    return records


def raw_text(git_dir: str, timeout: Optional[float] = None) -> str:
    """The `git diff --cached --raw -z -M` stdout for this commit, spawning at
    most once per (index identity, HEAD). Raises `ResolutionError` if the live
    spawn fails."""
    keyed = _cache_key(git_dir)
    if keyed is not None:
        key, index = keyed
        path = _cache_path(git_dir, index)
        cached = _load(path, key).get("raw")
        if isinstance(cached, str):
            return cached
    out = _run_git(["diff", "--cached", "--raw", "-z", "-M"], timeout)
    text = out.decode("utf-8", errors="surrogateescape")
    if keyed is not None:
        entry = _load(path, key)
        entry["raw"] = text
        _save(git_dir, path, key, index, entry)
    return text


def staged_raw(git_dir: str, timeout: Optional[float] = None) -> "list[RawRecord]":
    """The staged set as parsed raw records (rename detection on)."""
    return _parse(raw_text(git_dir, timeout))


def name_status(git_dir: str, timeout: Optional[float] = None) -> "list[tuple[str, str]]":
    """Rename-expanded `(status_letter, path)` rows: an `R` yields `D` for the
    old path and `A` for the new one, a `C` yields `A` for the copy."""
    rows: "list[tuple[str, str]]" = []
    for rec in staged_raw(git_dir, timeout):
        letter = rec.status[:1]
        if letter == "R" and rec.orig_path is not None:
            rows.append(("D", rec.orig_path))
            rows.append(("A", rec.path))
        elif letter == "C":
            rows.append(("A", rec.path))
        else:
            rows.append((letter, rec.path))
    return rows


def names(git_dir: str, timeout: Optional[float] = None) -> "list[str]":
    """Every staged path (an `R` contributes both its paths), in `-M` raw-record
    order, an `R` as old path then new path."""
    return [path for _status, path in name_status(git_dir, timeout)]


def names_filtered(
    git_dir: str, letters: str, timeout: Optional[float] = None
) -> "list[str]":
    """Destination paths of raw records whose status letter is in `letters`
    (`git diff --diff-filter=<letters>` with `-M` on: `"AMR"` includes renamed
    destinations)."""
    return [
        rec.path for rec in staged_raw(git_dir, timeout) if rec.status[:1] in letters
    ]


def toplevel(git_dir: str, timeout: Optional[float] = None) -> str:
    """The worktree root. Zero spawns where `git_dir` is `<top>/.git` and no
    `GIT_WORK_TREE` is set; otherwise one `rev-parse --show-toplevel`, cached
    with the staged set. Raises `ResolutionError` if that spawn fails."""
    real = os.path.realpath(git_dir)
    if os.path.basename(real) == ".git" and not os.environ.get("GIT_WORK_TREE"):
        return os.path.dirname(real)
    keyed = _cache_key(git_dir)
    entry: dict = {}
    if keyed is not None:
        key, index = keyed
        path = _cache_path(git_dir, index)
        entry = _load(path, key)
        cached = entry.get("toplevel")
        if isinstance(cached, str) and cached:
            return cached
    out = _run_git(["rev-parse", "--show-toplevel"], timeout)
    top = out.decode("utf-8", errors="surrogateescape").strip()
    if keyed is not None and top:
        entry["toplevel"] = top
        _save(git_dir, path, key, index, entry)
    return top
