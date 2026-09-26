"""
coordinator_core.ops.fleet.archive_terminal_handoffs — fleet.archive_completed_handoffs op.

Purpose: archive terminal handoffs from state/handoffs/ into
archive/handoffs/YYYY-MM/, bounded by a required per-invocation MOVE CAP so the
op stays inside the 500ms brightline regardless of how large the retained
population grows (docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md).

Registered under "fleet.archive_completed_handoffs" — the atomic name-cutover
(C1b of this plan, per staff-eng F9) deleted the killed
coordinator_core/ops/fleet/archive_handoffs.py module and repointed this
module's registration onto the same op key in the SAME commit, so the tree
was never left with two ops racing for the same registration key at any
intermediate commit. C1b also deleted the subsumed
coordinator_core/ops/fleet/archive_shipped_handoffs.py sibling and its sole
caller, coordinator/bin/sweep-shipped-handoffs.py (see C1b's dispatch brief
"The sibling op is subsumed" / staff-eng F0 caller sweep).

Naming debt (staff-eng F9, not resolved by C1b): the op key
("archive_completed_handoffs"), this module's filename
("archive_terminal_handoffs.py"), and the CLI script that will eventually
supersede sweep-shipped-handoffs.py ("sweep-terminal-handoffs.py", C4) still
carry three different nouns — renaming the op key is a second breaking change
on top of the `cap` param and is left for a follow-on pass.

Spec backlinks:
  - Plan: docs/plans/2026-08-25-the-handoff-auto-archive-comes-back-capped.md § C1a
  - Cap-axis decision (binding, not re-derived here):
    state/audits/2026-08-25-the-handoff-archive-op-earns-its-way-back.md § C0
  - Terminality contract (re-sourced from the DRs below, NOT from the killed
    module's docstring — see staff-eng F7):
      DR-324 (docs/decisions/DR-324-*.md) — Check 3's succession-vs-derivation
        narrowing: a terminal-deployment (Branch B) parent is not retained by a
        live SUCCESSION child (predecessor / additional_predecessors), but a
        live forked_from spinoff still retains it.
      DR-224 (docs/decisions/DR-224-*.md) — a forked_from spinoff founds its
        own line and does not retire its origin (the DR-324 narrowing's own
        precedent).
      DR-084 P4 — the shipped-only fail-closed shipped_in resolvability gate,
        and the four-member terminal deployment_state set {"shipped",
        "abandoned", "continued", "closed"}.
      coordinator_core/contract/cockpit-invoke-producer-contract.md — the wire
        envelope (§2.1/§3.1), the generic terminal-status display string
        convention, and §6's additive-field-without-rebump allowance (used
        below for the "deferred" cap-truncation field).
  - Cap param + git contract (staff-eng F3): mirrors
    coordinator_core/ops/fleet/archive_shipped_handoffs.py's scoped-pathspec
    negative-spec (never `-A`, never `.`), enforced by the shared
    `_common.archive_and_commit` helper reused unchanged below.
  - Single-flight rail (staff-eng F5): a dedicated O_EXCL lock file with a
    stale-lock timeout — see `_acquire_sweep_lock` below.
  - Lesson: state/lessons/2026-08-23-a-staged-index-reads-as-a-pathspec.md
    (why the git contract stays scoped-pathspec-only, never bare `git commit`).

Negative-spec:
  - Does NOT transcribe archive_handoffs.py's pipeline shape (its heir branch,
    H1-H4 promoter-owned-roadmap-baton carve-out, and per-candidate
    reverse_membership re-walk) — this plan's OWN design, authored from the
    terminality requirements cited above, is narrower by intent, and two PM
    rulings have narrowed it further since: Branch A and Branch B terminality
    are all that decide archival here. Check 3 (childlessness) and Check 4
    (live claim holder) both stood in `_scan_terminal` and were deleted on the
    ruling that neither a baton's children nor its holder's liveness says
    anything about whether it is used up — see their deletion notes there.
  - Does NOT take an OP_TIMEOUT_OVERRIDES row in coordinator_core/ipc.py — see
    that module's own comment on why a widened cap for a git-spawning op is
    how the cost stays unexamined (ceremony.scoped_git_commit's 150s row,
    53 spawns for one commit). This op resolves via the ordinary global
    runaway-guard timeout like any unlisted op.
  - Does NOT accept an absent `cap` — absent `cap` is a validate_params
    setup-error (exit_code:1), never an unbounded default. The cap's VALUE is
    the CALLER's choice; this module recommends one (see
    `_RECOMMENDED_CAP_CHOICE` below) but does not silently substitute it.
  - Does NOT walk state/handoffs/ more than once, and since Check 3's
    deletion does not build a reverse-edge index at all — nothing here asks
    what points at a candidate. ONE `_read_meta` pass over
    `collect_live_handoff_paths` feeds the Branch A/B classification and
    nothing else.
  - Does NOT spawn one git process per candidate, and (C10 of this plan,
    AC-11) does not spawn ANY git process for the `shipped_in` rail at all —
    `_resolve_shipped_in_no_spawn` below answers object-existence by reading
    loose objects and packed `.idx` files directly, bounded and
    dependency-free (no dulwich/pygit2, no `.git/index` DIRC parser — see
    that function's own docstring for the fail-closed degradation contract).
    The worktree-dirty rail (C3, classify-first) keeps its single `git
    status --porcelain` spawn on the standalone/op path
    (`known_dirty_relpaths=None`), scoped to the SURVIVING candidates
    classification already narrowed the corpus to — zero survivors means
    zero spawns for this rail, not merely a smaller pathspec. A future
    in-plane caller that already computed divergence via
    `commit_pipeline`/`commit_scoped` passes it through
    `known_dirty_relpaths=` instead, so `plan_sweep` spawns at most 1 git
    process on the standalone path (worktree-dirty only, 0 when nothing
    survives classification) and 0 on the in-plane path. (On the act path)
    the existing `archive_and_commit` helper's own single `git add` +
    single `git commit`. Spawn count does not scale with candidate count
    (AC-5).
"""

from __future__ import annotations

import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.git.content_hash import content_matches_index_sha
from coordinator_core.git.git_index import (
    IndexParseError as _IndexParseError,
    parse_index_identity,
)
from coordinator_core.git.git_state import (
    IndexParseError as _StateIndexParseError,
    head_blobs,
)
from coordinator_core.ops.ceremony.git_native import (
    _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS,
    REASON_WORKTREE_DIRTY,
    dirty_relpaths_from_porcelain,
)
from coordinator_core.ops.fleet._common import (
    Move,
    _is_identical_duplicate,
    _REASON_DEST_CONFLICT,
    _TERMINAL_DEPLOYMENT_STATES,
    archive_and_commit,
    declare_move_claims,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    collect_live_handoff_paths,
    handoff_archive_dest,
    main_worktree_root,
    rel_id,
    validate_params,
)

