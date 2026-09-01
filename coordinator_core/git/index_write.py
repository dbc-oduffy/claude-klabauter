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

_SIGNATURE = b"DIRC"
_ENTRY_FIXED_LEN = 62
_SUPPORTED_VERSION = 2

#: Sentinel: remove this path from the index (a staged deletion).
#:
#: THE SAME OBJECT `tree_spine` uses, deliberately, not a private twin. The
#: commit path builds ONE `assembled` dict and hands it to both
#: `_rewrite_head_spine` and `splice_index`; two distinct `object()` sentinels
#: would compare `is`-unequal and each leg would then try to unpack the
#: other's marker as a `(mode, sha)` pair. Found by claude-klabauter-fd against
#: a real staged deletion, which is the one shape that hits it.
ABSENT = _ABSENT


class IndexWriteError(Exception):
    """The index could not be spliced. Never partial: raised BEFORE any
    bytes reach `.git/index`, so the on-disk index is always either the
    original or the fully-spliced result."""


class IndexWriteLockBusy(IndexWriteError):
    """`.git/index.lock` already exists -- a peer is mid-write. The caller
    retries or refuses; this module never waits and never steals a lock."""


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
    """`(name_bytes, end_offset)` for the entry starting at `offset`."""
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
    """One v2 entry, laid out exactly as git writes it.

    The stat fields are what git's own `ce_match_stat` compares a worktree
    file against to answer "clean" without reading its bytes, so they must
    describe the file as it is right now -- a wrong mtime here makes every
    later `git status` re-hash the file (slow but correct), while a wrong
    size can make it read clean when it is not (fast and WRONG). Both are
    taken from one `stat()` of the file being recorded.
    """
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
    """Apply `updates` to `.git/index` in place, spawn-free.

    `updates` maps a repo-relative path to either `(mode, blob_sha_hex)` --
    insert or replace that entry -- or the `ABSENT` sentinel, meaning remove
    it (a staged deletion). A path already in the index is replaced in place;
    a new path is inserted in git's own sort order (byte-wise on the name).

    Every entry NOT named in `updates` is copied byte-for-byte from the index
    that was read, so fields this codebase cannot model round-trip untouched.

    Raises `IndexWriteLockBusy` if `.git/index.lock` exists. Raises
    `IndexWriteError` before writing anything on any shape it refuses.

    Raises `IndexStaleAfterCommit` when the final `os.replace` cannot land
    because a peer holds `.git/index`. This one is NOT a before-writing
    refusal like the two above, and callers must not treat it as one: this
    function is called after the ref swap, so the commit has already landed
    and only the index is stale. Retrying it commits the same work twice.

    THE READ IS UNDER THE LOCK, AND THAT IS THE WHOLE POINT OF TAKING IT.
    Every untouched entry is copied verbatim from the index this call read, so
    the read is not a lookup -- it is the base revision of a read-modify-write
    over state ~50 sessions share. Reading before acquiring the lock leaves the
    classic lost update wide open: a peer commit landing between our read and
    our write is silently reverted in the index, because our verbatim copy of
    the older snapshot overwrites it. For a path the peer ADDED that is worse
    than stale -- the path is in HEAD and absent from the index, which is
    exactly the shape `git status` renders as a staged deletion (`D `) of a
    file sitting on disk, and which a bare `git commit -a` by any session then
    lands for real. `IndexWriteLockBusy` on its own does NOT close this: it
    only reports that a peer held the lock at the instant we tried to write,
    which says nothing about whether the snapshot we are about to write back
    is still current. Holding the lock across read-modify-write is what closes
    it. The cost is k `stat` calls inside the critical section (k = the paths
    this commit touches, not the tree), which is why this is affordable at the
    session count that makes it necessary.
    """
    gitdir = resolve_git_dir(repo)
    index_path = gitdir / "index"
    lock_path = gitdir / "index.lock"
    root = Path(repo)

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise IndexWriteLockBusy(f"{lock_path} exists -- a peer holds the index") from exc

    # `os.fdopen` here, not down on the write path: every refusal below is
    # raised with the lock held, and on Windows an open descriptor on
    # `index.lock` makes the `finally`'s unlink fail -- which would leave a
    # refused call holding the lock against every peer for the life of the
    # process. The `with` closes it on the refusal paths too; the write path
    # closes it before the rename, which is what `os.replace` needs anyway.
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
    """The read-modify-write body of `splice_index`, run with `.git/index.lock`
    already held and `handle` open on it. Split out so the lock's `finally`
    cannot be separated from its acquisition by the length of the body."""
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
    # Closed BEFORE the rename, not by the caller's `with` after it: Windows
    # refuses `os.replace` on a source file that still has an open descriptor,
    # so leaving this to the context manager turns every write into the
    # `IndexStaleAfterCommit` path. The caller's `with` still closes it on the
    # refusal paths; a second close is a no-op.
    handle.close()
    if not _replace_with_retry(lock_path, index_path):
        # WAS UNWRAPPED, AND THAT BROKE THE DOCUMENTED CONTRACT. This
        # function's own docstring promises `IndexWriteLockBusy` or
        # `IndexWriteError`; the `try:` around this line carries only a
        # `finally:`, so a Windows `PermissionError` escaped as neither and
        # a caller written correctly against that contract still would not
        # catch it. Captured at 2/200 with 12 concurrent committers.
        #
        # A LOST INDEX WRITE IS NOT A LOST COMMIT, and the distinction is
        # the whole disposition here: `commit.py` splices the index AFTER
        # the ref swap, deliberately (an index matching a commit that never
        # landed is the same lie in the other direction), so reaching this
        # line means the commit ALREADY LANDED. The failure leaves a stale
        # index, not lost work, and the honest report says so rather than
        # implying the commit failed.
        raise IndexStaleAfterCommit(
            f"{index_path} could not be updated -- a peer held it. The "
            f"commit LANDED; only the shared index is stale. `git status` "
            f"may misreport these paths until any index write refreshes it."
        )
