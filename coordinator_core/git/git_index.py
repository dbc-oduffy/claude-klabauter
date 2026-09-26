"""coordinator_core.git.git_index -- an in-process, scoped reader for the
"is this handful of paths modified/deleted/clean" question a commit-path
caller asks before it decides whether to spawn anything at all.

`git status --porcelain` is O(worktree) no matter how narrow the pathspec --
it refreshes the whole index and walks every tracked directory before
filtering output down, to answer a question about the caller's OWN
pathspec, typically a handful of paths. The op never needs that: it needs a
stat per path plus an index lookup, which is exactly what git's own
`ce_match_stat` fast path does before it considers hashing a candidate.

REFUSES index v4 explicitly. `coordinator_core.git.git_index` is a
SEPARATE, NARROWER parser from `coordinator_core.git.git_state` (which
supports v2/v3/v4 for the `(mode, sha, stage)` identity a staged-content
caller needs) -- do not merge them. This module's own value tuple is
`(mode, size, mtime)`, projected off the SAME fixed-width entry layout, and
v4's name prefix-compression is exactly the kind of "guess a byte offset
right or read the wrong path" hazard the module docstring on `git_state`
warns about. A wrong parse here would read as a fast wrong clean/modified
verdict -- worse than the spawn it replaces. `read_index_status` therefore
RAISES `IndexV4Unsupported` rather than attempting the varint-prefix walk;
a caller hitting it falls back to its spawn, same as any other
`IndexParseError` from the sibling module.

`diff_index_name_status` is the exception to the
"narrower than git_state, no sha" framing above: a HEAD-vs-index
add/modified/deleted verdict needs sha identity, not a stat, so it goes
through `git_state.read_index` (full v2/v3/v4 parse) and `git_state.
head_blobs` directly rather than this module's own stat-only parser.

Negative-spec:
    - NO worktree hashing. Same rationale as `git_state`'s "THE WORKTREE
      HASH DOES NOT WORK" section: a `candidate` verdict (stat mismatch)
      is exactly as far as `scoped_status` goes -- settling it needs a
      content hash, which is the caller's job, not this module's.
    - NO process-lifetime cache anywhere in this module. Every call
      re-reads `.git/index` and re-stats the worktree paths fresh -- see
      `git_state.read_index`'s identical rationale (a cached scoped-status
      answer is the same partial-stage hazard class as a cached
      full-index snapshot). `diff_index_name_status` inherits this: it
      re-reads the index and re-calls `head_blobs` on every invocation,
      relying solely on `head_blobs`'s OWN memoisation for spawn-avoidance
      -- see that function's docstring for why serving two call sites
      (with a `git add` landing between them) from one cached diff result
      here would be a silent correctness bug, not an optimization.
    - `scoped_status`/`parse_index_stat` do NOT spawn `git` -- every path
      there is a file read or an `os.stat`. `diff_index_name_status` is
      the one function in this module that reaches `git_state.head_blobs`,
      whose own docstring documents its single retained, memoised spawn.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Collection, Dict, NamedTuple, Optional, Sequence, Union

from coordinator_core.git.git_objects import _retry_transient_read
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_state import head_blobs
from coordinator_core.git.git_state import read_index as _read_full_index

__all__ = [
    "IndexStatusEntry",
    "IndexIdentity",
    "IndexParseError",
    "IndexV4Unsupported",
    "parse_index_stat",
    "parse_index_identity",
    "scoped_status",
    "diff_index_name_status",
]

_SIGNATURE = b"DIRC"
_SUPPORTED_VERSIONS = (2, 3)
#: flags(2) = 62, EXCLUDING the optional v3 extended-flags halfword --
#: identical layout to `git_state._ENTRY_FIXED_LEN`.
_ENTRY_FIXED_LEN = 62


class IndexStatusEntry(NamedTuple):

    mode: int
    size: int
    mtime: int
    mtime_nsec: int = 0


class IndexIdentity(NamedTuple):

    mode: int
    sha: str
    size: int
    mtime: int
    mtime_nsec: int = 0


class IndexParseError(ValueError):
    pass


class IndexV4Unsupported(IndexParseError):
    pass


def parse_index_stat(repo: Union[str, Path]) -> Dict[str, IndexStatusEntry]:
    return {
        path: IndexStatusEntry(
            mode=e.mode, size=e.size, mtime=e.mtime, mtime_nsec=e.mtime_nsec
        )
        for path, e in parse_index_identity(repo).items()
    }


def parse_index_identity(
    repo: Union[str, Path], wanted: Optional[Collection[str]] = None
) -> Dict[str, IndexIdentity]:
    """ONE walk of `.git/index` yielding the UNION of what the two
    clean/modified axes need per path -- `(mode, sha, size, mtime,
    mtime_nsec)`.

    Neither existing reader returns the union this function needs:
    `git_state.IndexEntry` is `(mode, sha, stage)` and discards the stat
    fields it just read past, while this module's `IndexStatusEntry`
    carries the stat fields and no sha. A caller needing both axes -- "does
    the worktree match the index" (stat, then a content hash on a mismatch)
    and "does the index match HEAD" (sha identity) -- gets both from one
    walk here instead of two.

    `wanted`, when given, is a containment-tested set of repo-relative
    paths: entries outside it are stepped over unpacking ONLY the two bytes
    of `flags` needed to find the next one, and the walk RETURNS EARLY once
    every wanted path has been found. `wanted=None` materialises every
    entry, which is what `parse_index_stat` needs.

    THE EARLY EXIT IS GOVERNED BY SORT ORDER, NOT BY HOW FEW PATHS YOU ASK
    FOR. The index is sorted by path bytes, so the walk can only return once
    the LAST-SORTING wanted path has been passed: one late-sorting path
    forfeits the exit for every other path in the same call, so a scoped
    call is cheaper only in proportion to where the caller's paths happen to
    sort, and it remains O(index) in the worst case. Do NOT quote a scoped
    figure as if `wanted`'s SIZE alone produced it -- the sort position is
    the axis that governs.

    NO BINARY SEARCH IS AVAILABLE, and it is worth recording why so it is
    not re-proposed: entries are variable-length (the name is NUL-terminated
    and padded), so there is no way to address entry `k` without having
    walked the `k-1` before it. Exploiting the sortedness beyond this linear
    exit would need an offset table the wire format does not carry.

    Early exit is a saving, never a semantic difference: a path in `wanted`
    but absent from the index is absent from the result either way, and the
    caller reads that as untracked exactly as it would from a full walk.

    Same refusals as `parse_index_stat`: `IndexV4Unsupported` for a v4
    index, `IndexParseError` for a bad signature/version/truncation, and
    `{}` (not a raise) for a genuinely absent index file. NOTE that a
    truncation LATER in the file than an early exit reaches is not
    detected -- an early-exiting caller trades whole-file structural
    validation for the walk it skipped. A caller that needs the file
    validated end to end passes `wanted=None`.

    RAISES `IndexParseError` on any RETURNED entry with `stage != 0`
    (unmerged), mirroring `git_state`'s "a conflicted path must never read
    as staged" rule -- `commit.py::commit_paths` calls this function
    directly with the commit's own pathspec and treats every returned
    entry as an ordinary staged identity, so a conflicted path silently
    passing through here as identical to stage 0 would let a commit land
    mid-conflict. Only checked for entries that pass the `wanted` filter
    (or every entry, when `wanted=None`): a skipped entry costs only the
    two `flags` bytes already read, same trade-off as the truncation note
    above.
    """
    gitdir = resolve_git_dir(repo)
    index_path = gitdir / "index"

    raw = _read_index_bytes_with_retry(index_path)
    if raw is None:
        return {}

    return _parse_index_bytes(raw, index_path=index_path, wanted=wanted)


def _read_index_bytes_with_retry(index_path: Path) -> Optional[bytes]:
    """`.git/index` bytes, `None` when the index genuinely does not exist.

    Delegates to `git_objects._retry_transient_read` -- ONE ladder for every
    reader on this surface. A per-module copy is what left `git_state`'s
    reader bare while this one was covered.

    Before the retry, a single transient `OSError` became `IndexParseError`,
    and `IndexParseError` is what `diverging_paths` collapses to `[]` -- so a
    momentary read failure DISABLED the staged-content guard rather than
    pausing it.
    """
    try:
        return _retry_transient_read(index_path.read_bytes)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IndexParseError(f"could not read {index_path}: {exc}") from exc


def _parse_index_bytes(
    raw: bytes, *, index_path: Path, wanted: Optional[Collection[str]] = None
) -> Dict[str, IndexIdentity]:
    if len(raw) < 12:
        raise IndexParseError(f"{index_path}: truncated header ({len(raw)} bytes)")

    signature = raw[0:4]
    version, entry_count = struct.unpack(">II", raw[4:12])

    if signature != _SIGNATURE:
        raise IndexParseError(
            f"{index_path}: bad signature {signature!r}, expected {_SIGNATURE!r}"
        )
    if version == 4:
        raise IndexV4Unsupported(
            f"{index_path}: index v4 (prefix-compressed names) is refused, "
            "not guessed at -- see module docstring"
        )
    if version not in _SUPPORTED_VERSIONS:
        raise IndexParseError(
            f"{index_path}: unsupported index version {version} "
            f"(supported: {_SUPPORTED_VERSIONS})"
        )

    entries: Dict[str, IndexIdentity] = {}
    offset = 12

    wanted_bytes: Optional[set] = None
    if wanted is not None:
        wanted_bytes = {p.encode("utf-8", "surrogateescape") for p in wanted}
        if not wanted_bytes:
            return {}

    for _ in range(entry_count):
        entry_start = offset
        if offset + _ENTRY_FIXED_LEN > len(raw):
            raise IndexParseError(f"{index_path}: truncated entry at offset {offset}")

        flags = struct.unpack(">H", raw[offset + 60 : offset + 62])[0]
        offset += _ENTRY_FIXED_LEN

        extended = bool(flags & 0x4000)
        stage = (flags >> 12) & 0x3
        name_len_field = flags & 0x0FFF

        if extended:
            if version < 3:
                raise IndexParseError(
                    f"{index_path}: extended-flags bit set on a v{version} index"
                )
            if offset + 2 > len(raw):
                raise IndexParseError(f"{index_path}: truncated extended flags at {offset}")
            offset += 2

        try:
            nul = raw.index(b"\x00", offset)
        except ValueError as exc:
            raise IndexParseError(
                f"{index_path}: unterminated name at offset {offset}"
            ) from exc
        name = raw[offset:nul]
        if name_len_field != 0x0FFF and len(name) != name_len_field:
            raise IndexParseError(
                f"{index_path}: name-length mismatch at offset {entry_start} "
                f"(flags said {name_len_field}, NUL-scan found {len(name)})"
            )
        entry_len = (offset - entry_start) + len(name) + 1
        padding = (8 - (entry_len % 8)) % 8
        offset = nul + 1 + padding

        if wanted_bytes is not None and name not in wanted_bytes:
            continue

        if stage != 0:
            raise IndexParseError(
                f"{index_path}: entry {name!r} is unmerged (stage {stage}); "
                "a conflicted path must never read as staged"
            )

        mtime_sec, mtime_nsec = struct.unpack(
            ">II", raw[entry_start + 8 : entry_start + 16]
        )
        mode = struct.unpack(">I", raw[entry_start + 24 : entry_start + 28])[0]
        size = struct.unpack(">I", raw[entry_start + 36 : entry_start + 40])[0]
        sha = raw[entry_start + 40 : entry_start + 60].hex()
        path = name.decode("utf-8", "surrogateescape")
        entries[path] = IndexIdentity(
            mode=mode, sha=sha, size=size, mtime=mtime_sec, mtime_nsec=mtime_nsec
        )
        if wanted_bytes is not None and len(entries) == len(wanted_bytes):
            break

    return entries


def scoped_status(repo: Union[str, Path], paths: Sequence[str]) -> Dict[str, str]:
    """`{path: verdict}` for `paths` (repo-relative), verdict one of:
    `"clean"`, `"candidate"`, `"deleted"`, `"untracked"`.

    Mirrors git's own `ce_match_stat` fast path: a stat-matching file
    (same size, same mtime-second as the index entry) reads `"clean"`
    WITHOUT its bytes ever being read. A stat MISMATCH (size or mtime
    differs) reads `"candidate"` -- settling it needs a content hash,
    which is the caller's job, not this function's.

    THE MTIME COMPARISON IS TO THE NANOSECOND, not the second. A
    second-granularity compare is the racily-clean hole git's own
    `is_racy_timestamp` exists to plug: a same-size rewrite landing inside
    the index entry's own mtime-second matches on `(size, mtime_sec)` and
    reads a false `"clean"`. That is not hypothetical here -- the archival
    path writes via `os.replace` and asks immediately after, squarely
    inside that window. Git settles a racy entry by hashing the worktree
    bytes; this module cannot (see the negative-spec), so it closes the
    window at the source instead, on the sub-second half of the index's
    8-byte mtime field that a second-granularity read was discarding.

    GUARDED ON A NONZERO STORED `mtime_nsec`. A git that recorded no
    sub-second stat writes `0` there, and comparing that against a real
    worktree nanosecond would read every clean path as `"candidate"` --
    which is NOT the harmless conservative direction it looks like:
    `git_native :: _v2_state_records_chunked` maps `"candidate"` to a
    non-`.` `y`, so a blanket false-`candidate` reads as worktree
    divergence on every freshly-staged path and fails the commit loud.
    A zero therefore falls back to the second-granularity compare, keeping
    the pre-existing (narrower) exposure rather than inventing a wider one.

    A path present in the
    index but absent on disk reads `"deleted"`. A path absent from the
    index entirely reads `"untracked"`, regardless of whether it exists on
    disk (an untracked file that happens to exist is still `"untracked"`;
    this function does not distinguish "untracked, present" from
    "untracked, absent" -- neither shape is staged, and a caller asking
    about its own pathspec already knows which paths it expects to exist).

    Raises `IndexV4Unsupported` / `IndexParseError` exactly as
    `parse_index_stat` does -- this function does not swallow either.
    """
    entries = parse_index_identity(repo, wanted=paths)
    repo_path = Path(repo)
    verdicts: Dict[str, str] = {}

    for rel in paths:
        cached = entries.get(rel)
        try:
            st = (repo_path / rel).stat()
        except OSError:
            verdicts[rel] = "deleted" if cached is not None else "untracked"
            continue

        if cached is None:
            verdicts[rel] = "untracked"
            continue

        stat_matches = cached.size == (st.st_size & 0xFFFFFFFF) and cached.mtime == int(
            st.st_mtime
        )
        if stat_matches and cached.mtime_nsec:
            stat_matches = cached.mtime_nsec == st.st_mtime_ns % 1_000_000_000
        verdicts[rel] = "clean" if stat_matches else "candidate"

    return verdicts


def diff_index_name_status(repo: Union[str, Path], paths: Sequence[str]) -> Dict[str, str]:
    index_snapshot = _read_full_index(repo)
    head_entries = head_blobs(repo, paths)

    verdicts: Dict[str, str] = {}
    for p in paths:
        idx_entry = index_snapshot.get(p)
        head_entry = head_entries.get(p)

        if idx_entry is None and head_entry is None:
            continue
        if idx_entry is None:
            verdicts[p] = "D"
        elif head_entry is None:
            verdicts[p] = "A"
        elif (idx_entry.mode, idx_entry.sha) != head_entry:
            verdicts[p] = "M"

    return verdicts
