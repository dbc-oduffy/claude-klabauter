"""coordinator_core.git.git_objects -- loose object writer, tree-spine
reader, and a real locked ref CAS.

Purpose: the in-process replacement for `git hash-object -w --stdin`
(tree/commit objects and in-memory-authored blobs only -- see the
Negative-spec below), `git write-tree`'s bottom-up tree emission, and
`git update-ref`'s lockfile CAS + reflog append, so a commit built from
`.git/index` state can be written with ZERO git subprocesses. Spike this
ports from: `state/audits/2026-08-22-zero-spawn-commit-spike.py`.

This module sits on `coordinator_core.ops.ipc`'s cold-start path (see
`coordinator_core/git/run.py`'s IMPORT COST section) -- it imports only
`hashlib`/`mmap`/`os`/`re`/`struct`/`zlib`/`pathlib`, never `subprocess` and
never `coordinator_core.ops`. A caller that needs the shared packer
(argv chunking, `_NO_CONSOLE`, ...) imports it function-locally at the
one call site that needs it -- mirrors `git_state.head_blobs`'s
precedent for the same cold-start reason.

Negative-spec:
    - Do NOT call `write_object` for a blob whose bytes were read off the
      worktree, or for `commit_authored_content`'s own path-attributed
      blob -- neither has gone through git's clean pipeline
      (`filter.*.clean`/`text`/`eol=`/`core.autocrlf`), and `write_object`
      writes bytes verbatim. Those go through `git hash-object -w`
      instead (`--stdin-paths` / `--path=`) -- see C4/C8a.
    - Do NOT reintroduce `_OBJECT_CACHE` as an unbounded, ref/path/index
      -keyed, process-lifetime dict. This module lives in a warm,
      long-running engine (unlike `pickup_assemble`'s one-process-per-
      `brief()`-call lifetime) -- a pack file list, a parsed index, or a
      decoded pack-object-at-offset can all go stale under a live `git
      gc`/incoming fetch while this process keeps running. Only a sha
      (content-addressed, cannot change under its own key) is memoized
      here, and even that is capped (`_OBJECT_CACHE_MAX_ENTRIES`).

Spec backlink: docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md, chunk C2
"""
from __future__ import annotations

import hashlib
import mmap
import os
import struct
import time
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Callable, NamedTuple, Optional


#: DESTINATION is open in another process -- a reader, an indexer, a peer's
_REPLACE_RETRY_DELAYS_S = (0.002, 0.005, 0.01, 0.02, 0.05, 0.1)


#: The index is REPLACED, never edited in place, so a failed read or stat is a
_TRANSIENT_READ_RETRY_DELAYS_S = (0.002, 0.005, 0.01, 0.02, 0.05)


def _retry_transient_read(op: "Callable[[], object]") -> object:
    last: Optional[OSError] = None
    for delay in (None, *_TRANSIENT_READ_RETRY_DELAYS_S):
        if delay is not None:
            time.sleep(delay)
        try:
            return op()
        except FileNotFoundError:
            raise
        except OSError as exc:
            last = exc
    assert last is not None
    raise last


def _replace_with_retry(
    src: Path,
    dst: Path,
    *,
    still_valid: Optional[Callable[[], bool]] = None,
) -> bool:
    for delay in (None, *_REPLACE_RETRY_DELAYS_S):
        if delay is not None:
            time.sleep(delay)
            if still_valid is not None and not still_valid():
                return False
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            continue
        except OSError:
            break
    return False


def _obj_path(gitdir: Path, sha: str) -> Path:
    return Path(gitdir, "objects", sha[:2], sha[2:])


def write_object(gitdir: Path, kind: bytes, payload: bytes) -> str:
    body = kind + b" " + str(len(payload)).encode("ascii") + b"\x00" + payload
    sha = hashlib.sha1(body).hexdigest()
    path = _obj_path(gitdir, sha)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_bytes(zlib.compress(body))
        if not _replace_with_retry(tmp, path):
            # CONTENT-ADDRESSED, so a lost race is not a lost write. `path` is
            if not path.exists():
                raise OSError(
                    f"write_object: could not place {sha} at {path} -- the "
                    f"destination stayed locked and no peer wrote it"
                )
            try:
                tmp.unlink()
            except OSError:
                pass
    return sha