_LOG = logging.getLogger(__name__)

_FAMILY = "handoff"

# Scan-rail refusal reasons. NEGATIVE SPEC: these never reach the cockpit
#: (`REASON_WORKTREE_DIRTY`), shared with `archive_sizings.py`'s identical
_SCAN_REASON_WORKTREE_DIRTY = REASON_WORKTREE_DIRTY
_SCAN_REASON_NOT_TERMINAL = "not-terminal"
_SCAN_REASON_SHIPPED_IN_UNRESOLVABLE = "shipped-in-unresolvable"

_RECOMMENDED_CAP_CHOICE = 150

_SWEEP_LOCK_STALE_S = 120.0


def _sweep_lock_path(common_dir: Path) -> Path:
    return common_dir / "coordinator-sessions" / "archive-terminal-handoffs.lock"


def _acquire_sweep_lock(common_dir: Path) -> Optional[Path]:
    """Best-effort O_EXCL acquire of the single-flight sweep lock.

    Returns the lock Path on success (caller MUST unlink it when done, in a
    finally block). Returns None when another instance already holds a
    fresh lock — the caller's contract is to treat that as a first-class
    non-error "another instance holds the sweep" result, never an error.

    A stale lock (older than _SWEEP_LOCK_STALE_S — its writer crashed or was
    killed between acquire and release) is broken and retried exactly once:
    six entry points x detached spawns x a 50-70-concurrent-session box makes
    two concurrent instances of this op the expected case (staff-eng F5), so
    a merely-stale lock must self-heal rather than wedge every future sweep.
    """
    lock_path = _sweep_lock_path(common_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return lock_path
    except FileExistsError:
        try:
            age_s = time.time() - lock_path.stat().st_mtime
        except OSError:
            return None
        if age_s <= _SWEEP_LOCK_STALE_S:
            return None
        try:
            lock_path.unlink()
        except OSError:
            return None
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return lock_path
        except OSError:
            return None
    except OSError as exc:
        _LOG.warning(
            "archive_terminal_handoffs: sweep-lock acquire failed for %s — %s; "
            "degrading to 'contended' (fail-closed-to-skip)", lock_path, exc,
        )
        return None


def _release_sweep_lock(lock_path: Optional[Path]) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


_HEX_DIGITS = frozenset("0123456789abcdef")

_PACK_IDX_MAGIC = b"\xfftOc"

_SUPPORTED_PACK_IDX_VERSION = 2


# A `shipped_in` may be ABBREVIATED. git's own minimum useful abbreviation is
_MIN_ABBREV_SHA_HEX = 7


def _oid_search_range(sha_hex: str) -> Optional[Tuple[bytes, bytes]]:
    """The inclusive `(lo_key, hi_key)` 20-byte bounds an oid table is searched
    between for `sha_hex`, or None when `sha_hex` is not a usable oid.

    A full 40-hex sha yields `lo_key == hi_key` — the exact-match search the
    callers have always done, bit for bit. A 7..39-hex abbreviation yields the
    range that prefix spans (right-padded with `0`s and `f`s), so a sorted-oid
    binary search answers "does any object carry this prefix" in the same
    O(log N) reads as the exact case. `_object_exists_no_spawn` is an
    EXISTENCE gate, so an abbreviation matching more than one object is still
    a true "the recorded commit is present"; disambiguating it would need the
    object bodies this reader deliberately never touches.
    """
    sha = sha_hex.strip().lower()
    if not _MIN_ABBREV_SHA_HEX <= len(sha) <= 40 or not _HEX_DIGITS.issuperset(sha):
        return None
    try:
        return bytes.fromhex(sha.ljust(40, "0")), bytes.fromhex(sha.ljust(40, "f"))
    except ValueError:
        return None


def _sha_in_pack_idx(idx_path: Path, lo_key: bytes, hi_key: bytes) -> bool:
    with open(idx_path, "rb") as f:
        header = f.read(8)
        if len(header) < 8:
            raise ValueError(f"{idx_path}: truncated header")
        if header[:4] == _PACK_IDX_MAGIC:
            version = int.from_bytes(header[4:8], "big")
            if version != _SUPPORTED_PACK_IDX_VERSION:
                raise ValueError(f"{idx_path}: unsupported idx version {version}")
            fanout_offset = 8
            entry_stride = 20
            sha_field_offset = 0
        else:
            fanout_offset = 0
            entry_stride = 24
            sha_field_offset = 4

        f.seek(fanout_offset)
        fanout_bytes = f.read(1024)
        if len(fanout_bytes) != 1024:
            raise ValueError(f"{idx_path}: truncated fanout table")
        fanout = [int.from_bytes(fanout_bytes[i * 4:i * 4 + 4], "big") for i in range(256)]

        first_byte = lo_key[0]
        lo = fanout[first_byte - 1] if first_byte > 0 else 0
        hi = fanout[first_byte]
        shas_start = fanout_offset + 1024

        while lo < hi:
            mid = (lo + hi) // 2
            f.seek(shas_start + mid * entry_stride + sha_field_offset)
            candidate = f.read(20)
            if len(candidate) != 20:
                raise ValueError(f"{idx_path}: truncated sha entry at index {mid}")
            if candidate < lo_key:
                lo = mid + 1
            else:
                hi = mid

        if lo >= fanout[first_byte]:
            return False
        f.seek(shas_start + lo * entry_stride + sha_field_offset)
        candidate = f.read(20)
        if len(candidate) != 20:
            raise ValueError(f"{idx_path}: truncated sha entry at index {lo}")
        return lo_key <= candidate <= hi_key


_MIDX_MAGIC = b"MIDX"
_SUPPORTED_MIDX_VERSION = 1
_MIDX_CHUNK_FANOUT = b"OIDF"
_MIDX_CHUNK_LOOKUP = b"OIDL"


def _sha_in_multi_pack_index(midx_path: Path, lo_key: bytes, hi_key: bytes) -> bool:
    with open(midx_path, "rb") as f:
        header = f.read(12)
        if len(header) < 12:
            raise ValueError(f"{midx_path}: truncated header")
        if header[:4] != _MIDX_MAGIC:
            raise ValueError(f"{midx_path}: not a multi-pack-index")
        if header[4] != _SUPPORTED_MIDX_VERSION:
            raise ValueError(f"{midx_path}: unsupported midx version {header[4]}")
        if header[5] != 1:
            raise ValueError(f"{midx_path}: unsupported oid version {header[5]}")
        if header[7] != 0:
            raise ValueError(f"{midx_path}: incremental midx chain unsupported")

        num_chunks = header[6]
        table = f.read((num_chunks + 1) * 12)
        if len(table) != (num_chunks + 1) * 12:
            raise ValueError(f"{midx_path}: truncated chunk lookup table")
        chunks = {
            table[i * 12:i * 12 + 4]: int.from_bytes(table[i * 12 + 4:i * 12 + 12], "big")
            for i in range(num_chunks + 1)
        }

        fanout_offset = chunks.get(_MIDX_CHUNK_FANOUT)
        lookup_offset = chunks.get(_MIDX_CHUNK_LOOKUP)
        if fanout_offset is None or lookup_offset is None:
            raise ValueError(f"{midx_path}: missing OIDF/OIDL chunk")

        f.seek(fanout_offset)
        fanout_bytes = f.read(1024)
        if len(fanout_bytes) != 1024:
            raise ValueError(f"{midx_path}: truncated fanout table")
        fanout = [int.from_bytes(fanout_bytes[i * 4:i * 4 + 4], "big") for i in range(256)]

        first_byte = lo_key[0]
        lo = fanout[first_byte - 1] if first_byte > 0 else 0
        hi = fanout[first_byte]

        while lo < hi:
            mid = (lo + hi) // 2
            f.seek(lookup_offset + mid * 20)
            candidate = f.read(20)
            if len(candidate) != 20:
                raise ValueError(f"{midx_path}: truncated oid entry at index {mid}")
            if candidate < lo_key:
                lo = mid + 1
            else:
                hi = mid

        if lo >= fanout[first_byte]:
            return False
        f.seek(lookup_offset + lo * 20)
        candidate = f.read(20)
        if len(candidate) != 20:
            raise ValueError(f"{midx_path}: truncated oid entry at index {lo}")
        return lo_key <= candidate <= hi_key


def _require_out_accumulator(name: str, value: object) -> None:
    if value is not None and not isinstance(value, list):
        raise TypeError(
            f"{name} is an out-accumulator, not a flag: pass a list to collect "
            f"{{id, reason}} entries, or omit it; got {type(value).__name__}"
        )


def _require_git_common_dir(common_dir: Path) -> None:
    """Refuse a `common_dir` that names no object store.

    The object reader is existence-only and fail-closed, so a wrong path
    answers False for every sha rather than raising -- and the sweep then
    reports a per-baton `shipped-in-unresolvable`, which reads as a fact
    about the record. A caller who passed the ARCHIVE DESTINATION here spent
    40 minutes on a fabricated "shipped_in must be a full 40-char sha" rule
    and committed a record edit to satisfy it (example-store-repo-fb, 2026-09-11).
    Fail-closed is right; fail-closed while describing the data is what cost
    the time.
    """
    if not (common_dir / "objects").exists():
        raise ValueError(
            f"common_dir must be a git common dir (no objects/ under {common_dir}) — "
            "pass `git rev-parse --git-common-dir`, not a worktree or archive path"
        )


def _object_exists_no_spawn(common_dir: Path, sha_hex: str) -> bool:
    """Existence-only, dependency-free answer to "does this sha name an
    object that exists" — the Rail-2 bounded reader (C10, AC-11).

    Reads, in order: the loose object path `.git/objects/<2>/<38>` (a
    `stat`), the `multi-pack-index` when one exists (its own OIDF fanout +
    OIDL table), then each pack `.idx` via its fanout table and a binary search
    over the sorted sha list. No zlib, no object body, no `.git/index`
    parsing — existence only.

    ABBREVIATED shas resolve. `shipped_in` is written abbreviated by several
    stamping paths (8-9 hex is the common shape on this corpus), and requiring
    a full 40 made this reader answer False for every one of them — the same
    fail-closed retention the multi-pack-index bail-out caused, on the NEWEST
    records rather than the packed ones, and growing with every ship. A 7..39
    hex value is searched as the oid range its prefix spans
    (`_oid_search_range`); below 7 it is refused, since "some object starts
    with this" stops being evidence about the recorded commit. This is an
    EXISTENCE gate, so an abbreviation matching more than one object is still
    a true answer — disambiguating would need object bodies this never reads.

    FAIL-CLOSED DEGRADATION (the correctness argument, made explicit): any
    condition this reader does not model — a malformed/non-hex/too-short sha, an
    `objects/info/alternates` file (the object could live ONLY in an
    alternate object store this reader never reads), a `multi-pack-index.d`
    incremental chain, a MIDX whose header/chunk layout
    `_sha_in_multi_pack_index` refuses, an unexpected `.idx` version, or any
    read error — returns False (unresolvable), NEVER True.

    A MIDX is READ, not bailed on. Treating its mere presence as unmodeled
    made this reader answer False for EVERY packed object in any repo that
    has one — `git gc`/`git maintenance` write one by default — which
    silently retained every shipped handoff whose ship commit was packed
    (measured here 2026-08-27: 28 of 28 shipped records, sweep archiving
    nothing but the one record whose ship commit was still loose). It is
    consulted before the per-pack `.idx` scan (a `midx-expire` can leave a
    covered pack without its own `.idx`), and a MIDX *miss* still falls
    through to that scan, since a pack newer than the MIDX is not covered
    by it. A caller's existing
    "unresolvable retains (fail-closed)" disposition (`_classify_branch`)
    means a false-unresolvable degrades to "retain, don't archive" — the
    only direction this reader is permitted to err in. Guessing "exists" on
    an unhandled case would turn that fail-closed archive gate fail-open,
    which is the one thing this function must never do.
    """
    sha = sha_hex.strip().lower()
    bounds = _oid_search_range(sha)
    if bounds is None:
        return False
    lo_key, hi_key = bounds

    objects_dir = common_dir / "objects"
    try:
        if (objects_dir / "info" / "alternates").exists():
            return False
        pack_dir = objects_dir / "pack"
        if (pack_dir / "multi-pack-index.d").exists():
            return False

        loose_dir = objects_dir / sha[:2]
        if len(sha) == 40:
            if (loose_dir / sha[2:]).is_file():
                return True
        else:
            rest = sha[2:]
            try:
                if any(e.name.startswith(rest) for e in os.scandir(loose_dir)):
                    return True
            except FileNotFoundError:
                pass

        if not pack_dir.is_dir():
            return False

        midx_path = pack_dir / "multi-pack-index"
        if midx_path.is_file():
            try:
                if _sha_in_multi_pack_index(midx_path, lo_key, hi_key):
                    return True
            except (OSError, ValueError):
                return False
        for idx_path in sorted(pack_dir.glob("*.idx")):
            try:
                if _sha_in_pack_idx(idx_path, lo_key, hi_key):
                    return True
            except (OSError, ValueError):
                continue
        return False
    except OSError:
        return False


def _batch_resolve_shipped_in(common_dir: Path, shas: Sequence[str]) -> Dict[str, bool]:
    """Resolve every distinct `shipped_in` sha's object-existence via the
    Rail-2 bounded reader — ZERO git spawns (C10, AC-11; supersedes the
    prior `git cat-file --batch` rail).

    Deduplicates first (repeat shipped_in values across candidates are
    common). Returns {} when there is nothing to resolve.

    SYNCHRONOUS (C2, docs/plans/2026-08-26-the-sweep-stops-paying-for-a-
    room-it-nev.md): this function was `async def` with no `await` in its
    body at all — the easy tell that the whole op's async shape was
    self-imposed rather than load-bearing. See the module's own `_handler`
    docstring for the full justification.
    """
    distinct = sorted({s.strip() for s in shas if s and isinstance(s, str) and s.strip()})
    if not distinct:
        return {}
    return {sha: _object_exists_no_spawn(common_dir, sha) for sha in distinct}


def _dirty_relpaths_in_process(worktree: Path, ordered: Sequence[str]) -> Optional[set]:
    """The spawn-free arm of `_dirty_handoff_relpaths`: the same dirty set,
    or `None` to DECLINE, in which case the caller keeps its
    `git status --porcelain` spawn.

    ONE WALK OF THE INDEX, and a scoped one. The first cut of this was
    reverted for spending 134ms of process time to answer about three
    paths, against roughly 60ms of child CPU for the spawn it replaced: it
    composed three ready-made helpers, each of which parsed a 37k-entry
    index in full, to look at three of them. `parse_index_identity` exists
    so this reads `(mode, sha, size, mtime, mtime_nsec)` for exactly the
    wanted paths in a single early-exiting walk -- 18.8ms here.

    BOTH AXES, because that is what porcelain answers. `git status` reports
    a path dirty for index-vs-HEAD (staged, uncommitted) OR
    worktree-vs-index (unstaged) alike, and this rail needs both -- a
    survivor whose bytes are staged but not committed is exactly as unsafe
    to move as one with unstaged edits. Answering only the worktree axis
    would silently narrow the rail rather than speed it up.
      - index vs HEAD  -> `(mode, sha)` from this walk against
        `git_state.head_blobs`, which is spawn-free only because C2b taught
        it to read objects in-process instead of spawning `git ls-tree`.
      - worktree vs index -> `os.stat` against the entry's stat identity,
        with a mismatch settled by `content_hash.content_matches_index_sha`.

    DECLINE IS LOAD-BEARING, NOT AN ERROR PATH. `content_matches_index_sha`
    returns `None` for anything outside its verified precondition set
    (`core.autocrlf` not exactly `true`, any `text`/`-text`/`eol=` pin, any
    `filter=` clean pipeline, an unreadable path, and `autocrlf=input`,
    still unverified). One such path declines the WHOLE call rather than
    being guessed at: that keeps this an optimisation of a question the op
    could already answer, never a narrowing of which repos it can answer it
    for. An unparseable/v4 index declines the same way.

    FAIL-CLOSED IS THE CALLER'S JOB, NOT THIS FUNCTION'S. A decline returns
    `None` (fall back and ask git), never `set(ordered)` -- the
    all-survivors-dirty degradation belongs to an actual git FAILURE, and
    conflating "we chose not to answer" with "git could not answer" would
    refuse every survivor on any stock non-`autocrlf=true` box.
    """
    try:
        index = parse_index_identity(worktree, wanted=ordered)
        head = head_blobs(worktree, list(ordered))
    except (_IndexParseError, _StateIndexParseError):
        return None

    dirty: set = set()
    for rel in ordered:
        entry = index.get(rel)
        if entry is None:
            dirty.add(rel)
            continue

        if head.get(rel) != (entry.mode, entry.sha):
            dirty.add(rel)
            continue

        try:
            st = (worktree / rel).stat()
        except OSError:
            dirty.add(rel)
            continue

        stat_matches = entry.size == (st.st_size & 0xFFFFFFFF) and entry.mtime == int(
            st.st_mtime
        )
        if stat_matches and entry.mtime_nsec:
            stat_matches = entry.mtime_nsec == st.st_mtime_ns % 1_000_000_000
        if stat_matches:
            continue

        matches = content_matches_index_sha(worktree, rel, entry.sha)
        if matches is None:
            return None
        if not matches:
            dirty.add(rel)
    return dirty


def _dirty_handoff_relpaths(
    worktree: Path,
    survivor_relpaths: Sequence[str],
    *,
    fallback_pathspecs: Sequence[str] = ("state/handoffs",),
) -> set:
    """ONE scoped `git status --porcelain` call over `survivor_relpaths` —
    the classify-first reorder (C3): this is only invoked once classification
    has already narrowed the corpus to its surviving candidates, so the
    pathspec is bounded by the survivor count rather than the whole
    `state/handoffs` tree.

    PATHSPEC-BOUNDED, NEVER CHUNKED (staff-eng Finding 8): passing one
    pathspec argument per survivor risks the Windows argv cap once
    classification admits more than a few dozen candidates. Rather than
    chunk into multiple `git status` spawns (which would break the "at most
    one spawn" contract this rail exists to hold), a survivor set whose
    total pathspec byte cost exceeds `_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS`
    falls back to the CURRENT unscoped call
    (`git status --porcelain -- <fallback_pathspecs>`) — semantically
    identical to the predecessor's own corpus-wide call, still exactly one
    spawn, no new fail-open path.

    `fallback_pathspecs` (2026-08-30, the actioned-memo class gets an
    occasion, C2): the overflow-branch pathspec, generalised from the
    hardcoded `["state/handoffs"]` so a caller unioning a second corpus
    (e.g. `cross-repo/inbox`) into `survivor_relpaths` gets that corpus
    covered by the fallback too. Defaults to the original single-element
    tuple, so the sole pre-existing caller (this module's own handoff-only
    scan) is unchanged. Passing the WRONG (narrower) fallback here is a
    fail-OPEN hazard, not a cosmetic gap: a dirty path outside every listed
    pathspec can never appear in the unscoped call's own porcelain output,
    so it can never land in the returned dirty set — a caller unioning a
    second corpus's relpaths into `survivor_relpaths` MUST also list that
    corpus's root here, or an uncommitted file in it is silently treated as
    clean once the survivor set is large enough to overflow the budget.

    Returns the set of repo-relative paths (drawn from `survivor_relpaths`,
    or discovered in the unscoped fallback's own output) that carry
    uncommitted worktree changes. A dirty candidate is retained rather than
    archived this pass — moving and committing a handoff whose on-disk
    content has diverged from HEAD risks either committing content nobody
    has reviewed/staged themselves, or landing on `archive_and_commit`'s own
    disk/HEAD drift refusal at act time (a wasted move attempt this
    classification-time check avoids queuing in the first place).

    FAIL-CLOSED on any git failure (staff-eng Finding 8): a non-zero exit or
    launch failure on EITHER form returns `set(survivor_relpaths)` — every
    survivor refused as dirty — never an empty set. An empty dirty set from a
    failed git call would be fail-open (every survivor sails through
    unexcluded because the check that was supposed to gate them silently
    answered "nothing is dirty"), which is exactly the mode this rail exists
    to refuse.
    """
    if not survivor_relpaths:
        return set()

    ordered = sorted(set(survivor_relpaths))
    total_chars = sum(len(p) + 1 for p in ordered)
    if total_chars <= _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS:
        scoped_paths: List[str] = ordered
        in_process = _dirty_relpaths_in_process(worktree, ordered)
        if in_process is not None:
            return in_process
    else:
        scoped_paths = list(fallback_pathspecs)

    return dirty_relpaths_from_porcelain(
        worktree, scoped_paths,
        fail_closed_defaults=ordered,
        caller="archive_terminal_handoffs",
    )


# THE PRE-FILTER LIVES ENTIRELY HERE. It never replaces, wraps, or narrows
# It may only ever produce a SUPERSET of what `_classify_branch` over a full

_PREFILTER_READ_BYTES = 4096

_PREFILTER_NON_STRING_SCALARS = frozenset({"null", "~", "true", "false"})


def _prefilter_plain_scalar(raw: str) -> Optional[str]:
    if not raw or "#" in raw or raw[0] in "'\"|>[{&*!":
        return None
    if raw in _PREFILTER_NON_STRING_SCALARS:
        return None
    try:
        int(raw)
        return None
    except ValueError:
        pass
    try:
        as_float = float(raw)
        if math.isfinite(as_float):
            return None
    except ValueError:
        pass
    return raw


def _prefilter_qualifies(status_lower: str, deployment_lower: str) -> bool:
    """Mirror `_classify_branch`'s own True/False qualify decision, using
    ONLY the two keys the pre-filter extracts — never a re-derived rule.

    Over-admits deliberately: a `deployment_state` already in the terminal
    set always returns True here even for the "shipped" sub-case (which
    `_classify_branch` may still refuse on an unresolvable `shipped_in`) —
    that refinement needs the full parse, and this function's only
    obligation is superset-safety, not the final answer.

    `status_lower` is accepted but no longer read (C3, docs/reference/
    handoff-legal-state-table.md § "Ruling: terminality is a
    deployment_state question, never a status one"): `_classify_branch`'s
    own Branch A no longer qualifies a claimed/consumed record by
    `deployment_state != "in_flight"` — that inversion was the census-row-1
    bug this pre-filter must not resurrect a copy of — so `deployment_state`
    membership in `_TERMINAL_DEPLOYMENT_STATES` is the whole test, same as
    Branch B. Kept as a positional parameter so the call site's two-value
    shape (and this function's signature as the third documented copy of
    the predicate, per the module-level "THE PRE-FILTER LIVES ENTIRELY
    HERE" note) stays stable for any external caller/test holding a
    reference to it.
    """
    if deployment_lower in _TERMINAL_DEPLOYMENT_STATES:
        return True
    return False


def _prefilter_scan_disqualifies(path: Path) -> Optional[str]:
    """Cheap byte-level pre-check answering `_classify_branch`'s own
    disqualifying question without paying for `dag._read_meta`'s full
    read+hash+YAML parse.

    Returns a `_SCAN_REASON_NOT_TERMINAL`-prefixed refusal reason when both
    `status` and `deployment_state` are readable as plain, top-level,
    single-line scalars whose values definitively fail `_prefilter_
    qualifies` — else returns None, meaning "uncertain, fall through to the
    full parse". THE FALL-THROUGH LIST IS CLOSED (staff-eng Finding 6): no
    leading `---` on line 1; no closing delimiter within the read budget; a
    value that is not a plain unquoted single-line scalar (quoted, block,
    flow, anchor/alias/tag, or one `dag._parse_scalar` would coerce to a
    non-string); a tab in the frontmatter block's indentation; a duplicate
    key; or either target key absent. Every one of those returns None here,
    never a guessed reason.
    """
    try:
        with open(path, "rb") as f:
            chunk = f.read(_PREFILTER_READ_BYTES)
    except OSError:
        return None

    if not chunk.startswith(b"---"):
        return None

    text = chunk.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines[0].rstrip("\r") != "---":
        return None

    close_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            close_idx = i
            break
    if close_idx is None:
        return None

    block_lines = lines[1:close_idx]
    if any("\t" in ln for ln in block_lines):
        return None

    found_raw: Dict[str, str] = {}
    for ln in block_lines:
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip(" "))
        if indent != 0:
            continue
        colon_idx = stripped.find(":")
        if colon_idx == -1:
            continue
        key = stripped[:colon_idx].strip()
        if key not in ("status", "deployment_state"):
            continue
        if key in found_raw:
            return None
        found_raw[key] = stripped[colon_idx + 1:].strip()

    if "status" not in found_raw or "deployment_state" not in found_raw:
        return None

    status_scalar = _prefilter_plain_scalar(found_raw["status"])
    deployment_scalar = _prefilter_plain_scalar(found_raw["deployment_state"])
    if status_scalar is None or deployment_scalar is None:
        return None

    status_lower = status_scalar.strip().lower()
    deployment_lower = deployment_scalar.strip().lower()

    if _prefilter_qualifies(status_lower, deployment_lower):
        return None

    if deployment_lower == "in_flight" and status_lower in ("claimed", "consumed"):
        return f"{_SCAN_REASON_NOT_TERMINAL}: deployment_state=in_flight — not terminal (archive-safety)"
    return (
        f"{_SCAN_REASON_NOT_TERMINAL}: status={status_scalar!r} and "
        f"deployment_state={deployment_scalar!r} (not terminal)"
    )


def _classify_branch(meta: dict, shipped_in_resolved: Dict[str, bool]) -> Tuple[Optional[bool], str, str, bool]:
    """Branch A/B qualification — since Checks 3 and 4 were deleted, the whole
    of the archival criterion.

    Returns (qualifies, reason_if_not, status_label_if_qualifies, branch_b_qualified).
    `reason_if_not` arrives ALREADY family-prefixed (`_SCAN_REASON_*`), so the
    caller records it verbatim — a refusal that is terminal-but-retained must
    not inherit the caller's `not-terminal` family, which is the whole live
    corpus and drowns it.
    `qualifies` is None (never True/False confusion) only when genuinely
    indeterminate frontmatter retains — modeled here as qualifies=False with
    an explicit reason, per "indeterminate frontmatter retains, fail-closed".
    """
    status = meta.get("status")
    normalized_status = (status or "").strip().lower()
    deployment_state = (meta.get("deployment_state") or "").strip().lower()

    if deployment_state in _TERMINAL_DEPLOYMENT_STATES:
        if deployment_state == "shipped":
            shipped_in = meta.get("shipped_in")
            if not shipped_in or not shipped_in_resolved.get(str(shipped_in).strip(), False):
                sha = str(shipped_in).strip() if shipped_in else ""
                return (
                    False,
                    f"{_SCAN_REASON_SHIPPED_IN_UNRESOLVABLE}: deployment_state=shipped "
                    + (
                        f"but shipped_in {sha!r} names no object in this repo "
                        "(abbreviations resolve from 7 hex; shorter, non-hex, or "
                        "another repo's sha does not)"
                        if sha
                        else "but shipped_in is empty"
                    )
                    + " — retained (fail-closed)",
                    "",
                    False,
                )
        return True, "", deployment_state, True

    # _TERMINAL_DEPLOYMENT_STATES. The old test — "terminal unless
    if normalized_status in ("claimed", "consumed") and not deployment_state:
        return True, "", "consumed", False

    return (
        False,
        f"{_SCAN_REASON_NOT_TERMINAL}: status={status!r} and "
        f"deployment_state={deployment_state!r} (not terminal)",
        "",
        False,
    )


def _terminal_since(meta: dict, handoff_path: Path) -> Optional[str]:
    for field in ("shipped_at", "claimed_at", "updated", "created"):
        val = meta.get(field)
        if val:
            return str(val)
    try:
        mtime = handoff_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        print(
            f"skip: _terminal_since: mtime = handoff_path.stat().st_mtime failed: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        return None


def _sort_key(terminal_since: Optional[str]) -> str:
    return terminal_since if terminal_since else "9999-99-99T99:99:99Z"


def _scan_terminal(
    worktree_root: Path,
    common_dir: Path,
    *,
    scan_errors: Optional[List[str]] = None,
    known_dirty_relpaths: Optional[set] = None,
    skipped: Optional[List[dict]] = None,
) -> List[Tuple[Path, str, str, Optional[str]]]:
    """Return every terminal candidate — UNCAPPED, oldest-first — as
    (path, note, status_label, terminal_since) tuples.

    Terminal is the WHOLE criterion. Neither a candidate's children nor the
    liveness of whoever holds its claim narrows this set any more; both gates
    stood in the loop below and both were deleted on PM ruling (2026-08-28 and
    2026-09-04). Their deletion notes are inline at the sites.

    Cap enforcement happens in the handler, over this function's already-
    sorted output, so the same ordering rule serves both dry_run:true preview
    (which candidates are the ones this cap will act on next) and dry_run:false
    act (which candidate_ids this invocation is willing to move).

    CLASSIFY FIRST, DIRTY-CHECK ONLY THE SURVIVORS (C3, staff-eng Finding 1).
    Branch A/B classification runs over the WHOLE corpus first — it is pure
    in-memory frontmatter comparison, no git spawn — and only the surviving
    candidate relpaths are handed to Rail 1's worktree-dirty check, so the
    dirty check's pathspec (and, on the standalone path, the ONE `git
    status --porcelain` spawn it costs) scales with the survivor count, not
    the corpus. A record refused by classification never reaches the dirty
    rail: for a record that is BOTH worktree-dirty AND non-terminal this is
    a DESIGNED reason-precedence change from the predecessor (not-terminal
    now wins, per AC-5's amended assertion), not a regression. The lazy
    deferral that used to sit below this — a reverse-edge index build and a
    `resolve_live_session_ids()` call, both held back until a candidate
    survived — is gone with the two gates that needed it. There is nothing
    left after the dirty rail to defer.

    `known_dirty_relpaths` (C10, AC-11): the standalone/op path leaves this
    None and this function spawns its own scoped `git status --porcelain`
    over the survivor set (Rail 1, `_dirty_handoff_relpaths`) — unchanged in
    spawn count (still exactly one). An in-plane caller that has ALREADY
    computed worktree divergence for these same paths this invocation
    (`commit_pipeline`/`commit_scoped`'s own `_diverging_paths_chunked`/
    `diverging_paths` pass) passes that answer through here instead, so this
    rail spawns nothing — reusing the existing computation rather than
    asking the same question twice. Do NOT reimplement this rail's own
    git-status logic on the in-plane path; feed it the candidate set's
    divergence answer.

    `skipped` — opt-in out-param. When a list is supplied, every rail that
    refuses a candidate appends `{id, reason}` to it, so a refusal is
    observable from outside instead of vanishing into a bare `continue`.
    This channel carries the whole live corpus's non-terminal population
    (the bulk of it), so it is deliberately NOT wired to any wire-building
    caller — see the `_SCAN_REASON_*` block's negative spec.

    ONE pass over collect_live_handoff_paths(worktree_root), pre-filtered
    (C4, `_prefilter_scan_disqualifies`): the cheap byte-level pre-check
    answers `_classify_branch`'s own disqualifying question for the ~96% of
    records it can, so `dag._read_meta`'s full read+hash+parse is paid only
    for a pre-filter survivor here. The full-corpus `metas` backfill that
    used to follow — a `_read_meta` for every live node not already read,
    solely to feed a reverse-edge index — went with Check 3, so a
    pre-filter-refused record is now never read at all. The shipped_in shas
    needing the ONE batch resolvability check, and Branch A/B qualification
    per candidate, both come from the pre-filter survivor set.
    """
    _require_out_accumulator("skipped", skipped)
    _require_git_common_dir(common_dir)
    results: List[Tuple[Path, str, str, Optional[str]]] = []
    try:
        live_paths = collect_live_handoff_paths(worktree_root)
    except OSError as exc:
        live_dir = worktree_root / "state" / "handoffs"
        _LOG.warning(
            "_scan_terminal: cannot scan live handoffs under %s — %s; "
            "returning zero candidates (degrade safe)", live_dir, exc,
        )
        if scan_errors is not None:
            scan_errors.append(f"{live_dir}: {exc}")
        return results
    if not live_paths:
        return results

    # pre-filter SURVIVOR does. The backfill that used to top this up for the
    metas: Dict[str, dict] = {}
    live_set_str: List[str] = [str(p) for p in live_paths]

    def _refuse(candidate_id: str, reason: str) -> None:
        if skipped is not None:
            skipped.append({"id": candidate_id, "reason": reason})

    # BOTH worktree-dirty AND non-terminal this is a DESIGNED
    prefilter_survivors: List[Tuple[Path, str, dict]] = []
    for p, key in zip(live_paths, live_set_str):
        rel = rel_id(p, worktree_root)
        prefilter_reason = _prefilter_scan_disqualifies(p)
        if prefilter_reason is not None:
            _refuse(rel, prefilter_reason)
            continue
        meta = _read_meta(str(p)) or {}
        metas[key] = meta
        prefilter_survivors.append((p, rel, meta))

    shipped_in_shas = [
        str(meta.get("shipped_in")).strip()
        for _p, _rel, meta in prefilter_survivors
        if (meta.get("deployment_state") or "").strip().lower() == "shipped" and meta.get("shipped_in")
    ]
    shipped_in_resolved = _batch_resolve_shipped_in(common_dir, shipped_in_shas)

    survivors: List[Tuple[Path, str, dict, str, bool]] = []
    for p, rel, meta in prefilter_survivors:
        qualifies, reason, status_label, branch_b_qualified = _classify_branch(meta, shipped_in_resolved)
        if not qualifies:
            _refuse(rel, reason)
            continue
        survivors.append((p, rel, meta, status_label, branch_b_qualified))

    if not survivors:
        return results

    # Rail 1 — worktree-dirty exclusion, scoped to SURVIVORS ONLY (C3).
    if known_dirty_relpaths is not None:
        dirty_relpaths = known_dirty_relpaths
    else:
        dirty_relpaths = _dirty_handoff_relpaths(worktree_root, [rel for _p, rel, _m, _s, _b in survivors])

    remaining: List[Tuple[Path, str, dict, str, bool]] = []
    for p, rel, meta, status_label, branch_b_qualified in survivors:
        if rel in dirty_relpaths:
            _refuse(rel, _SCAN_REASON_WORKTREE_DIRTY)
            continue
        remaining.append((p, rel, meta, status_label, branch_b_qualified))

    if not remaining:
        return results


    for p, rel, meta, status_label, _branch_b_qualified in remaining:
        # SUCCESSION child no longer retained a Branch-B candidate, leaving only

        terminal_since = _terminal_since(meta, p)
        note = status_label
        results.append((p, note, status_label, terminal_since))

    results.sort(key=lambda t: _sort_key(t[3]))
    return results


def plan_sweep(
    worktree_root: Path,
    common_dir: Path,
    cap: int,
    *,
    candidate_ids: Optional[List[str]] = None,
    known_dirty_relpaths: Optional[set] = None,
    scan_skipped: Optional[List[dict]] = None,
) -> Tuple[List[Move], List[dict]]:
    """Classification-only planning: scan, cap-slot, and every exclusion
    rail — everything `_handle_act` used to do UP TO building its `moves`
    list. Mutates nothing, commits nothing, spawns nothing beyond what
    `_scan_terminal` itself spawns.

    `candidate_ids=None` — the in-plane path: every terminal candidate,
    cap-slotted oldest-first straight off `_scan_terminal`'s own ordering.

    `candidate_ids=<sequence>` — the op/act path: today's `_handle_act`
    re-verify/defer/duplicate semantics. Only this mode can emit the
    `deferred-cap`/`duplicate-candidate-id`/re-verify reasons that are a
    function of caller-supplied candidate_ids.

    `known_dirty_relpaths` (C10, AC-11) — passed straight through to
    `_scan_terminal`; see that function's own docstring. A provided set
    answers Rail 1 outright and spawns nothing. None (default) leaves Rail 1
    to `_dirty_handoff_relpaths`, which is ALSO normally spawn-free: its
    `_dirty_relpaths_in_process` arm answers from one scoped index walk, and
    the `git status --porcelain` spawn is a FALLBACK, taken only when that arm
    declines or the survivor pathspec exceeds
    `_DIVERGENCE_CHECK_ARGV_BUDGET_CHARS`. So the honest bound is AT MOST one
    spawn, not exactly one — and zero whenever classification leaves no
    survivor, since the rail is never invoked at all. Rail 2 spawns none
    either way.

    `scan_skipped` (opt-in out-param) — `_scan_terminal`'s own rail
    refusals, `{id, reason}` per refused record. Separate from the returned
    `skipped` because it carries the whole non-terminal population, which
    the cockpit wire must not: a wire-building caller passes nothing here
    and its envelope is byte-unchanged, while a diagnostic caller (the
    CLI) passes a list and sees why every record it did not archive was
    refused. Discharges AC-2's "every rail names itself".

    Returns (moves, skipped) — `moves` are `Move` objects ready for
    `apply_sweep` or `archive_and_commit`; `skipped` is a list of
    `{id, reason}` using the same reason strings that ship today, unchanged.
    """
    _require_out_accumulator("scan_skipped", scan_skipped)
    rails: List[dict] = scan_skipped if scan_skipped is not None else []
    terminal = _scan_terminal(
        worktree_root, common_dir, known_dirty_relpaths=known_dirty_relpaths,
        skipped=rails,
    )
    rail_reason = {entry["id"]: entry["reason"] for entry in rails if "id" in entry}
    terminal_by_id = {rel_id(p, worktree_root): (p, note) for p, note, _label, _ts in terminal}

    moves: List[Move] = []
    skipped: List[dict] = []

    def _plan_one(cid: str) -> None:
        handoff_path, _note = terminal_by_id[cid]
        dst = handoff_archive_dest(worktree_root, handoff_path)
        force = False
        if dst.exists():
            if not _is_identical_duplicate(handoff_path, dst):
                _LOG.warning(
                    "archive_terminal_handoffs: %s NOT archived — a DIFFERENT file already "
                    "occupies the archive destination %s.",
                    cid, rel_id(dst, worktree_root),
                )
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                return
            force = True
        moves.append(Move(src=handoff_path, dst=dst, candidate_id=cid, force=force))

    if candidate_ids is None:
        ordered_ids = list(terminal_by_id.keys())
        deferred_ids = set(ordered_ids[cap:])
        for cid in ordered_ids:
            if cid in deferred_ids:
                skipped.append({"id": cid, "reason": f"deferred-cap: invocation cap ({cap}) reached"})
                continue
            _plan_one(cid)
        return moves, skipped

    requested_set = set(candidate_ids)
    # requested ids in OLDEST-FIRST terminal order get applied, the rest
    oldest_first_requested = [cid for cid in terminal_by_id if cid in requested_set]
    allowed_ids = set(oldest_first_requested[:cap])
    deferred_ids = set(oldest_first_requested[cap:])

    for cid in candidate_ids:
        if cid not in terminal_by_id:
            src_guess = worktree_root / cid
            if not src_guess.exists():
                skipped.append({"id": cid, "reason": "already-archived"})
            else:
                skipped.append(
                    {
                        "id": cid,
                        "reason": rail_reason.get(
                            cid,
                            "terminality-drift: no longer classifies as terminal",
                        ),
                    }
                )
            continue
        if cid in deferred_ids:
            skipped.append({"id": cid, "reason": f"deferred-cap: invocation cap ({cap}) reached"})
            continue
        if cid not in allowed_ids:
            skipped.append({"id": cid, "reason": "duplicate-candidate-id"})
            continue
        _plan_one(cid)
        allowed_ids.discard(cid)

    return moves, skipped


def apply_planned_sweep(
    plan_result: "Tuple[List[Move], List[dict]]",
) -> Tuple[List[dict], List[dict]]:
    moves, _skipped = plan_result
    return apply_sweep(moves)


def apply_sweep(moves: List[Move]) -> Tuple[List[dict], List[dict]]:
    planned: List[Move] = moves

    acted: List[dict] = []
    failed: List[dict] = []

    for move in planned:
        if not move.force and move.dst.exists():
            failed.append({
                "id": move.candidate_id,
                "reason": "dst-exists: refusing overwrite (force=False)",
            })
            continue
        try:
            move.dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(move.src), str(move.dst))
        except OSError as exc:
            failed.append({
                "id": move.candidate_id,
                "reason": f"replace-failed: {exc}",
            })
            continue
        acted.append({"id": move.candidate_id, "archived": True})

    return acted, failed


def _handle_act(
    mode: str,
    worktree_root: Path,
    common_dir: Path,
    candidate_ids: List[str],
    cap: int,
) -> dict:
    """Act path: re-verify each candidate_id at act time, cap the moves
    ACTUALLY APPLIED this invocation to `cap` (oldest-first among the
    caller-supplied candidate_ids), and defer the rest with a first-class
    reason rather than truncating silently.

    SYNCHRONOUS (C2). `_common.archive_and_commit` was found to remain a
    coroutine (staff-eng Finding 9, not in this chunk's scope to change —
    it is a separate module under active rewrite elsewhere), so this
    function drives it at exactly ONE boundary via a single
    `asyncio.run(...)`, imported locally so the classification-only path
    (this function is reached only on dry_run:false) never pays the
    `asyncio` import cost on the far more common dry_run:true preview path.
    """
    moves, skipped = plan_sweep(worktree_root, common_dir, cap, candidate_ids=candidate_ids)

    acted: List[dict] = []
    failed: List[dict] = []

    if moves:
        import asyncio

        n = len(moves)
        commit_subject = (
            f"fleet: archive {n} terminal handoff(s)\n\n"
            f"Archived via fleet.archive_completed_handoffs (dry_run:false)."
        )
        new_acted, new_failed = asyncio.run(
            archive_and_commit(
                worktree_root=worktree_root,
                moves=moves,
                subject=commit_subject,
            )
        )
        acted.extend(new_acted)
        failed.extend(new_failed)

    return declare_move_claims(
        build_act_result(mode, acted, skipped, failed), moves, acted,
    )


@register_op("fleet.archive_completed_handoffs")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.archive_completed_handoffs — cap-bounded terminal-handoff archiver.

    Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md.

    dry_run:true  → preview: enumerate terminal candidates (Branch A/B
                    terminality alone), capped oldest-first at `cap`; excess
                    candidates are named (never silently dropped) in the
                    additive `deferred` key.
    dry_run:false → act: re-verify + move up to `cap` of the caller-supplied
                    candidate_ids, oldest-first; excess candidate_ids are
                    skipped with reason "deferred-cap", never silently
                    truncated.

    repo_root arg is the git common dir (_OP_KEY_SCOPE="common_dir").

    SYNCHRONOUS (C2, docs/plans/2026-08-26-the-sweep-stops-paying-for-a-
    room-it-nev.md § C2). `coordinator_core/ipc.py :: _dispatch` branches on
    `inspect.iscoroutinefunction(handler)`; the SYNC branch already runs
    `await asyncio.wait_for(asyncio.to_thread(handler, ...), timeout=op_timeout)`
    — the dispatcher offloads a sync handler to a thread and applies the
    per-op timeout itself. This handler declaring itself `async def` took on
    that same obligation a second time, for no benefit (none of its three
    internal `to_thread` calls awaited anything else concurrently), and paid
    a 31.3ms module-scope `import asyncio` (dragging `ssl`/`socket`) on the
    classification-only (dry_run:true) path to do it. Going sync retires the
    obligation and its import cost together; the ACT path's own
    `asyncio.run(...)` boundary is `_handle_act`'s, not this handler's.
    """
    parsed = validate_params(params)
    if isinstance(parsed, dict):
        return parsed

    mode, dry_run, candidate_ids = parsed

    cap = params.get("cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
        return build_setup_error_result(
            mode, dry_run,
            f"cap is required and must be a positive int, got {cap!r} — "
            f"no unbounded default (see plan's C0 cap-axis decision)",
        )

    if repo_root is None:
        _LOG.error("fleet.archive_terminal_handoffs: repo_root handler arg is None")
        return build_setup_error_result(mode, dry_run, "repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return build_setup_error_result(mode, dry_run, mismatch)

    lock_path = _acquire_sweep_lock(common_dir)
    if lock_path is None:
        if dry_run:
            result = build_dry_run_result(mode, [])
        else:
            result = build_act_result(mode, [], [], [])
        result["contended"] = True
        return result

    try:
        if dry_run:
            terminal = _scan_terminal(worktree, common_dir)
            accepted = terminal[:cap]
            deferred = terminal[cap:]
            candidates = []
            for handoff_path, note, status_label, terminal_since in accepted:
                candidates.append({
                    "id": rel_id(handoff_path, worktree),
                    "title": (_read_meta(str(handoff_path)) or {}).get("title") or handoff_path.stem,
                    "status": status_label,
                    "family": _FAMILY,
                    "terminal_since": terminal_since,
                    "note": note,
                })
            result = build_dry_run_result(mode, candidates)
            if deferred:
                result["deferred"] = {
                    "count": len(deferred),
                    "ids": [rel_id(p, worktree) for p, _n, _s, _t in deferred],
                }
            return result

        if candidate_ids is None:
            return build_setup_error_result(
                mode, dry_run,
                "candidate_ids resolved to None on the act path after "
                "validate_params accepted it — contract violation, refusing",
            )

        return _handle_act(mode, worktree, common_dir, candidate_ids, cap)
    finally:
        _release_sweep_lock(lock_path)
