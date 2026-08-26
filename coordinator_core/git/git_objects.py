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
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import NamedTuple, Optional


# ---------------------------------------------------------------------------
# Write side: loose objects + tree spine.
# ---------------------------------------------------------------------------


def _obj_path(gitdir: Path, sha: str) -> Path:
    return Path(gitdir, "objects", sha[:2], sha[2:])


def write_object(gitdir: Path, kind: bytes, payload: bytes) -> str:
    """Writes a loose object (`<kind> <len>\\0<payload>`, zlib-compressed,
    temp-file + `os.replace`) and returns its 40-char sha. Idempotent: an
    existing object at the target path is left alone, never rewritten,
    never truncated -- a sha collision on differing content cannot happen
    (SHA-1 preimage), so "already exists" always means "already correct".

    Safe ONLY for bytes the caller authored in memory (a tree, a commit,
    or a blob with no worktree/attribute provenance) -- see this module's
    Negative-spec.
    """
    body = kind + b" " + str(len(payload)).encode("ascii") + b"\x00" + payload
    sha = hashlib.sha1(body).hexdigest()
    path = _obj_path(gitdir, sha)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}")
        tmp.write_bytes(zlib.compress(body))
        os.replace(tmp, path)
    return sha


def build_tree(gitdir: Path, entries: dict) -> str:
    """`entries`: `{path: (mode: int, sha: str)}` -> root tree sha.

    Writes every subtree bottom-up via `write_object`. Git sorts tree
    entries by name with directories compared AS IF they carried a
    trailing `/` -- get this wrong and `git fsck` still passes (the tree
    is well-formed) while `git diff`/`git status` report phantom changes
    against a canonical `git write-tree` of the same content. Modes are
    octal-without-leading-zero ASCII bytes (`100644`, `100755`, `120000`,
    `160000`, and `40000` for a subtree -- FIVE digits, not six).
    """
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


# ---------------------------------------------------------------------------
# Read side -- extracted from `coordinator_core.pickup_assemble` so exactly
# one implementation of the pack/loose object reader exists. `pickup_assemble`
# imports these back down (see its own module docstring's Consumes manifest).
# ---------------------------------------------------------------------------


class _GitReadModelError(Exception):
    """Internal signal only -- an object-store read shape this module does
    not (yet) understand, or a corrupt/truncated pack stream. Caught at
    each caller's own dispatch boundary; never expected to propagate past
    a top-level read entry point."""


_PACK_TYPE_NAMES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_PACK_TYPE_NUMS = {v: k for k, v in _PACK_TYPE_NAMES.items()}

# Well above git's own `pack.depth` default ceiling of 50, so a well-formed
# pack never approaches this; only a corrupt/adversarial/cyclic delta chain
# does.
_MAX_DELTA_DEPTH = 200


class _PackIndex(NamedTuple):
    pack_path: Path
    fanout: tuple  # 256 cumulative counts (v2 idx fanout table)
    shas: bytes  # N * 20 bytes, sorted ascending
    offsets: tuple  # N pack byte-offsets, parallel to `shas`


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


# Caches the pack LISTING per `common_dir`, revalidated against the pack
# directory's own `(st_mtime_ns, st_size)` rather than by re-listing it.
# The listing was previously rebuilt on every object lookup, costing one
# `glob` plus two `stat`s per pack per lookup. On a volume where `stat`
# costs ~15ms -- measured, X: on the normal tier -- that put 2,937 `stat`
# calls and 32s of a 40s profile of one `pickup-assemble brief` into this
# function alone, and the brief did not complete inside 13 minutes.
#
# Staleness, which the previous no-cache docstring correctly cared about:
# git adds and removes packs by creating and unlinking entries IN this
# directory, so any change to the pack set moves the directory's own
# mtime. Revalidation is therefore one `stat` of the directory rather than
# a re-listing, and a `git gc` or incoming fetch between calls is still
# seen. A lookup that HITS needs no revalidation at all -- a sha is
# content-addressed, so an object found in a pack the cached listing
# already knows about is the right object whether or not another pack has
# since appeared. Only a MISS can be changed by a new pack, and only a
# miss pays the revalidating `stat` (see `_read_pack_object_by_sha`).
_PACK_LISTING_CACHE: "dict[str, tuple[Optional[tuple[int, int]], list[tuple[Path, Path]]]]" = {}