def build_tree(gitdir: Path, entries: dict) -> str:
    tree: dict = {}
    for path, (mode, sha) in entries.items():
        node = tree
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, sha)

    def emit(node: dict) -> str:
        items = []
        for name, val in node.items():
            if isinstance(val, dict):
                items.append((name + "/", name, b"40000", emit(val)))
            else:
                mode, sha = val
                items.append((name, name, oct(mode)[2:].encode("ascii"), sha))
        items.sort(key=lambda t: t[0])
        buf = b"".join(
            mode_bytes + b" " + name.encode("utf-8") + b"\x00" + bytes.fromhex(sha)
            for _, name, mode_bytes, sha in items
        )
        return write_object(gitdir, b"tree", buf)

    return emit(tree)


class _GitReadModelError(Exception):
    pass


_PACK_TYPE_NAMES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_PACK_TYPE_NUMS = {v: k for k, v in _PACK_TYPE_NAMES.items()}

_MAX_DELTA_DEPTH = 200


class _PackIndex(NamedTuple):
    pack_path: Path
    fanout: tuple
    shas: bytes
    offsets: tuple


def _read_loose_object(common_dir: Path, sha: str) -> Optional[tuple[str, bytes]]:
    path = common_dir / "objects" / sha[:2] / sha[2:]
    if not path.is_file():
        return None
    try:
        raw = zlib.decompress(path.read_bytes())
    except (OSError, zlib.error):
        return None
    header, _, payload = raw.partition(b"\x00")
    otype, _, _ = header.partition(b" ")
    return otype.decode("ascii", errors="replace"), payload


_PACK_LISTING_CACHE: "dict[str, tuple[Optional[tuple[int, int]], list[tuple[Path, Path]]]]" = {}


def _pack_dir_generation(pack_dir: Path) -> Optional[tuple[int, int]]:
    try:
        st = pack_dir.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _iter_pack_files(common_dir: Path, *, revalidate: bool = True) -> list[tuple[Path, Path]]:
    """Lists `(idx_path, pack_path)` pairs under `objects/pack/`, cached per
    `common_dir` -- see `_PACK_LISTING_CACHE` above for why that is safe in
    the warm long-running engine this module sits in.

    `revalidate=True`, the default and what every caller outside this module
    gets, stats the pack directory and rebuilds when the pack set has
    changed, so no caller ever sees a stale pack set. A caller whose answer
    cannot be changed by a newly-arrived pack passes `revalidate=False` to
    skip even that one stat."""
    key = str(common_dir)
    pack_dir = common_dir / "objects" / "pack"
    cached = _PACK_LISTING_CACHE.get(key)
    if cached is not None:
        if not revalidate:
            return cached[1]
        generation = _pack_dir_generation(pack_dir)
        if cached[0] == generation:
            return cached[1]
    else:
        generation = _pack_dir_generation(pack_dir)
    result: list[tuple[Path, Path]] = []
    if generation is not None:
        for idx_path in sorted(pack_dir.glob("*.idx")):
            if idx_path.name.startswith("."):
                continue
            pack_path = idx_path.with_suffix(".pack")
            if pack_path.is_file():
                result.append((idx_path, pack_path))
    _PACK_LISTING_CACHE[key] = (generation, result)
    _PACK_INDEXES_BY_LISTING.pop(key, None)
    return result


_PACK_INDEXES_BY_LISTING: "dict[str, list[tuple[Path, Path, _PackIndex]]]" = {}


def _pack_indexes(
    common_dir: Path, *, revalidate: bool = True
) -> list[tuple[Path, Path, "_PackIndex"]]:
    key = str(common_dir)
    listing = _iter_pack_files(common_dir, revalidate=revalidate)
    cached = _PACK_INDEXES_BY_LISTING.get(key)
    if cached is not None:
        return cached
    result: list[tuple[Path, Path, _PackIndex]] = []
    for idx_path, pack_path in listing:
        pidx = _parse_pack_index(idx_path)
        if pidx is None:
            continue
        result.append((idx_path, pack_path, pidx))
    _PACK_INDEXES_BY_LISTING[key] = result
    return result


_PACK_INDEX_CACHE_MAX_ENTRIES = 4096
_PACK_INDEX_CACHE: "OrderedDict[tuple[str, int, int], Optional[_PackIndex]]" = OrderedDict()


def _parse_pack_index(idx_path: Path) -> Optional[_PackIndex]:
    """Parses a v2 pack `.idx`, memoized by `(path, st_mtime_ns, st_size)`
    -- see `_PACK_INDEX_CACHE` above for why this is safe against a live
    `git gc`/fetch despite the warm long-running engine this module sits
    in. A `stat()` failure (file vanished under us) skips the cache and
    falls through to the uncached parse, which then degrades to `None` in
    the usual way."""
    try:
        st = idx_path.stat()
    except OSError:
        return _parse_pack_index_uncached(idx_path)
    key = (str(idx_path), st.st_mtime_ns, st.st_size)
    if key in _PACK_INDEX_CACHE:
        _PACK_INDEX_CACHE.move_to_end(key)
        return _PACK_INDEX_CACHE[key]
    result = _parse_pack_index_uncached(idx_path)
    _PACK_INDEX_CACHE[key] = result
    _PACK_INDEX_CACHE.move_to_end(key)
    while len(_PACK_INDEX_CACHE) > _PACK_INDEX_CACHE_MAX_ENTRIES:
        _PACK_INDEX_CACHE.popitem(last=False)
    return result


def _parse_pack_index_uncached(idx_path: Path) -> Optional[_PackIndex]:
    try:
        data = idx_path.read_bytes()
    except OSError:
        data = b""
    if not (len(data) >= 8 and data[:4] == b"\xfftOc" and struct.unpack(">I", data[4:8])[0] == 2):
        return None
    pos = 8
    fanout = struct.unpack(">256I", data[pos : pos + 1024])
    pos += 1024
    count = fanout[255]
    shas = data[pos : pos + count * 20]
    pos += count * 20
    pos += count * 4
    offsets32 = struct.unpack(f">{count}I", data[pos : pos + count * 4])
    pos += count * 4
    large_count = sum(1 for o in offsets32 if o & 0x80000000)
    large_offsets = (
        struct.unpack(f">{large_count}Q", data[pos : pos + large_count * 8]) if large_count else ()
    )
    offsets = []
    for raw_offset in offsets32:
        if raw_offset & 0x80000000:
            offsets.append(large_offsets[raw_offset & 0x7FFFFFFF])
        else:
            offsets.append(raw_offset)
    return _PackIndex(idx_path.with_suffix(".pack"), fanout, shas, tuple(offsets))


def _pack_index_find(pidx: _PackIndex, sha_hex: str) -> Optional[int]:
    try:
        target = bytes.fromhex(sha_hex)
    except ValueError:
        return None
    if len(target) != 20:
        return None
    first_byte = target[0]
    lo = pidx.fanout[first_byte - 1] if first_byte > 0 else 0
    hi = pidx.fanout[first_byte]
    while lo < hi:
        mid = (lo + hi) // 2
        mid_sha = pidx.shas[mid * 20 : mid * 20 + 20]
        if mid_sha == target:
            return pidx.offsets[mid]
        if mid_sha < target:
            lo = mid + 1
        else:
            hi = mid
    return None


def _read_pack_bytes(pack_path: Path) -> bytes | mmap.mmap:
    fh = open(pack_path, "rb")
    try:
        return mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except ValueError:
        fh.seek(0)
        return fh.read()
    finally:
        fh.close()


def _delta_read_size(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not (byte & 0x80):
            break
    return result, pos


def _apply_git_delta(base: bytes, delta: bytes) -> bytes:
    pos = 0
    _base_size, pos = _delta_read_size(delta, pos)
    _result_size, pos = _delta_read_size(delta, pos)
    out = bytearray()
    n = len(delta)
    while pos < n:
        opcode = delta[pos]
        pos += 1
        if opcode & 0x80:
            copy_offset = 0
            copy_size = 0
            if opcode & 0x01:
                copy_offset |= delta[pos]
                pos += 1
            if opcode & 0x02:
                copy_offset |= delta[pos] << 8
                pos += 1
            if opcode & 0x04:
                copy_offset |= delta[pos] << 16
                pos += 1
            if opcode & 0x08:
                copy_offset |= delta[pos] << 24
                pos += 1
            if opcode & 0x10:
                copy_size |= delta[pos]
                pos += 1
            if opcode & 0x20:
                copy_size |= delta[pos] << 8
                pos += 1
            if opcode & 0x40:
                copy_size |= delta[pos] << 16
                pos += 1
            if copy_size == 0:
                copy_size = 0x10000
            out += base[copy_offset : copy_offset + copy_size]
        elif opcode != 0:
            out += delta[pos : pos + opcode]
            pos += opcode
        else:
            raise _GitReadModelError("invalid pack delta opcode 0")
    return bytes(out)


def _zlib_decompress_bounded(pack_bytes: bytes, pos: int, size_hint: int) -> bytes:
    mv = memoryview(pack_bytes)
    n = len(pack_bytes)
    d = zlib.decompressobj()
    out = bytearray()
    cur = pos
    window = max(64, size_hint + 64) if size_hint else 4096
    while True:
        end = min(cur + window, n)
        if end <= cur:
            raise _GitReadModelError(f"truncated zlib stream in pack object at offset {pos}")
        out += d.decompress(mv[cur:end])
        cur = end
        if d.eof:
            return bytes(out)
        window *= 2


def _read_pack_object_at(
    common_dir: Path, pack_path: Path, pack_bytes: bytes, offset: int, _depth: int = 0
) -> tuple[int, bytes]:
    # Depth guard against a cyclic/over-deep OFS_DELTA chain (the direct
    # self-recursion below); REF_DELTA cycles route through `_read_object`
    if _depth > _MAX_DELTA_DEPTH:
        raise _GitReadModelError(f"delta chain exceeds max depth {_MAX_DELTA_DEPTH} (cyclic/corrupt pack?)")
    pos = offset
    first = pack_bytes[pos]
    pos += 1
    type_num = (first >> 4) & 0x7
    # for OFS_DELTA/REF_DELTA it's the uncompressed size of the delta STREAM
    usize = first & 0x0F
    shift = 4
    byte = first
    while byte & 0x80:
        byte = pack_bytes[pos]
        pos += 1
        usize |= (byte & 0x7F) << shift
        shift += 7

    if type_num == 6:  # OFS_DELTA
        byte = pack_bytes[pos]
        pos += 1
        base_rel_offset = byte & 0x7F
        while byte & 0x80:
            byte = pack_bytes[pos]
            pos += 1
            base_rel_offset += 1
            base_rel_offset = (base_rel_offset << 7) | (byte & 0x7F)
        base_offset = offset - base_rel_offset
        if base_offset < 0:
            raise _GitReadModelError(f"OFS_DELTA base offset underflow: {base_offset}")
        delta = _zlib_decompress_bounded(pack_bytes, pos, usize)
        base_type, base_content = _read_pack_object_at(common_dir, pack_path, pack_bytes, base_offset, _depth + 1)
        return base_type, _apply_git_delta(base_content, delta)

    if type_num == 7:  # REF_DELTA
        base_sha = pack_bytes[pos : pos + 20].hex()
        pos += 20
        delta = _zlib_decompress_bounded(pack_bytes, pos, usize)
        base = _read_object(common_dir, base_sha)
        if base is None:
            raise _GitReadModelError(f"REF_DELTA base object missing: {base_sha}")
        base_type_name, base_content = base
        base_type_num = _PACK_TYPE_NUMS.get(base_type_name)
        if base_type_num is None:
            raise _GitReadModelError(f"REF_DELTA base object has unsupported type: {base_type_name}")
        return base_type_num, _apply_git_delta(base_content, delta)

    content = _zlib_decompress_bounded(pack_bytes, pos, usize)
    return type_num, content


def _search_packs_for_sha(
    common_dir: Path, sha: str, *, revalidate: bool
) -> Optional[tuple[Optional[tuple[str, bytes]]]]:
    for _idx_path, pack_path, pidx in _pack_indexes(common_dir, revalidate=revalidate):
        offset = _pack_index_find(pidx, sha)
        if offset is None:
            continue
        try:
            pack_bytes = _read_pack_bytes(pack_path)
        except OSError:
            continue
        type_num, content = _read_pack_object_at(common_dir, pack_path, pack_bytes, offset)
        type_name = _PACK_TYPE_NAMES.get(type_num)
        if type_name is None:
            return (None,)
        return ((type_name, content),)
    return None


def _read_pack_object_by_sha(common_dir: Path, sha: str) -> Optional[tuple[str, bytes]]:
    found = _search_packs_for_sha(common_dir, sha, revalidate=False)
    if found is None:
        found = _search_packs_for_sha(common_dir, sha, revalidate=True)
    if found is None:
        return None
    return found[0]


_OBJECT_CACHE_MAX_ENTRIES = 4096
_OBJECT_CACHE: "OrderedDict[tuple[str, str], Optional[tuple[str, bytes]]]" = OrderedDict()
_CACHE_MISS = object()


def _object_cache_get(key: tuple[str, str]):
    if key in _OBJECT_CACHE:
        _OBJECT_CACHE.move_to_end(key)
        return _OBJECT_CACHE[key]
    return _CACHE_MISS


def _object_cache_put(key: tuple[str, str], value: Optional[tuple[str, bytes]]) -> None:
    _OBJECT_CACHE[key] = value
    _OBJECT_CACHE.move_to_end(key)
    while len(_OBJECT_CACHE) > _OBJECT_CACHE_MAX_ENTRIES:
        _OBJECT_CACHE.popitem(last=False)


def read_object(common_dir: Path, sha: str) -> Optional[tuple[str, bytes]]:
    return _read_object(common_dir, sha)


def _read_object(common_dir: Path, sha: str) -> Optional[tuple[str, bytes]]:
    """Reads a git object by full 40-hex sha -- packs first (v2 idx binary
    search across every pack in `objects/pack/`), loose second. Cached per
    (common_dir, sha), bounded (see `_OBJECT_CACHE` above)."""
    sha = sha.lower()
    key = (str(common_dir), sha)
    cached = _object_cache_get(key)
    if cached is not _CACHE_MISS:
        return cached
    try:
        result = _read_pack_object_by_sha(common_dir, sha)
    except (_GitReadModelError, zlib.error, struct.error):
        result = None
    if result is None:
        result = _read_loose_object(common_dir, sha)
    _object_cache_put(key, result)
    return result


def _read_ref_raw(ref_path: Path) -> Optional[str]:
    try:
        content = ref_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return content or None


def read_packed_ref(gitdir: Path, ref: str) -> Optional[str]:
    """`<sha>` for `ref` out of `<gitdir>/packed-refs`, or `None`.

    `packed-refs` is a flat text file, one ref per line, `<sha> SP <refname>`.
    Two line kinds are skipped: a leading `#` (the `# pack-refs with: ...`
    header) and a leading `^` (a tag's peeled-object line, which annotates the
    PRECEDING line rather than naming a ref of its own -- taking its sha would
    return the tagged commit under the tag's own name).

    This is the whole of the "pack-then-loose ref resolution" that
    `_resolve_cas_ref_target` previously declined to reproduce as too risky.
    It is a two-field split on a file git documents; the risk it was weighed
    against -- silently CAS-ing against the wrong file -- is not present,
    because the caller uses this only as the comparand when NO loose file
    exists, which is precisely when git itself reads the packed value.

    Deliberately NOT a general packed-refs parser: no `--sort`, no peel
    resolution, no `worktree/` per-worktree ref namespace. One exact-name
    lookup, which is all `cas_ref` needs.
    """
    try:
        text = (gitdir / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return sha.strip() or None
    return None


def _ref_exists_loose_or_packed(common_dir: Path, ref_rel: str) -> bool:
    return (common_dir / ref_rel).is_file() or read_packed_ref(common_dir, ref_rel) is not None


def _log_all_ref_updates(gitdir: Path) -> bool:
    config_path = gitdir / "config"
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    in_core = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_core = stripped.lower().startswith("[core]")
            continue
        if in_core and "=" in stripped:
            key, _, value = stripped.partition("=")
            if key.strip().lower() == "logallrefupdates":
                return value.strip().lower() not in ("false", "0", "no")
    return True


def append_reflog(gitdir: Path, ref: str, old: Optional[str], new: str, committer: str, message: str) -> None:
    if not _log_all_ref_updates(gitdir):
        return
    log_path = gitdir / "logs" / ref
    log_path.parent.mkdir(parents=True, exist_ok=True)
    old_hex = old if old else "0" * 40
    line = f"{old_hex} {new} {committer}\t{message}\n".encode("utf-8")
    with open(log_path, "ab") as fh:
        fh.write(line)


def _head_symref_target(head_gitdir: Path) -> Optional[str]:
    try:
        raw = (head_gitdir / "HEAD").read_bytes().decode("utf-8", "surrogateescape").strip()
    except OSError:
        return None
    if not raw.startswith("ref:"):
        return None
    return raw[4:].strip() or None


def cas_ref(
    gitdir: Path,
    ref: str,
    expected: Optional[str],
    new: str,
    *,
    reflog_committer: Optional[str] = None,
    reflog_message: Optional[str] = None,
    head_gitdir: Optional[Path] = None,
) -> bool:
    ref_path = gitdir / ref
    lock_path = gitdir / (ref + ".lock")
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except PermissionError:
        # WINDOWS SPELLS THE SAME LOSS DIFFERENTLY. `O_CREAT|O_EXCL` against a
        return False
    try:
        os.close(fd)
        current = _read_ref_raw(ref_path)
        if current is None:
            current = read_packed_ref(gitdir, ref)
        if current != expected:
            return False
        lock_path.write_bytes(new.encode("ascii") + b"\n")
        if reflog_committer is not None and reflog_message is not None:
            append_reflog(gitdir, ref, expected, new, reflog_committer, reflog_message)
            if head_gitdir is not None and ref != "HEAD":
                if _head_symref_target(head_gitdir) == ref:
                    append_reflog(
                        head_gitdir, "HEAD", expected, new, reflog_committer, reflog_message,
                    )
        def _expected_still_current() -> bool:
            now = _read_ref_raw(ref_path)
            if now is None:
                now = read_packed_ref(gitdir, ref)
            return now == expected

        return _replace_with_retry(
            lock_path, ref_path, still_valid=_expected_still_current
        )
    finally:
        if lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass
