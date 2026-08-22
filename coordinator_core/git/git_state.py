"""coordinator_core.git.git_state -- an in-process reader for the two pieces
of git state a commit-path caller actually needs (staged index, HEAD sha),
with exactly ONE retained spawn for the third (HEAD tree blobs), isolated
behind a single call site.

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

MANDATORY REUSE, both BLOCKING:
    - The one retained spawn (`head_blobs`) routes through
      `coordinator_core.git.run::run_git`, never a private `subprocess.run`
      -- `coordinator_core/tests/test_shared_git_runner.py` fails any new
      non-test module reaching a bare `["git", ...]` argv.
    - Path resolution routes through `coordinator_core.git.git_dir`, never a
      hand-joined `<repo_root>/.git/...`. `index` and `HEAD` are
      WORKTREE-PRIVATE (`resolve_git_dir`); `refs/heads/*` (loose) and
      `packed-refs` both live in the SHARED common dir
      (`resolve_git_common_dir`) -- using the private dir for either lookup
      works on a plain clone (private == common there) and mis-resolves
      silently only in a linked worktree.

THE WORKTREE HASH DOES NOT WORK -- measured, not suspected. This module
never hashes on-disk bytes and compares the result to a git OID
(`core.autocrlf`/`core.filemode`/smudge filters make that comparison wrong
for a meaningful fraction of paths on this repo). No `worktree_blob()` is
provided here, deliberately: a caller needing a worktree-vs-git answer
keeps its spawn. The worktree axis fork is RESOLVED to "keep the spawn" --
do not re-open it and do not add a stat-based worktree comparison here.

Negative-spec:
    - NO process-lifetime cache, NO memoisation keyed on repo root. A caller
      needing a second observation (a compare-and-swap, e.g.
      `_agree_branch_cas_refusal`) takes its pre-state as a value parameter
      and calls this module fresh for the current side; a cache here would
      collapse that CAS into a single stale look with the guard still
      reading green -- the exact 2026-08-14 partial-stage incident this
      module exists to not repeat. `read_index` therefore stats the index
      file FRESH on every call (see `IndexSnapshot.stat_identity`).
    - Does NOT return `{}` on any parse failure. Signature mismatch,
      unsupported version, a split index, or any unmerged (`stage > 0`)
      entry all RAISE `IndexParseError` -- an empty dict reads as "nothing
      is staged", the worst possible wrong answer for every caller here.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Sequence, Tuple, Union

from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir
from coordinator_core.git.run import run_git

__all__ = [
    "IndexEntry",
    "StatIdentity",
    "IndexSnapshot",
    "IndexParseError",
    "read_index",
    "head_sha",
    "head_blobs",
]

_SIGNATURE = b"DIRC"
_SUPPORTED_VERSIONS = (2, 3, 4)
_SHA_LEN = 20  # sha1 only; a sha256 index carries a different OID length
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


def read_index(repo: Union[str, Path]) -> IndexSnapshot:
    """Parse `resolve_git_dir(repo)/index` directly (no `git` spawn) into an
    `IndexSnapshot` -- `{path: IndexEntry(mode, sha, stage)}` plus the index
    file's `stat_identity`, read FRESH on every call.

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

    try:
        raw = index_path.read_bytes()
    except FileNotFoundError:
        return IndexSnapshot({}, None)
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
    return IndexSnapshot(entries, stat_identity)


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


def head_blobs(repo: Union[str, Path], paths: Sequence[str]) -> Dict[str, Tuple[int, str]]:
    """`{path: (mode, sha)}` for `paths` as they exist in HEAD's tree. THE
    ONE RETAINED SPAWN in this module (`git ls-tree`, via `run_git`) --
    isolated behind this single call site so a future packfile-reading
    replacement has exactly one place to change. Argv-chunked via
    `_chunk_paths` so a large batch cannot overrun the Windows argv cap.

    A path absent from HEAD (untracked, or repo has no commits yet) is
    simply absent from the result -- never raises.
    """
    # Function-local import: `git_native` carries `contextvars`/`tempfile`/
    # `uuid` and the ceremony layer's own transitive weight, which a module
    # under `coordinator_core/git/` (on `ipc`'s cold-start path, per
    # `run.py`'s IMPORT COST section) must not pay just to expose the
    # signature -- only a caller that actually reaches the one retained
    # spawn pays for the shared packer.
    from coordinator_core.ops.ceremony.git_native import _chunk_paths

    paths = [p for p in paths if p]
    if not paths:
        return {}

    result: Dict[str, Tuple[int, str]] = {}
    for chunk in _chunk_paths(paths):
        gr = run_git(["ls-tree", "-z", "HEAD", "--", *chunk], cwd=str(repo))
        if not gr.ok:
            continue
        for record in gr.stdout.split("\x00"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            mode_str, obj_type, sha = meta.split(" ")
            # "blob" covers regular files AND symlinks (mode 120000 is a
            # blob). "commit" is a submodule gitlink (mode 160000) -- it
            # MUST be admitted here: `_is_staged`'s caller in commit_gates.py
            # treats an absent `head_entry` as "no HEAD counterpart", which
            # is correct for an untracked/new path but silently misreports a
            # genuinely dirty *tracked* gitlink as staged (see
            # coordinator_core/ops/ceremony/tests/test_commit_gates.py's
            # gitlink_160000 regression). Only "tree" (a directory entry,
            # reachable if a caller ever passes a directory pathspec) is
            # excluded -- it is neither a leaf blob nor a gitlink and no
            # caller here compares against tree identity.
            if obj_type not in ("blob", "commit"):
                continue
            result[path] = (int(mode_str, 8), sha)
    return result