def _pack_dir_generation(pack_dir: Path) -> Optional[tuple[int, int]]:
    """`(st_mtime_ns, st_size)` of the pack directory itself, or None when
    it does not exist. One `stat`, and it moves whenever a pack is added
    or removed."""
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
            pack_path = idx_path.with_suffix(".pack")
            if pack_path.is_file():
                result.append((idx_path, pack_path))
    _PACK_LISTING_CACHE[key] = (generation, result)
    _PACK_INDEXES_BY_LISTING.pop(key, None)
    return result


# Parsed `.idx` bodies for the CURRENT cached listing, keyed the same way.
# `_parse_pack_index` memoizes by `(path, st_mtime_ns, st_size)` and so
# stats every `.idx` on every call -- 1,939 of the 2,937 `stat` calls in
# the profile above. This layer holds the parse for exactly as long as the
# listing it came from is valid, which is exactly as long as the pack set
# is unchanged, so the per-lookup stat disappears without widening the
# staleness window: it is dropped whenever `_iter_pack_files` rebuilds.
_PACK_INDEXES_BY_LISTING: "dict[str, list[tuple[Path, Path, _PackIndex]]]" = {}


def _pack_indexes(
    common_dir: Path, *, revalidate: bool = True
) -> list[tuple[Path, Path, "_PackIndex"]]:
    """`(idx_path, pack_path, parsed_index)` for every pack under
    `common_dir`, skipping any pack whose `.idx` fails to parse."""
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


# Memoizes the PARSE of a `.idx` file, keyed `(path, st_mtime_ns, st_size)`
# -- never the pack LISTING (`_iter_pack_files` stays uncached; see its own
# docstring). A rewritten or replaced `.idx` changes its own key, so a stale
# entry can never be served for content that has since changed; it just
# becomes an orphaned entry, reclaimed by the LRU cap below like any other.
# `_parse_pack_index` was previously called once per object lookup instead
# of once per pack (27 calls to resolve 6 objects on one spine walk in the
# measured baseline) -- this is a pure lookup-cost fix, not a staleness
# tradeoff: `_iter_pack_files` still re-lists live on every call, so a pack
# added or removed between calls is still seen; only the repeated re-parse
# of an unchanged `.idx` is what gets skipped.
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
    """Parses a v2 pack `.idx`: 8-byte header, 256-entry fanout table,
    sorted sha table, CRC32 table (skipped, not needed for object lookup),
    a 32-bit offset table, and -- only when a pack exceeds 2GB -- a 64-bit
    "large offsets" overflow table addressed via the MSB of a 32-bit
    entry. v1 idx (pre-2005) is not supported; no repo on this stack
    produces one. A failed magic-byte check degrades to `None` for either
    a benign v1 idx OR a genuinely corrupt v2 idx -- those two cases are
    indistinguishable here and both silently drop the pack from every
    downstream lookup, rather than surfacing corruption; accepted per the
    v1-out-of-scope carve-out, not urgent unless this becomes real."""
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
    pos += count * 4  # CRC32 table -- unused, object lookup doesn't need it
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
    """Binary search the fanout-bounded sorted sha table; returns the pack
    byte offset of the object, or None if `sha_hex` isn't in this pack."""
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
    """Maps the pack file rather than reading it. Returns a read-only mmap,
    which satisfies every use the pack byte-string is put to below --
    `len()`, integer indexing, slicing, and `memoryview()` in
    `_zlib_decompress_bounded` -- while faulting in only the pages an
    object read actually touches.

    This was `pack_path.read_bytes()`, one whole-file read per object
    lookup. Measured on one `pickup-assemble brief` against this repo:
    2,026 calls reading **22.33GB** to serve a working set of a few
    thousand objects, 9.46s of an 11.78s run even with every byte already
    in the OS page cache. The pack set here is 427MB across 8 packs and
    the largest single pack is 150MB -- a cold read of that pack, once per
    object, is the shape this replaces.

    Deliberately NOT cached and NOT held open across calls: the map drops
    when the caller's last reference to it goes, so this process never
    holds a file mapping that would stop a concurrent `git gc` from
    unlinking a pack it has just repacked (on Windows an open mapping
    makes that unlink fail). Mapping is cheap enough that caching buys
    nothing worth that hazard -- the OS page cache already holds the pages
    across calls, which is where the reuse belongs.

    Falls back to reading the file whole for the one case mmap cannot
    serve: a zero-length pack, which `mmap` rejects outright."""
    fh = open(pack_path, "rb")
    try:
        return mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except ValueError:
        fh.seek(0)
        return fh.read()
    finally:
        fh.close()


def _delta_read_size(data: bytes, pos: int) -> tuple[int, int]:
    """Git's delta-header varint: 7 bits per byte, little-endian, MSB
    continuation flag. Used for both the base-size and result-size header
    fields (values themselves are unused by `_apply_git_delta` -- the
    produced byte count is trusted implicitly, matching git's own
    zero-validation fast path)."""
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
    """Applies a git pack delta (copy/insert instruction stream, RFC-less
    but stable since the pack v2 format's introduction) to `base`,
    producing the target object's content."""
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
    """Decompresses a single zlib stream embedded inside a (possibly tens-
    of-MB) pack file, feeding growing bounded INPUT windows -- starting at
    `size_hint` (the object's own uncompressed size, parsed from the pack
    object header) and doubling -- instead of handing the entire remainder
    of the pack file to `zlib.decompressobj` in one call. Regression this
    prevents: `decompressobj().decompress(mv[pos:])` looks zero-copy
    (input is a memoryview slice), but once the DEFLATE stream's real end
    is found mid-buffer, CPython copies everything past that point into
    `decompressobj.unused_data` -- a full copy of the pack tail, per
    object read. `max_length` alone does not fix this: it bounds the
    OUTPUT per call, not how much of the input buffer gets scanned/copied
    into `unused_data` once the stream ends."""
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
    # and aren't depth-threaded, so a caller catches `RecursionError` too.
    if _depth > _MAX_DELTA_DEPTH:
        raise _GitReadModelError(f"delta chain exceeds max depth {_MAX_DELTA_DEPTH} (cyclic/corrupt pack?)")
    pos = offset
    first = pack_bytes[pos]
    pos += 1
    type_num = (first >> 4) & 0x7
    # Object header size varint (7 bits/byte, little-endian, MSB continuation
    # flag) -- for a non-delta object this is the uncompressed content size;
    # for OFS_DELTA/REF_DELTA it's the uncompressed size of the delta STREAM
    # itself (base-size + copy/insert opcodes), which is exactly the byte
    # count `_zlib_decompress_bounded` needs to size its first window.
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
            # A negative `base_offset` (corrupt/truncated pack) would
            # otherwise resolve via Python's negative indexing into the
            # tail of the file, silently decoding an unrelated byte region
            # as if it were a valid object header.
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
    """Searches one pack set for `sha`. Returns None when the sha is in no
    pack of that set -- distinct from a 1-tuple whose single element is the
    found object, or None for a pack entry of an unsupported type."""
    for _idx_path, pack_path, pidx in _pack_indexes(common_dir, revalidate=revalidate):
        offset = _pack_index_find(pidx, sha)
        if offset is None:
            continue
        pack_bytes = _read_pack_bytes(pack_path)
        type_num, content = _read_pack_object_at(common_dir, pack_path, pack_bytes, offset)
        type_name = _PACK_TYPE_NAMES.get(type_num)
        if type_name is None:
            return (None,)
        return ((type_name, content),)
    return None


def _read_pack_object_by_sha(common_dir: Path, sha: str) -> Optional[tuple[str, bytes]]:
    """Finds `sha` in any pack under `common_dir`. Searched at most twice:
    once against the cached pack set with no revalidating stat, and -- only
    if that misses -- once more against a revalidated one, because a miss is
    the only outcome a newly-arrived pack can change (a hit is
    content-addressed and cannot be)."""
    found = _search_packs_for_sha(common_dir, sha, revalidate=False)
    if found is None:
        found = _search_packs_for_sha(common_dir, sha, revalidate=True)
    if found is None:
        return None
    return found[0]


# Bounded, content-addressed-only cache: keyed on (common_dir, sha), which
# cannot change under its own key (sha IS the content's hash) -- unlike a
# path or ref, safe to hold across this process's entire lifetime. Capped
# so a warm long-running engine cannot accumulate an unbounded number of
# blob/tree/commit payloads over its lifetime; oldest entries evict first.
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


