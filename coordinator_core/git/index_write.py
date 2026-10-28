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

- **It does not write index v4.** v4 (prefix-compressed names) is REFUSED, not
  guessed at, matching `git_index.py`'s own refusal. v2 and v3 are spliced: a
  v3 index is what git writes the moment ANY entry carries extended flags
  (intent-to-add, skip-worktree), so refusing v3 left every committed path's
  entry at its pre-commit state after the ref had moved. Extended-flag entries
  are copied verbatim; the header is v3 only while one survives.
- **It does not preserve extensions.** The TREE cache and REUC/UNTR
  extensions are dropped on write. This is legal and is what git itself does
  when it cannot incrementally update the cache -- git regenerates TREE on the
  next command that wants it. Dropping it costs a later `write-tree` some
  work; keeping a STALE one would produce a wrong tree, which is the failure
  that matters.
- **It does not merge.** An unmerged index (any entry at stage != 0) is
  refused outright -- a commit pass has no business splicing into a
  mid-conflict index. The one carve-out is `collapse_unmerged`: the caller
  names the resolved paths, each must carry a stage-0 update, and only those
  paths' stage 1-3 entries are replaced. Any other unmerged path still refuses.
- **It is not a general index editor.** One call, one splice, under the lock.

`git_index.py`'s module docstring forbids WRITING from that module, which is
why this is a separate one rather than an edit there.
"""

from __future__ import annotations

import bisect
import hashlib
import os
import stat
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Collection, Dict, Mapping, Optional, Tuple, Union

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_objects import _replace_with_retry
from coordinator_core.git.tree_spine import _ABSENT
from coordinator_core.lock_preflight import preflight_reap_stale_lock

_SIGNATURE = b"DIRC"
_ENTRY_FIXED_LEN = 62
_EXT_FLAGS_LEN = 2
_SUPPORTED_VERSIONS = (2, 3)
_FLAG_EXTENDED = 0x4000
_XFLAG_INTENT_TO_ADD = 0x2000


_ZERO_STAT = SimpleNamespace(st_mtime=0, st_size=0)


class _DropIfIntentToAdd:
    """Update value: remove the entry only if it is an intent-to-add entry whose file is gone."""


DROP_IF_PHANTOM_INTENT_TO_ADD = _DropIfIntentToAdd()

ABSENT = _ABSENT


class IndexWriteError(Exception):
    pass


class DotGitPathRefused(IndexWriteError):
    pass


def refuse_dotgit(names) -> None:
    """Chokepoint: no index entry may carry a `.git` component (GitHub `hasDotgit`)."""
    for n in names:
        if ".git" in str(n).replace("\\", "/").lower().split("/"):
            raise DotGitPathRefused(f"refused: .git path: {n}")


class IndexWriteLockBusy(IndexWriteError):
    pass


class IndexStaleAfterCommit(IndexWriteError):
    """THE COMMIT LANDED AND ONLY THE INDEX IS STALE -- the one outcome on
    this surface that must not be retried.

    `commit.py` takes `index.lock` before the ref swap but renames the built
    index onto `.git/index` only AFTER it: an index matching a commit that
    never landed is the same lie in the other direction. So a failure at the
    rename is on the far side of durability.
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

    def __init__(
        self, *args: object, outcome: object = None, paths: Tuple[str, ...] = ()
    ) -> None:
        super().__init__(*args)
        self.outcome = outcome
        self.paths = paths


def _name_offset(raw: bytes, start: int) -> int:
    return start + _ENTRY_FIXED_LEN + (_EXT_FLAGS_LEN if raw[start + 60] & 0x40 else 0)


def _entry_name(raw: bytes, start: int) -> bytes:
    begin = _name_offset(raw, start)
    return raw[begin : raw.index(b"\x00", begin)]


def _walk_entries(
    raw: bytes, entry_count: int, collapse: frozenset = frozenset()
) -> Tuple[list, int]:
    """Entry start offsets and the end of the entry block, reading each name
    length from the flags field (a 0xFFF length saturates and needs a NUL scan).
    A stage != 0 entry is kept in the walk only when its name is in `collapse`."""
    starts: list = []
    offset = 12
    limit = len(raw)
    for _ in range(entry_count):
        if offset + _ENTRY_FIXED_LEN > limit:
            raise IndexWriteError(f"truncated entry at offset {offset}")
        (flags,) = struct.unpack_from(">H", raw, offset + 60)
        if (flags >> 12) & 0x3 and (not collapse or _entry_name(raw, offset) not in collapse):
            raise IndexWriteError(
                "unmerged entry (stage != 0) -- refusing to splice a mid-conflict index"
            )
        fixed = _ENTRY_FIXED_LEN + (_EXT_FLAGS_LEN if flags & _FLAG_EXTENDED else 0)
        if offset + fixed > limit:
            raise IndexWriteError(f"truncated entry at offset {offset}")
        name_len = flags & 0x0FFF
        if name_len == 0x0FFF:
            nul = raw.find(b"\x00", offset + fixed)
            if nul < 0:
                raise IndexWriteError(f"unterminated name at offset {offset}")
            name_len = nul - offset - fixed
        starts.append(offset)
        offset += (fixed + name_len + 8) & ~7
    if offset > limit:
        raise IndexWriteError(f"truncated entry at offset {starts[-1]}")
    return starts, offset


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


def _worktree_matches_blob(file: Path, mode: int, sha_hex: str, st: os.stat_result) -> bool:
    """False only when `file` is a regular file whose git blob hash differs from `sha_hex`.

    An entry carrying a differing blob next to the worktree's stat reads clean to git, hiding
    the unlanded bytes; the caller then records a zeroed stat so git re-hashes. Symlinks,
    gitlinks and unreadable files keep the real stat (hashing them here is not the contract).
    Costs one full read per spliced file; clean/autocrlf-filtered files and sha256-format repos
    always mismatch, which only zeroes the stat (git re-hashes), never hides content.
    """
    if mode not in (0o100644, 0o100755) or not stat.S_ISREG(st.st_mode):
        return True
    h = hashlib.sha1(b"blob %d\x00" % st.st_size)
    seen = 0
    try:
        with open(file, "rb") as fh:
            while chunk := fh.read(1 << 20):
                seen += len(chunk)
                h.update(chunk)
    except OSError:
        return True
    return seen == st.st_size and h.hexdigest() == sha_hex.lower()


def splice_index(
    repo: Union[str, Path],
    updates: Mapping[str, object],
    *,
    stat_fallback: bool = False,
    collapse_unmerged: Collection[str] = frozenset(),
) -> None:
    """Splice `updates` into `.git/index` under `index.lock`.
    See `prepare_splice` for the arguments and the two-phase form.
    """
    prepare_splice(
        repo, updates, stat_fallback=stat_fallback, collapse_unmerged=collapse_unmerged
    ).publish()


def prepare_splice(
    repo: Union[str, Path],
    updates: Mapping[str, object],
    *,
    stat_fallback: bool = False,
    collapse_unmerged: Collection[str] = frozenset(),
) -> PreparedSplice:
    """Take `index.lock` and build the spliced index in it; `.git/index` is untouched until
    `PreparedSplice.publish()`. Raises `IndexWriteLockBusy` when a peer holds the lock, with
    nothing written; any failure while building releases the lock.

    `collapse_unmerged`: paths whose stage 1-3 entries are dropped and replaced by their stage-0
    update. Each must appear in `updates` (an ABSENT value resolves it to deletion); any other
    unmerged entry refuses.

    `stat_fallback`: a path whose worktree file cannot be stat'ed still gets its entry, with a zeroed
    stat that git reads as changed and re-hashes. Off, one unstat-able path refuses the whole splice.
    """
    refuse_dotgit(updates)
    collapse = frozenset(
        str(p).replace("\\", "/").encode("utf-8", "surrogateescape") for p in collapse_unmerged
    )
    update_keys = {
        str(p).replace("\\", "/").encode("utf-8", "surrogateescape") for p in updates
    }
    missing = sorted(collapse - update_keys)
    if missing:
        raise IndexWriteError(
            f"collapse_unmerged path has no update to resolve it to: {missing[0]!r}"
        )
    gitdir = resolve_git_dir(repo)
    index_path = gitdir / "index"
    lock_path = gitdir / "index.lock"
    root = Path(repo)

    preflight_reap_stale_lock(str(root))

    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise IndexWriteLockBusy(f"{lock_path} exists -- a peer holds the index") from exc

    prepared = PreparedSplice(lock_path, index_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            _splice_locked(
                handle, root, index_path, lock_path, updates, stat_fallback, collapse
            )
    except BaseException:
        prepared.abort()
        raise
    return prepared


def _splice_locked(
    handle: IO[bytes],
    root: Path,
    index_path: Path,
    lock_path: Path,
    updates: Mapping[str, object],
    stat_fallback: bool = False,
    collapse: frozenset = frozenset(),
) -> None:
    try:
        raw = index_path.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        raise IndexWriteError(f"could not read {index_path}: {exc}") from exc

    starts: list = []
    body_end = 12
    in_version = _SUPPORTED_VERSIONS[0]
    if raw:
        if len(raw) < 12 or raw[0:4] != _SIGNATURE:
            raise IndexWriteError(f"{index_path}: not a DIRC index")
        version, entry_count = struct.unpack_from(">II", raw, 4)
        if version not in _SUPPORTED_VERSIONS:
            raise IndexWriteError(
                f"{index_path}: index v{version} -- only v2/v3 are written, "
                "refused rather than guessed at"
            )
        in_version = version
        starts, body_end = _walk_entries(raw, entry_count, collapse)

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
        if value is DROP_IF_PHANTOM_INTENT_TO_ADD:
            idx = bisect.bisect_left(starts, key, key=lambda st: _entry_name(raw, st))
            if (
                idx < len(starts)
                and _entry_name(raw, starts[idx]) == key
                and raw[starts[idx] + 60] & 0x40
                and struct.unpack_from(">H", raw, starts[idx] + _ENTRY_FIXED_LEN)[0]
                & _XFLAG_INTENT_TO_ADD
                and not (root / path).exists()
            ):
                replacements[key] = None
            continue
        mode, sha_hex = value  # type: ignore[misc]
        try:
            st = (root / path).stat()
        except OSError as exc:
            if not stat_fallback:
                raise IndexWriteError(
                    f"cannot stat {path} to record its index entry: {exc}"
                ) from exc
            st = _ZERO_STAT
        if st is not _ZERO_STAT and not _worktree_matches_blob(
            root / path, int(mode), str(sha_hex), st
        ):
            st = _ZERO_STAT
        replacements[key] = _build_entry(key, int(mode), str(sha_hex), st)

    for key in collapse:
        if key not in replacements:
            raise IndexWriteError(
                f"collapse_unmerged path has no update to resolve it to: {key!r}"
            )

    view = memoryview(raw)
    count = len(starts)
    bounds = starts + [body_end]

    def name_at(start: int) -> bytes:
        return _entry_name(raw, start)

    pieces: list = []
    lo = 0
    total = count
    # Extended-flag entries still standing after the splice; only a v3 input can hold any.
    extended = (
        sum(1 for st in starts if raw[st + 60] & 0x40) if in_version == 3 else 0
    )
    for key in sorted(replacements):
        new_bytes = replacements[key]
        idx = bisect.bisect_left(starts, key, lo=lo, key=name_at)
        present = idx < count and name_at(starts[idx]) == key
        if not present and new_bytes is None:
            continue
        if idx > lo:
            pieces.append(view[bounds[lo] : bounds[idx]])
        if new_bytes is not None:
            pieces.append(new_bytes)
        if present:
            hi = idx + 1
            while hi < count and name_at(starts[hi]) == key:
                hi += 1
            lo = hi
            for j in range(idx, hi):
                extended -= raw[starts[j] + 60] & 0x40 != 0
            total -= hi - idx
            if new_bytes is not None:
                total += 1
        else:
            lo = idx
            total += 1
    if lo < count:
        pieces.append(view[bounds[lo] : body_end])

    digest = hashlib.sha1()
    head = struct.pack(">4sII", _SIGNATURE, 3 if extended > 0 else 2, total)
    digest.update(head)
    for piece in pieces:
        digest.update(piece)

    handle.write(head)
    for piece in pieces:
        handle.write(piece)
    handle.write(digest.digest())
    handle.close()


class PreparedSplice:
    """A fully built replacement index sitting in a held `index.lock`.

    Exactly one of `publish()` / `abort()` ends it; both release the lock. `publish()` is the
    only step that touches `.git/index`, so a caller can move a ref between `prepare_splice`
    and `publish` and know the index write cannot be refused by a peer's lock.
    """

    def __init__(self, lock_path: Path, index_path: Path) -> None:
        self._lock_path = lock_path
        self._index_path = index_path

    def abort(self) -> None:
        if self._lock_path.exists():
            try:
                self._lock_path.unlink()
            except OSError:
                pass

    def publish(self) -> None:
        # Abort only on failure: after a successful rename the lock path is free, and a peer may
        # already hold a fresh `index.lock` there that `abort()` would delete.
        try:
            self._publish()
        except BaseException:
            self.abort()
            raise

    def _publish(self) -> None:
        lock_path, index_path = self._lock_path, self._index_path
        if not _replace_with_retry(lock_path, index_path):
            _raise_stale(index_path)


def _raise_stale(index_path: Path) -> None:
    """The rename onto `.git/index` failed (a Windows reader held it): a typed stale-index report,
    not a bare `PermissionError`. When a ref moved first, the commit already landed."""
    raise IndexStaleAfterCommit(
        f"{index_path} could not be updated -- a peer held it. The "
        f"commit LANDED; only the shared index is stale. `git status` "
        f"may misreport these paths until any index write refreshes it."
    )
