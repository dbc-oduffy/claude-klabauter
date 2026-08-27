"""coordinator_core.git.git_state -- an in-process reader for the pieces of
git state a commit-path caller actually needs (staged index, HEAD sha, HEAD
tree blobs). `head_blobs` (HEAD tree blobs) was, until 2026-08-26 (C2b,
docs/dispatch-briefs/2026-08-26-the-commit-op-stops-asking-git-eleven-times/
C2b.md), this module's one deliberately retained spawn (`git ls-tree`); it
now routes through `read_tree_spine`'s in-process tree-spine walk instead --
see that function's own call site in `head_blobs` for the mode-admission
rule a former `ls-tree` consumer still needs (gitlink `160000` is a blob-
equivalent leaf, not a directory to descend into).

Why this exists: the census behind
`docs/plans/2026-08-16-one-engine-for-the-whole-box.md` found the commit
path spawning `git ls-files -s`, `git status`, `git cat-file` and
`git rev-parse HEAD` repeatedly per invocation to answer questions that are
plain file reads once the index format is known. This module hand-parses
`.git/index` (v2/v3/v4) and `.git/HEAD` directly, matching
`git ls-files -s` byte-for-byte over this repo's own 31,520-entry index
(0 keys only-in-git, 0 keys only-in-parse, 0 value mismatches) at 24.1ms
against 319ms wall for the spawn it replaces. See
`state/dispatch-briefs/2026-08-21-the-commit-path-reads-git-state-without-spawning-git/C1.md`
for the measurement and the reviewer-gate ACs it binds.

MANDATORY REUSE, BLOCKING:
    - Path resolution routes through `coordinator_core.git.git_dir`, never a
      hand-joined `<repo_root>/.git/...`. `index` and `HEAD` are
      WORKTREE-PRIVATE (`resolve_git_dir`); `refs/heads/*` (loose) and
      `packed-refs` both live in the SHARED common dir
      (`resolve_git_common_dir`) -- using the private dir for either lookup
      works on a plain clone (private == common there) and mis-resolves
      silently only in a linked worktree.

THE WORKTREE HASH DOES NOT WORK, FOR RAW BYTES -- measured, not suspected.
This module never hashes on-disk bytes AS-IS and compares the result to a
git OID (`core.autocrlf`/`core.filemode`/smudge filters make that NAIVE
comparison wrong for a meaningful fraction of paths on this repo -- 326 of
400 clean tracked files MISMATCH here under `core.autocrlf=true`, the
reverted `da156a723` incident). No `worktree_blob()` is provided in THIS
module, deliberately: a caller here needing a worktree-vs-git answer keeps
its spawn.

SATISFIED, NOT LIFTED, ELSEWHERE (C3e, 2026-08-26, docs/dispatch-briefs/
2026-08-26-the-commit-op-stops-asking-git-eleven-times/C3e.md): what is
forbidden is hashing RAW worktree bytes. `coordinator_core.git.content_
hash.content_matches_index_sha` hashes NORMALIZED bytes -- git's own
`core.autocrlf=true`, default-attribute checkin-side transform, reproduced
in process and verified byte-identical against real `git hash-object` over
14 shapes -- which is a different operation with a different correctness
argument, and it DECLINES (returns `None`, caller keeps its spawn) for
every path outside the exact precondition set that verification covers
(`autocrlf` not resolved to exactly `true`, any repo-local `text`/`-text`/
`eol=` attribute pin, any `filter=` clean pipeline, or a read failure).
`coordinator_core/git/divergence.py :: diverging_paths` is the one
consumer, settling only its stat-mismatch "candidate" paths this way and
falling back to the spawn for every DECLINE. Do not re-open the RAW-bytes
question this section still forbids, and do not add a stat-based worktree
comparison to THIS module -- the normalize-then-hash path lives in
`content_hash.py`, not here, and stays scoped to the one caller above.

Negative-spec:
    - NO process-lifetime cache, NO memoisation keyed on repo root. A caller
      needing a second observation (a compare-and-swap, e.g.
      `_agree_branch_cas_refusal`) takes its pre-state as a value parameter
      and calls this module fresh for the current side; a process-lifetime
      cache here would collapse that CAS into a single stale look with the
      guard still reading green -- the exact 2026-08-14 partial-stage
      incident this module exists to not repeat. `read_index` therefore
      stats the index file FRESH on every call BY DEFAULT (see
      `IndexSnapshot.stat_identity`).
    - The ONE exception (C2, 2026-08-26,
      docs/plans/2026-08-26-the-close-path-spends-its-last-known-levers.md):
      `index_read_cache_scope()` opens a cache scoped to a single call's
      lifetime (a `contextvars.ContextVar`, never a module-level/process
      cache), for callers that read the SAME on-disk index multiple times
      within one commit and have no need to observe a mid-call write. A
      caller that DOES need that (the compare-and-swap re-read) passes
      `read_index(repo, fresh=True)`, which always stats+parses regardless
      of an open scope and never populates it -- `_agree_branch_cas_refusal`
      is the one production caller that does this, by design (see AC3 of
      the plan above).
    - Does NOT return `{}` on any parse failure. Signature mismatch,
      unsupported version, a split index, or any unmerged (`stage > 0`)
      entry all RAISE `IndexParseError` -- an empty dict reads as "nothing
      is staged", the worst possible wrong answer for every caller here.
"""

from __future__ import annotations

import contextvars
import os
import struct
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, NamedTuple, Optional, Sequence, Tuple, Union

from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.git_objects import _read_object
from coordinator_core.git.run import run_git

__all__ = [
    "IndexEntry",
    "StatIdentity",
    "IndexSnapshot",
    "IndexParseError",
    "read_index",
    "read_index_stat_identity",
    "index_read_cache_scope",
    "head_branch",
    "head_sha",
    "head_tree_sha",
    "read_tree_spine",
    "head_blobs",
]

_SIGNATURE = b"DIRC"
_SUPPORTED_VERSIONS = (2, 3, 4)
_SHA_LEN = 20  # sha1 only; a sha256 index carries a different OID length
#: The symref prefix `head_branch` strips to match `git rev-parse
#: --abbrev-ref HEAD`. A symref outside this namespace is returned whole --
#: git does not abbreviate those to a bare name either.
_HEADS_PREFIX = "refs/heads/"

#: Fixed-width fields preceding the name in every entry, EXCLUDING the
#: optional v3+ extended-flags halfword: ctime(8) + mtime(8) + dev(4) +
#: ino(4) + mode(4) + uid(4) + gid(4) + size(4) + sha1(20) + flags(2) = 62.
_ENTRY_FIXED_LEN = 62


class IndexEntry(NamedTuple):
    """One staged path's identity, as `git ls-files -s` would print it."""

    mode: int
    sha: str
    stage: int


class StatIdentity(NamedTuple):
    """The index FILE's stat identity at the moment it was read -- exists so
    a caller performing a compare-and-swap can prove it re-read rather than
    reused a stale snapshot. See this module's "no cache" negative-spec.
    """

    st_mtime_ns: int
    st_size: int
    st_ino: int


class IndexSnapshot(dict):
    """`{path: IndexEntry}`, plus the `.stat_identity` the index file carried
    when this snapshot was built. A plain `dict` subclass so every existing
    `{path: IndexEntry}` consumer keeps working unmodified.
    """

    def __init__(self, entries: Dict[str, IndexEntry], stat_identity: Optional[StatIdentity]):
        super().__init__(entries)
        self.stat_identity = stat_identity


class IndexParseError(ValueError):
    """Raised instead of ever returning a partial or empty result for a
    malformed, unsupported, or unmerged index. See module negative-spec.
    """


#: Scoped-to-one-call `read_index` cache (C2). `None` outside any
#: `index_read_cache_scope()` -- the ordinary, still-default, fully-fresh
#: path. A `contextvars.ContextVar` rather than a module global so an async/
#: threaded caller never leaks one call's cache into a concurrent one on the
#: same process; keyed on the resolved index PATH (not `repo`) so two `repo`
#: spellings of the same worktree still share a hit.
_INDEX_CALL_CACHE: "contextvars.ContextVar[Optional[Dict[Path, IndexSnapshot]]]" = (
    contextvars.ContextVar("_index_call_cache", default=None)
)


@contextmanager
def index_read_cache_scope() -> Iterator[None]:
    """Open a `read_index` cache for the lifetime of this `with` block only.

    Every ordinary (`fresh=False`, the default) `read_index(repo)` call made
    while this scope is open, for the SAME resolved index path, returns the
    snapshot from the FIRST such call in this scope rather than re-reading
    and re-parsing `.git/index` -- see AC3 of
    docs/plans/2026-08-26-the-close-path-spends-its-last-known-levers.md.

    Never nest this with the intent of sharing one cache across two
    unrelated commits -- each call to this context manager opens its OWN
    fresh, empty cache (nesting just shadows the outer one for the inner
    block's duration, then restores it on exit); there is no cross-call
    persistence anywhere in this module, by design (see module negative-
    spec). `read_index(repo, fresh=True)` -- used by
    `_agree_branch_cas_refusal`'s re-observation -- ignores this scope
    entirely: it neither reads from nor writes into it.
    """
    token = _INDEX_CALL_CACHE.set({})
    try:
        yield
    finally:
        _INDEX_CALL_CACHE.reset(token)


def read_index(repo: Union[str, Path], *, fresh: bool = False) -> IndexSnapshot:
    """Parse `resolve_git_dir(repo)/index` directly (no `git` spawn) into an
    `IndexSnapshot` -- `{path: IndexEntry(mode, sha, stage)}` plus the index
    file's `stat_identity`.

    By default (`fresh=False`) this stats+parses the index file FRESH on
    every call UNLESS an `index_read_cache_scope()` is currently open, in
    which case a prior call THIS SCOPE already made for the same resolved
    index path is returned instead of re-reading the file (C2). Pass
    `fresh=True` to force a real disk read regardless of any open scope,
    and to skip populating it -- the compare-and-swap re-observation
    (`_agree_branch_cas_refusal`) always does this; see module negative-
    spec and `index_read_cache_scope`'s own docstring.

    Handles index v2, v3 (extended per-entry flags) and v4 (name
    prefix-compression). Raises `IndexParseError` -- never returns `{}` --
    on: a bad signature, an unsupported version, a split index (a `link`
    extension or a sibling `sharedindex.*` file), any entry with
    `stage > 0` (unmerged; a conflicted path must never read as "staged"),
    or a walk that does not land exactly on the start of the extension
    region.

    A genuinely absent index file (no `.git/index` at all -- an unborn repo
    before the first `git add`) is the one legitimate empty-result case:
    there is truly nothing staged, so this returns an empty `IndexSnapshot`
    with `stat_identity=None` rather than raising.
    """
    gitdir = resolve_git_dir(repo)
    index_path = gitdir / "index"

    cache = None if fresh else _INDEX_CALL_CACHE.get()
    if cache is not None:
        cached = cache.get(index_path)
        if cached is not None:
            return cached

    try:
        raw = index_path.read_bytes()
    except FileNotFoundError:
        snapshot = IndexSnapshot({}, None)
        if cache is not None:
            cache[index_path] = snapshot
        return snapshot
    except OSError as exc:
        raise IndexParseError(f"could not read {index_path}: {exc}") from exc

    for sibling in _iter_sharedindex_siblings(gitdir):
        raise IndexParseError(
            f"split index detected ({sibling.name} present alongside "
            f"{index_path}); this module refuses to guess which half is "
            "authoritative"
        )

    st = index_path.stat()
    stat_identity = StatIdentity(
        st_mtime_ns=st.st_mtime_ns, st_size=st.st_size, st_ino=st.st_ino
    )

    entries = _parse_index_bytes(raw, index_path=index_path)
    snapshot = IndexSnapshot(entries, stat_identity)
    if cache is not None:
        cache[index_path] = snapshot
    return snapshot


def read_index_stat_identity(repo: Union[str, Path]) -> Optional[StatIdentity]:
    """`read_index(repo, fresh=True).stat_identity` WITHOUT reading or
    parsing the index body -- for a caller (a compare-and-swap re-
    observation) that only ever consulted `.stat_identity` and threw the
    parsed entries away. `IndexSnapshot.stat_identity` (see that class) is
    built purely from `index_path.stat()` -- three integers off the
    filesystem -- so a caller that never touches `IndexSnapshot`'s `{path:
    IndexEntry}` body has no reason to pay `_parse_index_bytes`'s full
    five-figure-entry walk to obtain it.

    Negative-spec (mirrors this module's own, see the module docstring):
        - UNCONDITIONALLY FRESH. This function never consults
          `_INDEX_CALL_CACHE` and never populates it -- there is no
          `fresh` parameter, because there is no non-fresh mode to opt out
          of. A cached stat identity here would defeat the one property a
          CAS re-observation needs: proof that THIS call, not a memoised
          one, touched the filesystem just now.
        - Same split-index refusal as `read_index`: a `sharedindex.*`
          sibling next to the index file raises `IndexParseError`, same as
          the full reader. A CAS re-observation that silently succeeded
          against a split index would be a new instance of the exact
          defect C1 closed (a re-observation that cannot actually fail) --
          refusing the same way `read_index` does is the conservative
          choice, not a stricter one invented here.
        - Returns `None` for a genuinely absent index file (no `.git/index`
          at all -- an unborn repo), matching `read_index`'s own
          `stat_identity=None` fold for that state, not a raise.

    Reads `resolve_git_dir(repo)/index` (worktree-private, per this
    module's own MANDATORY REUSE note) via a single `Path.stat()` call --
    no `read_bytes()`, no `_parse_index_bytes()`.
    """
    gitdir = resolve_git_dir(repo)
    index_path = gitdir / "index"

    for sibling in _iter_sharedindex_siblings(gitdir):
        raise IndexParseError(
            f"split index detected ({sibling.name} present alongside "
            f"{index_path}); this module refuses to guess which half is "
            "authoritative"
        )

    try:
        st = index_path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise IndexParseError(f"could not read {index_path}: {exc}") from exc

    return StatIdentity(st_mtime_ns=st.st_mtime_ns, st_size=st.st_size, st_ino=st.st_ino)


def _iter_sharedindex_siblings(gitdir: Path):
    try:
        yield from gitdir.glob("sharedindex.*")
    except OSError:
        return


def _parse_index_bytes(raw: bytes, *, index_path: Path) -> Dict[str, IndexEntry]:
    if len(raw) < 12:
        raise IndexParseError(f"{index_path}: truncated header ({len(raw)} bytes)")

    signature = raw[0:4]
    version, entry_count = struct.unpack(">II", raw[4:12])

    if signature != _SIGNATURE:
        raise IndexParseError(
            f"{index_path}: bad signature {signature!r}, expected {_SIGNATURE!r}"
        )
    if version not in _SUPPORTED_VERSIONS:
        raise IndexParseError(
            f"{index_path}: unsupported index version {version} "
            f"(supported: {_SUPPORTED_VERSIONS})"
        )

    entries: Dict[str, IndexEntry] = {}
    offset = 12
    prev_name = b""

    for _ in range(entry_count):
        entry_start = offset
        if offset + _ENTRY_FIXED_LEN > len(raw):
            raise IndexParseError(f"{index_path}: truncated entry at offset {offset}")

        mode = struct.unpack(">I", raw[offset + 24 : offset + 28])[0]
        sha = raw[offset + 40 : offset + 60].hex()
        flags = struct.unpack(">H", raw[offset + 60 : offset + 62])[0]
        offset += _ENTRY_FIXED_LEN

        stage = (flags >> 12) & 0x3
        extended = bool(flags & 0x4000)
        name_len_field = flags & 0x0FFF

        if extended:
            if version < 3:
                raise IndexParseError(
                    f"{index_path}: extended-flags bit set on a v{version} index"
                )
            if offset + 2 > len(raw):
                raise IndexParseError(f"{index_path}: truncated extended flags at {offset}")
            offset += 2  # extra flags carried but not needed by any caller here

        if version == 4:
            strip_len, offset = _decode_varint(raw, offset, index_path=index_path)
            nul = raw.index(b"\x00", offset)
            suffix = raw[offset:nul]
            base = prev_name[: len(prev_name) - strip_len] if strip_len else prev_name
            name = base + suffix
            offset = nul + 1
            prev_name = name
        else:
            nul = raw.index(b"\x00", offset)
            name = raw[offset:nul]
            if name_len_field != 0x0FFF and len(name) != name_len_field:
                raise IndexParseError(
                    f"{index_path}: name-length mismatch at offset {entry_start} "
                    f"(flags said {name_len_field}, NUL-scan found {len(name)})"
                )
            entry_len = (offset - entry_start) + len(name) + 1
            padding = (8 - (entry_len % 8)) % 8
            offset = nul + 1 + padding

        if stage != 0:
            raise IndexParseError(
                f"{index_path}: entry {name!r} is unmerged (stage {stage}); "
                "a conflicted path must never read as staged"
            )

        path = name.decode("utf-8", "surrogateescape")
        entries[path] = IndexEntry(mode=mode, sha=sha, stage=stage)

    _extension_region_start(raw, offset, index_path=index_path)
    _reject_link_extension(raw, offset, index_path=index_path)

    return entries


def _extension_region_start(raw: bytes, offset: int, *, index_path: Path) -> None:
    """Validate that `offset` (the entry-walk's landing point) lands on a
    plausible extension-region boundary: either exactly at the trailing
    checksum (no extensions) or at the start of a well-formed
    `(signature, size, data)` extension chain that runs cleanly to the
    checksum. Enforcement is entirely by RAISE (`IndexParseError` on a
    truncated header, a malformed signature, or an extension that overruns
    the checksum) -- there is no return value a caller can compare against
    to detect a bad boundary; not raising IS the pass signal. The return
    value here is not meaningful and callers must not compare against it.
    """
    remaining = len(raw) - offset
    if remaining < _SHA_LEN:
        raise IndexParseError(
            f"{index_path}: only {remaining} bytes left at offset {offset}, "
            f"short of the trailing {_SHA_LEN}-byte checksum"
        )
    if remaining == _SHA_LEN:
        return

    cursor = offset
    while len(raw) - cursor > _SHA_LEN:
        if len(raw) - cursor < 8:
            raise IndexParseError(
                f"{index_path}: truncated extension header at offset {cursor}"
            )
        sig = raw[cursor : cursor + 4]
        if not sig.isalpha() or not sig.isupper():
            raise IndexParseError(
                f"{index_path}: offset {cursor} is not a well-formed extension "
                f"signature ({sig!r})"
            )
        size = struct.unpack(">I", raw[cursor + 4 : cursor + 8])[0]
        cursor += 8 + size
        if cursor > len(raw) - _SHA_LEN:
            raise IndexParseError(
                f"{index_path}: extension {sig!r} at offset {offset} overruns "
                "the trailing checksum"
            )


def _reject_link_extension(raw: bytes, offset: int, *, index_path: Path) -> None:
    cursor = offset
    while len(raw) - cursor > _SHA_LEN:
        sig = raw[cursor : cursor + 4]
        size = struct.unpack(">I", raw[cursor + 4 : cursor + 8])[0]
        if sig == b"link":
            raise IndexParseError(
                f"{index_path}: 'link' extension present -- this is a split "
                "index and this module refuses to guess which half is "
                "authoritative"
            )
        cursor += 8 + size


def _decode_varint(raw: bytes, offset: int, *, index_path: Path) -> Tuple[int, int]:
    """Git's index-v4 name-compression varint: MSB-set means "more bytes",
    and each continuation byte contributes `((val + 1) << 7) | (byte & 0x7f)`
    -- NOT the plain "concatenate low 7 bits" LEB128 shape. See
    `Documentation/technical/index-format.txt`'s `decode_varint`.
    """
    if offset >= len(raw):
        raise IndexParseError(f"{index_path}: truncated v4 name-offset varint at {offset}")
    c = raw[offset]
    offset += 1
    val = c & 0x7F
    while c & 0x80:
        if offset >= len(raw):
            raise IndexParseError(f"{index_path}: truncated v4 name-offset varint at {offset}")
        c = raw[offset]
        offset += 1
        val = ((val + 1) << 7) | (c & 0x7F)
    return val, offset


def head_branch(repo: Union[str, Path]) -> Optional[str]:
    """The current branch NAME -- what `git rev-parse --abbrev-ref HEAD`
    reports -- read from `resolve_git_dir(repo)/HEAD` with no `git` spawn.

    HEAD is worktree-PRIVATE, so this reads the private gitdir and never the
    common dir (module docstring's first bullet); a linked worktree on its own
    branch must not report the main worktree's.

    Returns:
        - `"<name>"` for a symref into `refs/heads/` -- the branch name with
          the prefix stripped, matching `--abbrev-ref`'s output. This is the
          only reader here that is correct on an UNBORN branch: no ref file
          exists yet, but HEAD still names it, and `--abbrev-ref` reports it
          too. `head_sha` returns `None` for that same state, deliberately --
          the two answer different questions and disagree here on purpose.
        - `"HEAD"` for a detached HEAD, which is `--abbrev-ref`'s own literal
          answer for that state, not a sentinel invented here.
        - the full ref for a symref outside `refs/heads/`, unstripped.
        - `None` only when HEAD is unreadable or empty. Never raises.

    Deliberately NOT folded into `head_sha`: that function resolves a symref
    THROUGH to a sha and needs the common dir plus a packed-refs fallback to
    do it; this one stops at HEAD's own contents and touches exactly one file.
    A caller wanting both pays one extra small read rather than making either
    signature three-valued in a second dimension.
    """
    gitdir = resolve_git_dir(repo)
    try:
        content = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not content:
        return None

    if not content.startswith("ref:"):
        return "HEAD"

    ref = content[len("ref:") :].strip()
    if not ref:
        return None
    if ref.startswith(_HEADS_PREFIX):
        return ref[len(_HEADS_PREFIX) :] or None
    return ref


def head_sha(repo: Union[str, Path]) -> Optional[str]:
    """Read `resolve_git_dir(repo)/HEAD`, following ONE ref hop -- HEAD's
    own `ref: refs/heads/<x>` line to `refs/heads/<x>`'s content, read from
    `resolve_git_common_dir(repo)` (refs are never worktree-private; only
    `HEAD` itself is) -- and falling back to `resolve_git_common_dir(repo)/
    packed-refs` when the loose ref file does not exist. Returns `None` for
    a detached-nothing/unborn branch (a symref whose target has no loose
    ref and no packed-refs entry), never raises on a missing HEAD.
    """
    gitdir = resolve_git_dir(repo)
    try:
        content = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not content.startswith("ref:"):
        return content or None

    ref = content[len("ref:") :].strip()
    common_dir = resolve_git_common_dir(repo)
    try:
        sha = (common_dir / ref).read_text(encoding="utf-8").strip()
        if sha:
            return sha
    except OSError:
        pass

    try:
        packed_text = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return None

    for line in packed_text.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, ref_name = line.partition(" ")
        if ref_name == ref:
            return sha
    return None


def head_tree_sha(repo: Union[str, Path]) -> Optional[str]:
    """`head_sha(repo)`, then read the HEAD commit object (loose or packed,
    via `coordinator_core.git.git_objects._read_object`) and parse its
    `tree <sha>` first line. Returns `None` -- never raises -- for an
    unresolvable HEAD, an unreadable/corrupt commit object, or a HEAD sha
    that resolves to something other than a `commit` object; a caller sees
    `None` as "take the ladder", same as every other three-valued reader in
    this module.
    """
    sha = head_sha(repo)
    if sha is None:
        return None
    common_dir = resolve_git_common_dir(repo)
    result = _read_object(common_dir, sha)
    if result is None:
        return None
    otype, payload = result
    if otype != "commit":
        return None
    first_line, _, _ = payload.partition(b"\n")
    if not first_line.startswith(b"tree "):
        return None
    tree_sha = first_line[len(b"tree ") :].decode("ascii", errors="replace").strip()
    if len(tree_sha) != 40:
        return None
    return tree_sha


def _parse_tree_entries(payload: bytes) -> Optional[Dict[str, Tuple[int, str]]]:
    """`<mode> <name>\\0<20-byte-sha>` repeated -- the inverse of
    `git_objects.build_tree`'s emission. Returns `None` (never raises) on
    any structural surprise -- a missing separator, a non-octal mode, a
    short trailing sha -- so a corrupt/unexpected tree object reads as
    "unreadable", not as a partial or wrong entry set.
    """
    entries: Dict[str, Tuple[int, str]] = {}
    pos = 0
    n = len(payload)
    try:
        while pos < n:
            sp = payload.index(b" ", pos)
            mode = int(payload[pos:sp], 8)
            nul = payload.index(b"\x00", sp + 1)
            name = payload[sp + 1 : nul].decode("utf-8", "surrogateescape")
            sha_bytes = payload[nul + 1 : nul + 21]
            if len(sha_bytes) != 20:
                return None
            entries[name] = (mode, sha_bytes.hex())
            pos = nul + 21
    except (ValueError, IndexError):
        return None
    return entries


def read_tree_spine(
    repo: Union[str, Path], paths: Sequence[str]
) -> Optional[Dict[str, Dict[str, Tuple[int, str]]]]:
    """For `paths` (repo-relative), returns the tree objects along each
    path's directory spine: `{dir: {name: (mode, sha)}}`, root keyed as
    `""`, root always included. Walks ONLY the directory components each
    path actually needs -- O(path depth), never O(repo) -- reading a given
    directory's tree object at most once even when multiple `paths` share a
    prefix. Never touches `.git/index`.

    A path's own leaf component (its last `/`-segment) is never descended
    into regardless of its mode -- a gitlink (`160000`) or any other
    non-tree entry there is simply the value the caller finds in its
    parent directory's dict, not a directory this function opens.

    Returns `None` -- take the ladder -- for an unresolvable HEAD or any
    unreadable/corrupt tree object encountered along a spine (including the
    root); never returns a partial spine silently missing a directory it
    could not read.
    """
    root_sha = head_tree_sha(repo)
    if root_sha is None:
        return None
    common_dir = resolve_git_common_dir(repo)
    spine: Dict[str, Dict[str, Tuple[int, str]]] = {}
    dir_sha: Dict[str, str] = {"": root_sha}

    def _load_dir(dirpath: str) -> Optional[Dict[str, Tuple[int, str]]]:
        if dirpath in spine:
            return spine[dirpath]
        sha = dir_sha.get(dirpath)
        if sha is None:
            return None
        result = _read_object(common_dir, sha)
        if result is None:
            return None
        otype, payload = result
        if otype != "tree":
            return None
        entries = _parse_tree_entries(payload)
        if entries is None:
            return None
        spine[dirpath] = entries
        return entries

    if _load_dir("") is None:
        return None

    for path in paths:
        if not path:
            continue
        parts = path.split("/")
        cur_dir: Optional[str] = ""
        for part in parts[:-1]:
            entries = _load_dir(cur_dir)
            if entries is None:
                return None
            entry = entries.get(part)
            if entry is None or entry[0] != 0o40000:
                cur_dir = None
                break
            cur_dir = f"{cur_dir}/{part}" if cur_dir else part
            dir_sha[cur_dir] = entry[1]
        if cur_dir is not None:
            if _load_dir(cur_dir) is None:
                return None

    return spine


#: `head_blobs` memo, keyed `(repo, head_sha, paths)`. See that function for why
#: the key makes this correct without invalidation. Bounded because the warm
#: engine is long-lived and every distinct HEAD leaves its entries behind
#: unreachable-but-resident; LRU eviction by insertion order keeps the working
#: set (one HEAD, a handful of pathspecs) and drops the history. Deliberately
#: NOT a `functools.lru_cache`: the key includes a value read at call time
#: (`head_sha`), which a decorator over the public signature cannot see.
_HEAD_BLOBS_CACHE: "OrderedDict[Tuple[str, str, Tuple[str, ...]], Dict[str, Tuple[int, str]]]" = OrderedDict()
_HEAD_BLOBS_CACHE_MAX = 64


def head_blobs(repo: Union[str, Path], paths: Sequence[str]) -> Dict[str, Tuple[int, str]]:
    """`{path: (mode, sha)}` for `paths` as they exist in HEAD's tree.

    Routes through `read_tree_spine` -- an in-process walk of only the
    directory components `paths` actually need, via
    `coordinator_core.git.git_objects._read_object` -- rather than spawning
    `git ls-tree` (2026-08-26, C2b of docs/dispatch-briefs/2026-08-26-the-
    commit-op-stops-asking-git-eleven-times/C2b.md; `read_tree_spine` itself
    was extracted in C2, but this call site kept spawning `ls-tree` until
    now). No `git` process, and no Windows argv-length concern -- there is
    no argv to chunk once the walk never leaves this process.

    A path's own leaf entry is taken directly from its PARENT directory's
    tree dict (never descended into, regardless of its mode -- matching
    `read_tree_spine`'s own contract for a path's last `/`-segment). Mode
    `0o40000` (a directory/tree entry -- reachable only if a caller passes a
    directory pathspec) is excluded; every other mode -- a regular file, a
    symlink (`120000`), or a gitlink/submodule (`160000`) -- is admitted
    as a leaf, mirroring the former `git ls-tree` reader's own "blob or
    commit object type" admission rule (see `test_commit_gates.py`'s
    `gitlink_160000` regression: a `160000` entry MUST be admitted here,
    never silently dropped as "not a blob").

    A path absent from HEAD (untracked, repo has no commits yet, or HEAD's
    own tree is unreadable/corrupt) is simply absent from the result --
    never raises.
    """
    paths = [p for p in paths if p]
    if not paths:
        return {}

    # MEMOISED ON HEAD's OWN SHA, which makes the cache correct by
    # construction rather than by discipline: this function's whole answer is
    # a projection of HEAD's tree, so two calls that agree on `repo`, `paths`
    # and `head_sha()` cannot disagree on the result. Anything that could
    # change the answer -- a commit here, a peer's commit, a branch switch,
    # a reset -- moves HEAD, changes the key, and misses. Nothing invalidates
    # by hand, so nothing can forget to.
    #
    # Why it is worth a cache at all: a single `ceremony.scoped_git_commit`
    # called this three times on identical arguments (measured 2026-08-23 --
    # `_reject_stale_index_paths`, `commit_gates.dirty_tree_gate`, and
    # `git_native._head_blobs`), and on this box a spawn IS the cost of the
    # call (`git --version`, doing nothing, ranges 15.3ms to 279.3ms under
    # the load norm). Three identical `ls-tree` spawns per commit is a
    # missing cache, not a mechanism that needs rebuilding.
    #
    # `head_sha()` is spawn-free (it reads `.git/HEAD` plus the loose ref or
    # `packed-refs`), so the key costs file reads, never a process.
    #
    # NOT cached when HEAD is unborn/unresolvable (`head_sha()` -> None):
    # there is no key that would distinguish one unborn state from the next,
    # so those calls fall through to the spawn exactly as before. The freshness
    # re-reads before a CAS are deliberately NOT special-cased -- they want
    # "HEAD's blobs as of now", and if HEAD has not moved the cached value IS
    # that, while if it has moved the key has changed.
    cache_key = None
    head = head_sha(repo)
    if head is not None:
        cache_key = (str(repo), head, tuple(paths))
        cached = _HEAD_BLOBS_CACHE.get(cache_key)
        if cached is not None:
            _HEAD_BLOBS_CACHE.move_to_end(cache_key)
            return dict(cached)

    result: Dict[str, Tuple[int, str]] = {}
    spine = read_tree_spine(repo, paths)
    if spine is None:
        # THE SPAWN IS THE FALLBACK, AND IT IS NOT OPTIONAL. `read_tree_spine`
        # returns None when it cannot read HEAD's tree -- an unreadable or
        # unsupported packfile, a corrupt object, a shape `_read_object`
        # declines. Continuing here with an empty `result` would return `{}`,
        # which is BYTE-IDENTICAL to the honest answer "none of these paths
        # exist in HEAD" and is consumed as exactly that:
        #
        #   `commit_gates.deletion_block_gate`'s Kept-claim leg reads an
        #   absent entry as "not at HEAD" and BLOCKS a legitimate commit;
        #   `dirty_tree_gate` recomputes staged-ness against this dict and
        #   misclassifies every staged path at once.
        #
        # Worse, the memo below would then cache that empty answer under
        # HEAD's own sha, so one unreadable read poisons every caller until
        # HEAD moves. A silent wrong answer where a correct one was available
        # is not a spawn saved; it is a defect bought at 22ms discount.
        #
        # So: fall through to `ls-tree`. This is the rare arm -- the spine
        # serves the ordinary case at zero spawns, which is the whole point of
        # C2b -- but the op must never be LESS able to answer than it was
        # before the cut. → docs/plans/2026-08-26-the-commit-op-stops-asking-
        # git-eleven-times.md C2b.
        from coordinator_core.git.argv_batch import _chunk_paths

        for chunk in _chunk_paths(paths):
            gr = run_git(["ls-tree", "-z", "HEAD", "--", *chunk], cwd=str(repo))
            if not gr.ok:
                continue
            for record in gr.stdout.split("\x00"):
                if not record:
                    continue
                meta, _, path = record.partition("\t")
                mode_str, obj_type, sha = meta.split(" ")
                # "blob" covers regular files AND symlinks (120000).
                # "commit" is a submodule gitlink (160000) and MUST be
                # admitted -- see the spine arm's own mode note below and
                # test_commit_gates.py's `gitlink_160000` regression. Only
                # "tree" is excluded.
                if obj_type not in ("blob", "commit"):
                    continue
                result[path] = (int(mode_str, 8), sha)
    else:
        for path in paths:
            parts = path.split("/")
            dirpath = "/".join(parts[:-1])
            leaf = parts[-1]
            entries = spine.get(dirpath)
            if entries is None:
                continue
            entry = entries.get(leaf)
            if entry is None:
                continue
            mode, sha = entry
            # `0o40000` is a directory/tree entry -- excluded, matching the
            # former `ls-tree` reader's own "tree" exclusion (see this
            # function's docstring). Every other mode -- blob, symlink
            # (120000), or gitlink/submodule (160000) -- is a leaf and is
            # admitted.
            if mode == 0o40000:
                continue
            result[path] = (mode, sha)

    if cache_key is not None:
        _HEAD_BLOBS_CACHE[cache_key] = dict(result)
        while len(_HEAD_BLOBS_CACHE) > _HEAD_BLOBS_CACHE_MAX:
            _HEAD_BLOBS_CACHE.popitem(last=False)
    # A COPY out, always -- callers mutate what they get back (`git_native`
    # merges freshness re-reads into its own dict), and handing out the cached
    # object would let one caller's edit become the next caller's truth.
    return dict(result)