def _read_object(common_dir: Path, sha: str) -> Optional[tuple[str, bytes]]:
    """Reads a git object by full 40-hex sha -- packs first (v2 idx binary
    search across every pack in `objects/pack/`), loose second. Cached per
    (common_dir, sha), bounded (see `_OBJECT_CACHE` above)."""
    sha = sha.lower()
    key = (str(common_dir), sha)
    cached = _object_cache_get(key)
    if cached is not _CACHE_MISS:
        return cached
    # Packs first, loose second: a sha is content-addressed, so a pack copy
    # and a loose copy of the same sha are byte-identical and this ordering
    # is a performance choice only, given intact object stores. A
    # found-but-corrupt pack entry (truncated zlib stream, a bad delta
    # chain) raises rather than returning None, so it would otherwise skip
    # the loose fallback below entirely; catch narrowly and fall back so a
    # damaged pack copy doesn't shadow an intact loose copy of the same sha.
    try:
        result = _read_pack_object_by_sha(common_dir, sha)
    except (_GitReadModelError, zlib.error, struct.error):
        result = None
    if result is None:
        result = _read_loose_object(common_dir, sha)
    _object_cache_put(key, result)
    return result


# ---------------------------------------------------------------------------
# Ref CAS + reflog.
# ---------------------------------------------------------------------------


def _read_ref_raw(ref_path: Path) -> Optional[str]:
    try:
        content = ref_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return content or None


def _log_all_ref_updates(gitdir: Path) -> bool:
    """`core.logAllRefUpdates` -- git defaults this to true for any repo
    with a worktree (only a bare repo defaults it to false); this reads
    `gitdir/config` for an explicit override and otherwise assumes the
    non-bare default, since `cas_ref` is never used against a bare repo
    in this codebase."""
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
    """Appends one entry to `logs/<ref>` in git's own format:
    `<old> <new> <committer>\\t<message>\\n` -- `committer` already bundles
    `Name <email> <timestamp> <tz>` (the same string used for the commit
    object's own author/committer line), and `old`/`new` are full 40-hex
    shas (`old=None` renders as git's all-zero "new ref" sentinel).
    Honours `core.logAllRefUpdates` (silently a no-op when it's false).
    Caller's responsibility to call this under the same lock as the ref
    write it documents (see `cas_ref`)."""
    if not _log_all_ref_updates(gitdir):
        return
    log_path = gitdir / "logs" / ref
    log_path.parent.mkdir(parents=True, exist_ok=True)
    old_hex = old if old else "0" * 40
    # Explicit LF -- `open(..., "a")` in text mode would translate `\n` to
    # `\r\n` on Windows, and `git fsck --strict` reports trailing garbage
    # for a CRLF-terminated reflog line just as it does for a ref file.
    line = f"{old_hex} {new} {committer}\t{message}\n".encode("utf-8")
    with open(log_path, "ab") as fh:
        fh.write(line)


def _head_symref_target(head_gitdir: Path) -> Optional[str]:
    """The ref `HEAD` points at (`refs/heads/<branch>`), or `None` when
    `HEAD` is detached, unreadable, or absent.

    Exists because `logs/HEAD` is a SECOND reflog git maintains alongside
    `logs/<ref>`: `git update-ref refs/heads/x` writes both whenever HEAD
    symrefs to `x`, and `git reflog` with no argument reads `logs/HEAD`.
    Writing only `logs/<ref>` leaves every coordinator commit invisible to
    the bare `git reflog` a human actually runs after a bad landing."""
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
    """Git's lockfile protocol, not a read-compare-write: `O_CREAT|O_EXCL`
    on `<ref>.lock` (an existing lock is a refusal, never a wait-and-
    overwrite), re-read `ref` UNDER the lock, compare to `expected`, write
    the lock file (`write_bytes` + explicit LF -- `write_text` emits CRLF
    on Windows and `git fsck --strict` then reports `trailingRefContent`),
    `os.replace` onto `ref`, remove the lock on every exit path.

    `expected=None` means "must not exist". Returns False on a mismatch
    (lost CAS race or wrong starting point); never raises on a lost race.
    For a detached HEAD, the CAS target IS `HEAD` itself -- pass
    `ref="HEAD"` with `gitdir` as the worktree-private git dir in that
    case, rather than a `refs/heads/<branch>` path under the common dir.

    When `reflog_committer`/`reflog_message` are given, the reflog entry
    (AC8 -- `logs/<ref>`) is appended while the lock file still exists,
    before the `os.replace` onto `ref`, so the write and the log entry
    share one atomic critical section under the lock.
    """
    ref_path = gitdir / ref
    lock_path = gitdir / (ref + ".lock")
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.close(fd)
        current = _read_ref_raw(ref_path)
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
        os.replace(lock_path, ref_path)
        return True
    finally:
        if lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass
