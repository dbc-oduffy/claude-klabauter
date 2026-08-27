"""coordinator_core.git.content_hash -- in-process reproduction of git's
`core.autocrlf=true`, default-attribute (`text=auto`) checkin-side blob
normalization, plus the "does normalize(worktree bytes) hash to this index
sha" predicate a scoped worktree-vs-index divergence check needs to settle a
stat-mismatch candidate WITHOUT spawning `git`.

MOVED DOWN, not re-derived, from `coordinator_core.ops.ceremony.git_native`
(C3e, docs/dispatch-briefs/2026-08-26-the-commit-op-stops-asking-git-eleven-
times/C3e.md). `git_native` re-exports every name this module defines so its
own existing call sites (`_hash_worktree_blobs`, `commit_authored_new_file`)
keep importing what they imported before this move -- only the definitions'
home changed. The move exists so `coordinator_core/git/divergence.py` (which
sits on `ipc`'s cold-start path, see `git_state.py`'s own import-cost note)
can reach these primitives without importing UPWARD into `ops/` -- that edge
was deliberately retired 2026-08-26 (C1 of the archival-commit-helper plan
moved `_chunk_paths` into `git/argv_batch.py` for the identical reason).

VERIFIED, not derived from convert.c: `_autocrlf_checkin_normalize` was
proven byte-identical to real `git hash-object` over 14 shapes (CRLF-only,
LF-only, mixed, lone-CR-mid, lone-CR-after-a-good-CRLF, trailing lone CR,
NUL early, NUL past 8KB, >5000-line CRLF, empty, bare CR, NUL between CRLF
pairs, CR at end after LF, UTF-16-ish NUL-heavy) -- C3c's spike,
docs/plans/2026-08-26-the-commit-op-stops-asking-git-eleven-times.md,
re-verified independently by the EM ahead of C3e against real git, not a
fixture. `autocrlf=input` performs the same checkin-side conversion but is
DELIBERATELY still refused -- it has no corpus run of its own; widening to
it without one would be exactly the un-spiked claim the spike exists to
forbid.

THE PROHIBITION IS SATISFIED, NOT LIFTED. `coordinator_core/git/git_state.py`
forbids hashing worktree bytes and comparing the result to an index sha,
because the NAIVE comparison is wrong under the checkin filters -- measured
wrong, 326 of 400 clean tracked files on this repo, the reverted `da156a723`
incident. What was forbidden was hashing RAW worktree bytes. NORMALIZE-then-
hash, verified byte-identical to what `git hash-object` itself would write,
is a different operation with a different correctness argument, and this
module never hashes a path this function's own preconditions have not
cleared -- every other shape DECLINES (returns `None`) and the caller keeps
its spawn. See `content_matches_index_sha`'s own docstring for the exact
precondition list.

Spec backlink: docs/dispatch-briefs/2026-08-26-the-commit-op-stops-asking-
git-eleven-times/C3e.md
Spec backlink: docs/plans/2026-08-26-the-commit-op-stops-asking-git-eleven-
times.md
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from fnmatch import fnmatch
from pathlib import Path
from typing import List, Optional, Set, Tuple

from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

__all__ = [
    "content_matches_index_sha",
    "_autocrlf_checkin_normalize",
    "_repo_autocrlf_true",
    "_text_attribute_pinned",
    "_system_gitconfig_paths",
    "_clean_filter_may_apply",
    "_attributes_pattern_matches",
]

#: Gitattributes filename consulted at every directory level between the
#: repo root and a path's own directory, mirroring git's own attribute-file
#: search order. Deliberately NOT the full set git consults --
#: `core.attributesFile` (a global path) is out of reach without a config
#: walk this module's budget cannot afford.
_LOCAL_ATTRIBUTES_FILENAME = ".gitattributes"


def _read_config_core_autocrlf(config_path: Path) -> Optional[str]:
    """Best-effort `[core] autocrlf = ...` read from one git config file.
    A full git-config parser is out of scope for a fast-path check whose
    failure just means "take the ladder", never a wrong blob. Returns the
    LAST `autocrlf` value found in the `[core]` section (git's own
    last-wins precedence within one file), lower-cased, or `None` if the
    file is unreadable or carries no such key."""
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    value: Optional[str] = None
    in_core = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_core = stripped.lower().startswith("[core]")
            continue
        if in_core and "=" in stripped:
            key, _, raw_value = stripped.partition("=")
            if key.strip().lower() == "autocrlf":
                value = raw_value.strip().strip('"').lower()
    return value


def _system_gitconfig_paths() -> Tuple[Path, ...]:
    """Candidate SYSTEM git config locations, resolved without a spawn.

    C3d: Git for Windows writes `core.autocrlf=true` into the SYSTEM config
    (`<install>/etc/gitconfig`), NOT into the repo config and NOT into
    `~/.gitconfig`. A chain that reads only repo-local and global therefore
    resolves `False` on a stock Windows install, the caller refuses every
    CR-bearing path, and a verified-correct in-process blob write never
    executes. The ordering `shutil.which` gives us also covers a
    non-default install prefix, which a hardcoded `C:\\Program Files`
    would miss.  # abs-path-ok: illustrative example in a docstring, not a real path"""
    candidates: List[Path] = []
    git_exe = shutil.which("git")
    if git_exe:
        # <prefix>/bin/git.exe -> <prefix>/etc/gitconfig, and Git for
        # Windows' own <prefix>/mingw64/etc/gitconfig.
        bin_dir = Path(git_exe).resolve().parent
        prefix = bin_dir.parent
        candidates.append(prefix / "etc" / "gitconfig")
        candidates.append(prefix / "mingw64" / "etc" / "gitconfig")
        candidates.append(prefix.parent / "etc" / "gitconfig")
    candidates.append(Path("/etc/gitconfig"))
    seen: Set[str] = set()
    ordered: List[Path] = []
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    return tuple(ordered)


def _repo_autocrlf_true(root: Path) -> bool:
    """Is `core.autocrlf` resolvable, from sources this module can read
    without a spawn, to exactly `true`? Reads git's full layer stack in
    git's own precedence -- system, then global `~/.gitconfig`, then
    repo-local (`core.*` is not worktree-private) -- with LAST WINS, so a
    repo-local `false` beats a system `true`. `GIT_CONFIG_SYSTEM` and
    `GIT_CONFIG_NOSYSTEM` are honoured because real git honours them.

    Deliberately narrow: `autocrlf=input` performs the SAME checkin-side
    CRLF->LF conversion `true` does, but only `true` has been verified
    byte-identical against real `git hash-object` -- widening to `input`
    without its own corpus run would be exactly the un-spiked claim that
    spike exists to forbid. `false`/unset/any other value (including a
    read failure) returns `False`, which routes the caller to the spawn
    ladder -- never a silent wrong guess."""
    if os.environ.get("GIT_CONFIG_NOSYSTEM"):
        system_paths: Tuple[Path, ...] = ()
    elif os.environ.get("GIT_CONFIG_SYSTEM"):
        system_paths = (Path(os.environ["GIT_CONFIG_SYSTEM"]),)
    else:
        system_paths = _system_gitconfig_paths()

    # LAST WINS, which is git's own precedence and the opposite of a
    # first-hit return. Read every layer low-to-high and keep overwriting:
    # system, then global, then repo-local.
    value: Optional[str] = None
    for config_path in (
        *system_paths,
        Path.home() / ".gitconfig",
        resolve_git_common_dir(root) / "config",
    ):
        found = _read_config_core_autocrlf(config_path)
        if found is not None:
            value = found
    return value == "true"


def _attributes_pattern_matches(pattern: str, rel_to_attrs_dir: str) -> bool:
    """Does a gitattributes `pattern` match `rel_to_attrs_dir` (the target
    path, relative to the directory the attributes file lives in)?

    Deliberately OVER-matching where it is imprecise, never under-matching:
    `fnmatch`'s `*` crosses `/` where git's single `*` does not, so this
    can answer `True` where git would answer `False`. That error direction
    is the safe one -- a false positive costs the caller a loud refusal it
    can act on, a false negative costs a silently wrong blob in a
    repository we do not own.

    Pattern semantics implemented (gitattributes(5)): a pattern with no
    `/` matches the BASENAME at any depth; a pattern containing a `/` is
    anchored to the attributes file's own directory, with a leading `/`
    stripped; a pattern ending in `/` names a directory and so cannot
    match the file being written."""
    if not pattern or pattern.endswith("/"):
        return False
    if "/" not in pattern:
        return fnmatch(rel_to_attrs_dir.rpartition("/")[2], pattern)
    return fnmatch(rel_to_attrs_dir, pattern.lstrip("/"))


def _text_attribute_pinned(root: Path, normalized: str) -> Optional[str]:
    """`None` when no repo-local attributes file assigns `text`, `-text`,
    `text=...`, or `eol=...` to `normalized`, else a diagnostic naming the
    file and the pattern that does.

    Same traversal, same candidate ordering, same safe-direction bias as
    `_clean_filter_may_apply` (carried, not re-derived) -- an `[attr]`
    macro line carrying any of these tokens refuses the path outright,
    same as that function's `filter=` macro case: resolving macro
    expansion is a second pass this function does not implement.
    `_autocrlf_checkin_normalize`'s corpus was only run against paths with
    NO forced text/eol/binary attribute (git's default `text=auto`
    disposition under `core.autocrlf=true`) -- a path this function flags
    is a disposition the spike never measured, so a caller refuses it to
    the spawn ladder rather than guessing that the auto heuristic still
    applies."""
    parent = Path(normalized).parent
    parts = [] if parent == Path(".") else list(parent.parts)

    candidates = [(resolve_git_dir(root) / "info" / "attributes", normalized)]
    for depth in range(len(parts) + 1):
        candidates.append(
            (
                root.joinpath(*parts[:depth]) / _LOCAL_ATTRIBUTES_FILENAME,
                "/".join(normalized.split("/")[depth:]),
            )
        )

    for candidate, rel_to_attrs_dir in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            code = line.split("#", 1)[0].strip()
            if not code:
                continue
            tokens = code.split()
            attr_tokens = tokens[1:] if len(tokens) > 1 else []
            has_text_directive = any(
                tok in ("text", "-text") or tok.startswith("text=") or tok.startswith("eol=")
                for tok in attr_tokens
            )
            if not has_text_directive:
                continue
            if code.startswith("[attr]"):
                return (
                    f"{candidate} defines an `[attr]` macro carrying a "
                    "text/eol directive -- macro expansion is not resolved "
                    "here, so the path is refused rather than guessed"
                )
            pattern = tokens[0]
            if _attributes_pattern_matches(pattern, rel_to_attrs_dir):
                return (
                    f"{candidate} routes {pattern!r} through a text/eol "
                    "attribute, and that pattern matches this path"
                )
    return None


def _clean_filter_may_apply(root: Path, normalized: str) -> Optional[str]:
    """`None` when no repo-local attributes file routes `normalized`
    through a `filter.*.clean` driver, else a diagnostic naming the file
    and the pattern that does.

    Zero spawns. Reads `.git/info/attributes` and every `.gitattributes`
    from the repo root down to the path's own directory, and refuses only
    on a `filter=` line whose PATTERN MATCHES this path
    (`_attributes_pattern_matches`). Path-scoped, not repo-scoped: a repo
    declaring an LFS filter for `*.uasset`/`*.png`/`*.exe`-shaped binaries
    still leaves a markdown memo deliverable through this path.

    A `[attr]` MACRO line carrying `filter=` refuses the path outright:
    resolving which patterns then assign that macro is a second pass this
    function does not implement, and guessing in the permissive direction
    is the one error this function must not make. That refusal is
    REPO-WIDE, not path-scoped -- a deliberate safe-direction tradeoff,
    not a bug."""
    parent = Path(normalized).parent
    parts = [] if parent == Path(".") else list(parent.parts)

    #: `(attributes file, the target path relative to that file's directory)`
    #: -- `.git/info/attributes` patterns are rooted at the repo, the same
    #: as a root `.gitattributes`.
    candidates = [(resolve_git_dir(root) / "info" / "attributes", normalized)]
    for depth in range(len(parts) + 1):
        candidates.append(
            (
                root.joinpath(*parts[:depth]) / _LOCAL_ATTRIBUTES_FILENAME,
                "/".join(normalized.split("/")[depth:]),
            )
        )

    for candidate, rel_to_attrs_dir in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            code = line.split("#", 1)[0].strip()
            if "filter=" not in code:
                continue
            if code.startswith("[attr]"):
                return (
                    f"{candidate} defines an `[attr]` macro carrying `filter=` "
                    "-- macro expansion is not resolved here, so the path is "
                    "refused rather than guessed"
                )
            pattern = code.split()[0]
            if _attributes_pattern_matches(pattern, rel_to_attrs_dir):
                return (
                    f"{candidate} routes {pattern!r} through a `filter=` "
                    "attribute, and that pattern matches this path, so a "
                    "`filter.*.clean` driver may apply to it"
                )
    return None


def _autocrlf_checkin_normalize(content: bytes) -> bytes:
    """Reproduce git's `core.autocrlf=true`, default-attribute (`text=
    auto`) checkin-side normalization for `content`, IN PROCESS, to
    byte-identity with `git hash-object --path=<p> --stdin < <p>` (C3c
    spike, docs/plans/2026-08-26-the-commit-op-stops-asking-git-eleven-
    times.md -- verified against a corpus of CRLF-only, LF-only, mixed,
    lone-CR, NUL-containing, and >8000-byte content run against a real
    `git hash-object` subprocess, byte-identical in every case; this is
    NOT a re-derivation of git's own convert.c logic, it is the transform
    that reproduced it).

    Two refusal-shaped no-ops, matching what real git leaves untouched:
    a NUL byte ANYWHERE in `content` (not sampled/truncated -- the spike's
    >8000-byte-with-late-NUL case confirmed a full-buffer scan, not
    xdiff's diff-heuristic sample) marks the blob binary, so it passes
    through verbatim; a CR byte not immediately followed by LF, ANYWHERE
    in `content`, blocks conversion of the WHOLE buffer, not just the
    offending line (the spike's `trailing_lone_cr_at_end` case: a single
    well-formed CRLF pair earlier in the same buffer was left
    unconverted once a later lone CR appeared). Only when neither
    condition holds does git convert every `\\r\\n` pair to `\\n` --
    exactly `bytes.replace`, with lone CR/LF bytes elsewhere in the
    buffer already ruled out by the two checks above.

    Caller's responsibility, not this function's: this is only correct
    for a path with NO repo-local `text`/`-text`/`eol=` attribute pin
    (`_text_attribute_pinned`) under a repo-config-resolved
    `core.autocrlf=true` (`_repo_autocrlf_true`) -- this function does no
    attribute or config reading of its own and trusts its caller for
    both preconditions."""
    if b"\x00" in content:
        return content
    if re.search(rb"\r(?!\n)", content):
        return content
    return content.replace(b"\r\n", b"\n")


def content_matches_index_sha(root: Path, normalized: str, index_sha: str) -> Optional[bool]:
    """Settle a stat-mismatch ("candidate") worktree-vs-index verdict via
    NORMALIZE-THEN-HASH, with zero spawns, whenever every precondition
    `_autocrlf_checkin_normalize` was verified under actually holds for
    `normalized`. Returns `True`/`False` when determinable, `None` when
    this function DECLINES -- the caller keeps its spawn, exactly as
    `_hash_worktree_blobs` does for the identical precondition set.

    Preconditions (ALL required, checked in cheapest-first order; any
    failure is an immediate `None`):
      - no repo-local `filter=` clean-pipeline attribute for `normalized`
        (`_clean_filter_may_apply`) -- a filter-driven blob is not what
        `_autocrlf_checkin_normalize` reproduces.
      - `core.autocrlf` resolves, through the full config layer stack, to
        exactly `true` (`_repo_autocrlf_true`). `autocrlf=input` is
        deliberately still refused, unverified.
      - no repo-local `text`/`-text`/`eol=` attribute pin for `normalized`
        (`_text_attribute_pinned`) -- git's default `text=auto`
        disposition is the only one the spike measured.
      - `normalized` is readable off disk under `root`.

    `index_sha` is the path's OID as recorded in `.git/index` (an
    `IndexEntry.sha`, not a HEAD sha) -- this settles the WORKTREE-vs-
    INDEX axis only, never worktree-vs-HEAD."""
    if _clean_filter_may_apply(root, normalized) is not None:
        return None
    if not _repo_autocrlf_true(root):
        return None
    if _text_attribute_pinned(root, normalized) is not None:
        return None
    try:
        content = (root / normalized).read_bytes()
    except OSError:
        return None
    normalized_bytes = _autocrlf_checkin_normalize(content)
    header = b"blob " + str(len(normalized_bytes)).encode("ascii") + b"\x00"
    sha = hashlib.sha1(header + normalized_bytes).hexdigest()
    return sha == index_sha
