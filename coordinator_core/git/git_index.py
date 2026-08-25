"""coordinator_core.git.git_index -- an in-process, scoped reader for the
"is this handful of paths modified/deleted/clean" question a commit-path
caller asks before it decides whether to spawn anything at all.

Why this exists: the census behind
`docs/plans/2026-08-16-one-engine-for-the-whole-box.md` found
`ceremony.scoped_git_commit` spawning THREE shapes of `git status
--porcelain` per commit (`v1`, `v2`, `--untracked-files=all`) to answer a
question about its OWN pathspec, typically a handful of paths. `git status`
is O(worktree) no matter how narrow the pathspec -- it refreshes the whole
index and walks every tracked directory before filtering output down. The
op never needs that: it needs a stat per path plus an index lookup, which
is exactly what git's own `ce_match_stat` fast path does before it
considers hashing a candidate. Measured in
`state/audits/2026-08-23-in-process-scoped-status-spike.py` against this
repo's own worktree; see `state/dispatch-briefs/2026-08-23-the-scoped-commit-rebuilt-from-first-principles/C2.md`
for the promotion brief.

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

`diff_index_name_status` (C3, `state/dispatch-briefs/2026-08-23-the-scoped-
commit-rebuilt-from-first-principles/C3.md`) is the exception to the
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
from typing import Dict, NamedTuple, Sequence, Union

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_state import head_blobs
from coordinator_core.git.git_state import read_index as _read_full_index

__all__ = [
    "IndexStatusEntry",
    "IndexParseError",
    "IndexV4Unsupported",
    "parse_index_stat",
    "scoped_status",
    "diff_index_name_status",
]

_SIGNATURE = b"DIRC"
_SUPPORTED_VERSIONS = (2, 3)
#: ctime(8) mtime(8) dev(4) ino(4) mode(4) uid(4) gid(4) size(4) sha1(20)
#: flags(2) = 62, EXCLUDING the optional v3 extended-flags halfword --
#: identical layout to `git_state._ENTRY_FIXED_LEN`.
_ENTRY_FIXED_LEN = 62


class IndexStatusEntry(NamedTuple):
    """One index entry's stat identity: `(mode, size, mtime_seconds)`."""

    mode: int
    size: int
    mtime: int


class IndexParseError(ValueError):
    """Raised instead of ever returning a partial or empty result for a
    malformed or truncated index. See module negative-spec.
    """


class IndexV4Unsupported(IndexParseError):
    """Raised for an index v4 file specifically -- prefix-compressed names
    are refused rather than guessed at. See module docstring.
    """


def parse_index_stat(repo: Union[str, Path]) -> Dict[str, IndexStatusEntry]:
    """Parse `resolve_git_dir(repo)/index` directly (no `git` spawn) into
    `{path: IndexStatusEntry(mode, size, mtime)}`.

    Handles index v2 and v3 (extended per-entry flags) only. Raises
    `IndexV4Unsupported` for a v4 index, and `IndexParseError` for any
    other bad signature, unsupported version, or structural truncation --
    never returns a partial or empty dict for a malformed file.

    A genuinely absent index file (no `.git/index` at all -- an unborn
    repo before the first `git add`) is the one legitimate empty-result
    case: this returns `{}` rather than raising.
    """
    gitdir = resolve_git_dir(repo)
    index_path = gitdir / "index"

    try:
        raw = index_path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise IndexParseError(f"could not read {index_path}: {exc}") from exc

    return _parse_index_bytes(raw, index_path=index_path)


def _parse_index_bytes(raw: bytes, *, index_path: Path) -> Dict[str, IndexStatusEntry]:
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

    entries: Dict[str, IndexStatusEntry] = {}
    offset = 12

    for _ in range(entry_count):
        entry_start = offset
        if offset + _ENTRY_FIXED_LEN > len(raw):
            raise IndexParseError(f"{index_path}: truncated entry at offset {offset}")

        mtime_sec = struct.unpack(">I", raw[offset + 8 : offset + 12])[0]
        mode = struct.unpack(">I", raw[offset + 24 : offset + 28])[0]
        size = struct.unpack(">I", raw[offset + 36 : offset + 40])[0]
        flags = struct.unpack(">H", raw[offset + 60 : offset + 62])[0]
        offset += _ENTRY_FIXED_LEN

        extended = bool(flags & 0x4000)
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

        path = name.decode("utf-8", "surrogateescape")
        entries[path] = IndexStatusEntry(mode=mode, size=size, mtime=mtime_sec)

    return entries


def scoped_status(repo: Union[str, Path], paths: Sequence[str]) -> Dict[str, str]:
    """`{path: verdict}` for `paths` (repo-relative), verdict one of:
    `"clean"`, `"candidate"`, `"deleted"`, `"untracked"`.

    Mirrors git's own `ce_match_stat` fast path: a stat-matching file
    (same size, same mtime-second as the index entry) reads `"clean"`
    WITHOUT its bytes ever being read. A stat MISMATCH (size or mtime
    differs) reads `"candidate"` -- settling it needs a content hash,
    which is the caller's job, not this function's. A path present in the
    index but absent on disk reads `"deleted"`. A path absent from the
    index entirely reads `"untracked"`, regardless of whether it exists on
    disk (an untracked file that happens to exist is still `"untracked"`;
    this function does not distinguish "untracked, present" from
    "untracked, absent" -- neither shape is staged, and a caller asking
    about its own pathspec already knows which paths it expects to exist).

    Raises `IndexV4Unsupported` / `IndexParseError` exactly as
    `parse_index_stat` does -- this function does not swallow either.
    """
    entries = parse_index_stat(repo)
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

        if cached.size == (st.st_size & 0xFFFFFFFF) and cached.mtime == int(st.st_mtime):
            verdicts[rel] = "clean"
        else:
            verdicts[rel] = "candidate"

    return verdicts


def diff_index_name_status(repo: Union[str, Path], paths: Sequence[str]) -> Dict[str, str]:
    """`{path: "A"|"M"|"D"}` for `paths` (repo-relative) -- the in-process,
    pathspec-scoped replacement for `git diff --cached --name-status`
    (without `--find-renames`; a caller needing rename pairing keeps that
    as its own concern -- see below). An unchanged path is simply absent
    from the result, exactly like git's own name-status output never lists
    a clean path.

    Comparison is by `(mode, sha)` identity only -- both sides are
    already-computed git object identities, so this never hashes worktree
    bytes and never opens a blob's content. The staged side comes from
    `coordinator_core.git.git_state.read_index` (the FULL v2/v3/v4 index
    parse, including sha -- this module's OWN `parse_index_stat` is the
    wrong tool here: it deliberately carries no sha and refuses v4
    outright, neither of which a content-identity diff can tolerate). The
    HEAD side comes from `git_state.head_blobs`, the one retained spawn in
    the git package, already scoped to `paths` and memoised per `(repo,
    head_sha, paths)` by that module -- this function spawns nothing of
    its own.

    Verdicts:
      staged only (no HEAD counterpart)             -> `"A"`
      HEAD only (no staged counterpart)              -> `"D"`
      present on both sides, `(mode, sha)` differs   -> `"M"`
      present on both sides, `(mode, sha)` identical -> absent from result

    NO cache of its own -- every call re-reads the index fresh (via
    `read_index`'s own "no cache" negative-spec) and calls `head_blobs`
    fresh every time, relying entirely on THAT module's memoisation for
    spawn-avoidance. This matters for a specific ordering property: the
    dead `diff --cached --name-status` call this replaces was invoked
    TWICE per commit, with a `git add` landing between the two calls, and
    the two calls are NOT duplicates -- the second must observe the newly
    staged path. A single cached diff result served to both call sites
    would silently answer the second call with pre-`git add` staged state,
    a correctness bug already declined once on this surface. A caller
    wanting the post-`git add` picture MUST call this function again after
    staging, not reuse an earlier result.

    Raises `git_state.IndexParseError` exactly as `read_index` does for a
    malformed, unsupported, or unmerged index -- never returns a partial
    or silently-wrong result for one.
    """
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
