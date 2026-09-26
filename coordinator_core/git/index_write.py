"""coordinator_core.git.index_write -- splice k entries into `.git/index`
without spawning git, and without re-serialising the entries this commit did
not touch.

WHY THIS EXISTS. `git add` is the only `.git/index` write in a commit pass,
and it costs ~20.3ms of process creation -- the single largest item in the
brightline budget once the tree is built in process. Deleting it is not
optional at a zero-spawn target, but deleting it WITHOUT replacing the index
write is corruption, not an optimisation: HEAD would move while `.git/index`
still described the old state, and every one of the ~50 sessions sharing this
worktree would read a newly-committed path as a staged deletion (present in
HEAD, absent from the index) or a worktree-sourced edit as a staged
reverse-change. `git status` is the blast radius.

SPLICE, NOT RE-SERIALISE. `git_state.IndexEntry` is `(mode, sha, stage)` and
`git_index.IndexIdentity` adds only the stat fields it needs; NEITHER carries
the full per-entry record (dev, ino, uid, gid, ctime, flags, extended flags),
so nothing in this codebase can round-trip an index it parsed. Rewriting from
parsed state would silently drop fields git wrote, which is how an index gets
subtly wrong rather than loudly broken. So: every untouched entry's bytes are
copied VERBATIM from the file that was read, and only the k entries this
commit touches are constructed.

NEGATIVE SPEC -- what this module deliberately does not do:

- **It does not write index v3 or v4.** A v3 index (extended flags) or v4
  (prefix-compressed names) is REFUSED, not guessed at, matching
  `git_index.py`'s own refusal. v2 is what git writes for this repo.
- **It does not preserve extensions.** The TREE cache and REUC/UNTR
  extensions are dropped on write. This is legal and is what git itself does
  when it cannot incrementally update the cache -- git regenerates TREE on the
  next command that wants it. Dropping it costs a later `write-tree` some
  work; keeping a STALE one would produce a wrong tree, which is the failure
  that matters.
- **It does not merge.** An unmerged index (any entry at stage != 0) is
  refused outright -- a commit pass has no business splicing into a
  mid-conflict index.
- **It is not a general index editor.** One call, one splice, under the lock.

`git_index.py`'s module docstring forbids WRITING from that module, which is
why this is a separate one rather than an edit there.
"""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path
from typing import IO, Dict, Mapping, Optional, Tuple, Union

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_objects import _replace_with_retry
from coordinator_core.git.tree_spine import _ABSENT
from coordinator_core.lock_preflight import preflight_reap_stale_lock

_SIGNATURE = b"DIRC"
_ENTRY_FIXED_LEN = 62
_SUPPORTED_VERSION = 2

ABSENT = _ABSENT


class IndexWriteError(Exception):
    pass


class IndexWriteLockBusy(IndexWriteError):
    pass


class IndexStaleAfterCommit(IndexWriteError):
    """THE COMMIT LANDED AND ONLY THE INDEX IS STALE -- the one outcome on
    this surface that must not be retried.

    `commit.py` splices the index AFTER the ref swap, deliberately: an index
    matching a commit that never landed is the same lie in the other
    direction. So a failure at the splice is on the far side of durability.
    The work is in history, the caller holds a real sha, and the only damage
    is that peers' `git status` misreports those paths until any subsequent
    index write refreshes it.

    Distinguished from its siblings because the difference decides what the
    caller does, and getting it wrong is expensive in both directions.
    `IndexWriteError` and `IndexWriteLockBusy` are raised BEFORE any bytes
    reach `.git/index` and before the ref moves, so retrying is correct
    there. Retrying THIS one commits the same work twice. That hazard is not
    hypothetical: `commit_pipeline`'s `NameError` produced exactly this shape
    for real earlier today, with two peer commits reported as failures after
    they had landed, and the fix was to teach the caller which side of the ref
    it was on. `CommitOutcome` carries a sha on success and had nothing that
    said "committed, index stale" -- this type is that missing word, and it
    turns a retry hazard into a `git status`.

    `outcome` carries the `CommitOutcome` the call would have RETURNED had the
    splice succeeded -- sha included. Without it this type names the right
    outcome and still strands the caller, who knows a commit landed but not
    which one, and so cannot report a sha or stamp a trailer. It is optional
    only so the type stays constructible in tests and by any future raiser
    that genuinely has no outcome in hand; every raiser on the commit path
    passes it.

    Typed as `object` rather than importing `CommitOutcome`: `commit.py`
    already imports THIS module, so naming its type here would close an
    import cycle. The one raiser is `commit.py` itself and it passes the real
    thing.
    """

    def __init__(self, *args: object, outcome: object = None) -> None:
        super().__init__(*args)
        self.outcome = outcome


def _entry_span(raw: bytes, offset: int) -> Tuple[bytes, int]:
    if offset + _ENTRY_FIXED_LEN > len(raw):
        raise IndexWriteError(f"truncated entry at offset {offset}")
    entry_start = offset
    flags = struct.unpack(">H", raw[offset + 60 : offset + 62])[0]
    if flags & 0x4000:
        raise IndexWriteError(
            "extended-flags entry (index v3 shape) -- refused, not guessed at"
        )
    if (flags >> 12) & 0x3:
        raise IndexWriteError(
            "unmerged entry (stage != 0) -- refusing to splice a mid-conflict index"
        )
    offset += _ENTRY_FIXED_LEN
    nul = raw.find(b"\x00", offset)
    if nul < 0:
        raise IndexWriteError(f"unterminated name at offset {offset}")
    name = raw[offset:nul]
    entry_len = (offset - entry_start) + len(name) + 1
    padding = (8 - (entry_len % 8)) % 8
    return name, offset + len(name) + 1 + padding


def _build_entry(name: bytes, mode: int, sha_hex: str, st: os.stat_result) -> bytes:
    sha = bytes.fromhex(sha_hex)
    if len(sha) != 20:
        raise IndexWriteError(f"bad blob sha for {name!r}: {sha_hex!r}")
    name_len = min(len(name), 0x0FFF)
    out = struct.pack(
        ">IIIIIIIIII20sH",
        int(getattr(st, "st_ctime", 0)),
        getattr(st, "st_ctime_ns", 0) % 1_000_000_000,
        int(st.st_mtime),
        getattr(st, "st_mtime_ns", 0) % 1_000_000_000,
        getattr(st, "st_dev", 0) & 0xFFFFFFFF,
        getattr(st, "st_ino", 0) & 0xFFFFFFFF,
        mode,
        getattr(st, "st_uid", 0) & 0xFFFFFFFF,
        getattr(st, "st_gid", 0) & 0xFFFFFFFF,
        st.st_size & 0xFFFFFFFF,
        sha,
        name_len,
    )
    out += name + b"\x00"
    padding = (8 - (len(out) % 8)) % 8
    return out + b"\x00" * padding


def splice_index(
    repo: Union[str, Path],
    updates: Mapping[str, object],
) -> None:
    gitdir = resolve_git_dir(repo)
    index_path = gitdir / "index"
    lock_path = gitdir / "index.lock"
    root = Path(repo)

    preflight_reap_stale_lock(str(root))

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise IndexWriteLockBusy(f"{lock_path} exists -- a peer holds the index") from exc

    try:
        with os.fdopen(fd, "wb") as handle:
            _splice_locked(handle, root, index_path, lock_path, updates)
    finally:
        if lock_path.exists():
            try:
                lock_path.unlink()
            except OSError:
                pass


def _splice_locked(
    handle: IO[bytes],
    root: Path,
    index_path: Path,
    lock_path: Path,
    updates: Mapping[str, object],
) -> None:
    try:
        raw = index_path.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise IndexWriteError(f"could not read {index_path}: {exc}") from exc

    kept: list = []
    if raw:
        if len(raw) < 12 or raw[0:4] != _SIGNATURE:
            raise IndexWriteError(f"{index_path}: not a DIRC index")
        version, entry_count = struct.unpack(">II", raw[4:12])
        if version != _SUPPORTED_VERSION:
            raise IndexWriteError(
                f"{index_path}: index v{version} -- only v2 is written, "
                "refused rather than guessed at"
            )
        offset = 12
        for _ in range(entry_count):
            name, end = _entry_span(raw, offset)
            kept.append((name, raw[offset:end]))
            offset = end

    replacements: Dict[bytes, Optional[bytes]] = {}
    for path, value in updates.items():
        normalized = path.replace("\\", "/")
        if normalized.startswith("/") or (
            len(normalized) >= 2
            and normalized[1] == ":"
            and normalized[0].isalpha()
        ):
            raise IndexWriteError(
                f"{path!r}: absolute path used as an index key -- refused "
                "before writing, not normalized. An index key is always "
                "repo-relative; the caller must resolve it first."
            )
        key = normalized.encode("utf-8", "surrogateescape")
        if value is ABSENT:
            replacements[key] = None
            continue
        mode, sha_hex = value  # type: ignore[misc]
        try:
            st = (root / path).stat()
        except OSError as exc:
            raise IndexWriteError(
                f"cannot stat {path} to record its index entry: {exc}"
            ) from exc
        replacements[key] = _build_entry(key, int(mode), str(sha_hex), st)

    out_entries: list = []
    seen: set = set()
    for name, entry_bytes in kept:
        if name in replacements:
            seen.add(name)
            new_bytes = replacements[name]
            if new_bytes is not None:
                out_entries.append((name, new_bytes))
            continue
        out_entries.append((name, entry_bytes))
    for name, new_bytes in replacements.items():
        if name not in seen and new_bytes is not None:
            out_entries.append((name, new_bytes))

    out_entries.sort(key=lambda pair: pair[0])

    body = struct.pack(">4sII", _SIGNATURE, _SUPPORTED_VERSION, len(out_entries))
    body += b"".join(entry_bytes for _, entry_bytes in out_entries)
    body += hashlib.sha1(body).digest()

    handle.write(body)
    handle.close()
    if not _replace_with_retry(lock_path, index_path):
        # WAS UNWRAPPED, AND THAT BROKE THE DOCUMENTED CONTRACT. This
        raise IndexStaleAfterCommit(
            f"{index_path} could not be updated -- a peer held it. The "
            f"commit LANDED; only the shared index is stale. `git status` "
            f"may misreport these paths until any index write refreshes it."
        )
