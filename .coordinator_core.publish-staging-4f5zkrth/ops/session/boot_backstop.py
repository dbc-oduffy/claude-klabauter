"""
coordinator_core.ops.session.boot_backstop — session.boot_sweep op (rebuilt mechanism).

Purpose: replace the composite session.boot_sweep archival sweep (formerly
coordinator_core/ops/session/boot_sweep.py, suspended for its own 30s-timeout
breach history — op_census.breaches: 164 of 164 runs over 9.5s, 122 non-ok)
with the spike-proven, zero-git-query-spawn mechanism: Python enumerates
state/handoffs/*.md, selects on frontmatter alone, moves each terminal
candidate through the touch-claim-aware relocation helper, then records the
whole batch as ONE scoped `git add -A` plus ONE `git commit`. See
docs/research/spike-verdicts/2026-08-22-boot-backstop-without-a-history-walk.md.

This module is C4a of
docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md — "the
spike-proven mechanism only, measured against AC3". The surviving DR-084
consumed-handoff semantics (30-minute claimed_at recency floor, non-heir
in_flight skip-and-surface, the orphan-sweep-notes WARN marker, heir
(b)-suppression) are layered onto this mechanism by C4b, in this same file —
they are NOT present yet. (c) best-effort shipped_in stamping and (f) the
heir pre-stamp pass never return to this module at all; per DR-353 they
become ceremony-gate passengers under C3/AC6f.

Op id is PRESERVED across the rebuild: this module renames the FILE
(boot_sweep.py -> boot_backstop.py) but registers the SAME op id,
"session.boot_sweep" — AC10/AC11/C7 of the gated plan act on that id
staying stable, and the DoE-claude SessionStart hook
(coordinator/hooks/hooks.json:24 -> bin/sweep-boot.py) invokes it by name.
Changing the id would silently break that hook, orphan the SUSPENDED_OPS
row rather than letting C7 remove it, and leave AC11 describing an op that
no longer exists.

Self-registration: importing this module calls
register_op("session.boot_sweep", _handler) as a side-effect. NOT yet wired
into coordinator_core/ops/__init__.py's eager-import list — that repoint,
and the deletion of the boot_sweep.py module this replaces, is plan chunk
C5. Until C5 lands, this module is reachable only by direct import (tests,
and C4b's own additions), never through the live op registry.

Spec backlinks:
  - Plan (C4a): docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md § C4a
  - Plan (C4b): docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md § C4b
  - Spike: docs/research/spike-verdicts/2026-08-22-boot-backstop-without-a-history-walk.md
  - AC0: stated in terms of what a caller loses without it — a backstop for
    artifacts whose session died without a terminal flip, not bulk archival.
  - AC1: zero git QUERY spawns for the backstop's own selection/move work.
  - AC1b: every relocation routes through
    coordinator_core.session.scope.relocate_touched_path, never a bare
    os.replace/shutil.move/Path.rename — enforced by the repo-wide static
    AST guard coordinator_core/session/tests/test_no_untracked_relocation.py.
  - AC3: the backstop's own work (enumerate, frontmatter-select, move)
    measures under 200ms of process time cold, excluding the commit and
    excluding C4b's DR-084 substrate.
  - AC5 (C4b): the DR-084 (a),(b),(d),(e) consumed-handoff semantics survive
    the rebuild — 30-minute claimed_at recency floor, non-heir in_flight
    skip-and-surface (awaiting-adjudication-dr084), the
    tasks/orphan-sweep-notes.md WARN marker, and heir (b)-suppression.

C4b substrate (this module, layered onto C4a's mechanism):
  - `dag_index` is built via `archive_handoffs._collect_all_handoff_paths`
    (REUSE, not reimplementation) over the combined live + archive corpus —
    the same ~870-handoff (265 live + 605 archived, 2026-08-22 measurement)
    enumeration the retired composite built for its own heir classification.
  - Heir classification is `archive_handoffs._classify_heir_children`
    (REUSE) — a fail-closed ("error" -> retain) partition of a candidate's
    referencing children into heir / fork-only / childless, built on
    `archival.reverse_membership`, which is a pure in-memory frontmatter
    scan (no git spawn of any kind, query or otherwise) — this is why
    reusing it, rather than `archive_handoffs._is_terminal` in full,
    preserves AC1: `_is_terminal`'s Branch-B and heir arms additionally call
    `_shipped_in_resolvable` (a `git cat-file -e` QUERY spawn) to verify a
    `shipped_in` commit resolves, which this module's own AC1 forbids. This
    chunk therefore reuses the heir-classification primitive by name (as its
    body instructs) without pulling in `_is_terminal`'s own git-spawning
    shipped_in-resolvability arm — see Negative-spec below.
  - The 30-minute claimed_at recency floor (a), the WARN marker (d), and the
    heir deployment_state:shipped stamp (e) are local, frontmatter-only
    reimplementations of the retired composite's
    `_is_consumed_at_too_recent` / `_append_warn_marker` /
    `_set_deployment_state`, for the same import-budget reason `_archive_dest`
    above gives (this chunk's own writes: scope is this module and its test
    file only). (a) intentionally omits the retired composite's C5a
    claim-ledger cross-check (`coordinator_core.claim_state.resolve_claim_state`)
    — that refinement is a separate, later concern, not named among this
    chunk's (a)/(b)/(d)/(e) scope.

Negative-spec:
  - Does NOT walk git history. No `_git_path_ever_tracked`, no
    `build_git_history_cache`, no per-path `git log` — the tracked/untracked
    question the old composite answered via
    coordinator_core.ops.fleet._common.archive_and_commit is never asked at
    all: a backstop only ever archives files present on disk, so the
    disk-absent case that history walk exists to answer cannot arise here.
  - Does NOT route through coordinator_core.ops.fleet._common's
    archive_and_commit / rm_and_commit — reaching that shared family is
    exactly what pulled the boot path into dag.py's history-spawn machinery
    (see the plan's Cross-plan coordination section). This module does its
    own move-then-commit instead. `_archive_dest` below is a deliberate
    LOCAL reimplementation of that module's `handoff_archive_dest`, not an
    import of it — see `_archive_dest`'s own docstring.
  - Does NOT use `git mv` — a plain in-process move plus one scoped
    `git add -A` produces the identical R100 rename records git's own
    similarity detection derives for free (spike: 18/18 measured as R100).
  - Does NOT carry the DR-084 consumed-handoff semantics yet (recency floor,
    skip-and-surface, WARN marker, heir suppression) — those are C4b, added
    to this same module. This chunk's selection predicate is the plain
    terminal-status/deployment_state test only (`_is_archival_candidate`).
    [C4b note: the DR-084 layer above is now present; `_is_archival_candidate`
    stays the plain prefilter it always was — the DR-084 refinements run
    AFTER it, over the candidates it already selected.]
  - C4b does NOT call `archive_handoffs._is_terminal` — only
    `_classify_heir_children` (see module docstring above). Calling
    `_is_terminal` for a Branch-B (`deployment_state in {shipped, ...}`) or
    heir-qualified candidate would spawn `git cat-file -e` via
    `_shipped_in_resolvable`, which AC1's zero-git-QUERY-spawn contract
    forbids unconditionally, not just for the plain-mechanism enumeration
    C4a covers. C4b's heir (e) suppression always stamps `shipped`
    unconditionally (mirrors the retired composite's FIX-1 revision, which
    made `_resolve_heir_deployment_state` single-valued) rather than
    re-deriving a resolvable-shipped_in verdict, so no resolvability check is
    needed here at all.
  - Does NOT relocate any of the five cadence riders or the session.reap
    sub-reap the old composite hosted (priority_drain, fold_observed_set,
    handoff_reconcile, the eol riders, drain_pending_push) — those are C3's
    ceremony-gate passengers, orthogonal to this chunk.
  - Does NOT support the two-repo state_common_dir split the old composite
    carried (AC7 there) — this chunk operates against a single worktree
    only. C4b's dag_index (live + archive corpus) is scoped to that same
    worktree; a two-repo split is not named anywhere in C4a/C4b's own task
    bodies and is left for a future chunk to add if a consumer needs it.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.contract.apply_base import (
    resolve_explicit_session_id as _resolve_explicit_session_id,
)
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.lifecycle_constants import (
    HANDOFF_TERMINAL_DEPLOYMENT,
    HANDOFF_TERMINAL_STATUS,
)
from coordinator_core.ops.fleet.archive_handoffs import (
    _classify_heir_children,
    _collect_all_handoff_paths,
)
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags

_LOG = logging.getLogger(__name__)

# DR-084 (a): a just-claimed handoff's consuming session is almost certainly
# live even if this module's own selection predicate would otherwise treat
# it as archival-eligible (heartbeat lag, session dir not yet present on
# this machine) — mirrors the retired composite's
# _CONSUMED_AT_RECENCY_FLOOR_SECONDS (coordinator_core/ops/session/boot_sweep.py).
_CLAIMED_AT_RECENCY_FLOOR_SECONDS: int = 30 * 60

# DR-084 (b): the distinct reason token for a non-heir consumed+in_flight
# candidate this module skips-and-surfaces rather than archiving or flipping
# — mirrors the retired composite's _AWAITING_ADJUDICATION_REASON.
_AWAITING_ADJUDICATION_REASON: str = "awaiting-adjudication-dr084"

# DR-084 (d): WARN-marker write target, GIT_ROOT-worktree-rooted — mirrors
# the retired composite's tasks/orphan-sweep-notes.md convention, read by
# /workday-start Step 0.8.
_ORPHAN_SWEEP_NOTES_RELPATH: Tuple[str, str] = ("tasks", "orphan-sweep-notes.md")

# Informal documentation of the tracked paths this op reads from and writes
# to — NOT the checked `GENERATES` provenance declaration
# (coordinator_core/ops/generator_provenance.py): this module never calls a
# file-write primitive directly (relocation is delegated to
# session_scope.relocate_touched_path's own shutil.move, and the archival
# record is a git subprocess, not a Python write), so it is not a
# "generator" in that module's sense. Kept for the same reason the retired
# composite carried one: a reader scanning this file's top should not have
# to reconstruct which tracked directories a boot-time op can rewrite.
MUTATES = ["state/handoffs/*.md", "archive/handoffs/**/*.md"]

# Used only when neither params.session_id nor the process's own session-id
# environment carries a real id (resolve_explicit_session_id returns None) —
# e.g. a direct-call test harness. session_scope.relocate_touched_path
# requires a non-empty session_id, and a synthetic one here degrades safely:
# core.session_dir(_FALLBACK_SESSION_ID, cwd) will not exist, so
# relocate_touched_path's own phantom-live-peer guard skips ALL claim
# bookkeeping (no touched.txt write, nothing to restate) while the move
# itself still happens — see that function's own docstring.
_FALLBACK_SESSION_ID = "session.boot_sweep-backstop"


def _strip_quotes(value: Optional[str]) -> str:
    """Trim a frontmatter scalar's surrounding quotes, mirroring the
    `.strip().strip("\\"'")` idiom the retired composite applied at every one
    of its own frontmatter-value comparisons (e.g. its `shipped`
    deployment_state check) — reproduced here rather than importing that
    module for one line.
    """
    return (value or "").strip().strip("\"'")


def _is_archival_candidate(fm_text: str) -> bool:
    """True iff this handoff's frontmatter already carries a TERMINAL
    `status` or `deployment_state` — the plain selection predicate this
    chunk proves against AC3. The liveness-aware DR-084 refinements (30-min
    recency floor, non-heir in_flight skip-and-surface, heir
    (b)-suppression) are layered on top of this predicate by C4b, not here.
    """
    status = _strip_quotes(read_fm_field(fm_text, "status"))
    if status in HANDOFF_TERMINAL_STATUS:
        return True
    deployment_state = _strip_quotes(read_fm_field(fm_text, "deployment_state"))
    return deployment_state in HANDOFF_TERMINAL_DEPLOYMENT


def _is_claimed_at_too_recent(fm_text: str) -> bool:
    """DR-084 (a): True iff `claimed_at` (old name: `consumed_at`, read as a
    fallback) resolves to a timestamp within the last 30 minutes.

    Frontmatter-only — deliberately omits the retired composite's C5a
    claim-ledger cross-check (`coordinator_core.claim_state.resolve_claim_state`);
    that refinement is not named among this chunk's (a)/(b)/(d)/(e) scope.
    Mirrors `coordinator_core.ops.session.boot_sweep._is_consumed_at_too_recent`'s
    ISO-8601 parse (trailing `Z` -> `+00:00`, never a bare `.rstrip("Z")`,
    which would misread an explicit `+HH:MM` offset as UTC).

    Returns False (proceed with archival) when the field is absent or the
    value fails to parse — "only skip when fresh" (mirrors the deleted
    session-init.sh, DoE 2f8b8450).
    """
    raw = read_fm_field(fm_text, "claimed_at") or read_fm_field(fm_text, "consumed_at")
    raw_str = _strip_quotes(raw).strip()
    if not raw_str:
        return False
    if raw_str.endswith("Z"):
        raw_str = raw_str[:-1] + "+00:00"
    try:
        epoch = datetime.fromisoformat(raw_str).timestamp()
    except (ValueError, AttributeError):
        return False
    if epoch <= 0.0:
        return False
    now = datetime.now(tz=timezone.utc).timestamp()
    return (now - epoch) < _CLAIMED_AT_RECENCY_FLOOR_SECONDS


def _stamp_deployment_state_shipped(handoff_path: Path) -> bool:
    """DR-084 (e): idempotently stamp `deployment_state: shipped` on a heir
    candidate's OWN frontmatter, in place, before it is queued for the
    batch move+commit.

    Unconditionally "shipped" — mirrors the retired composite's FIX-1
    revision (`_resolve_heir_deployment_state`, single-valued as of
    2026-07-22): a heir candidate's own H4-equivalent eligibility was
    already proved by `_classify_heir_children` returning "heir" before this
    is called, so no further shipped_in-resolvability check (which would
    spawn `git cat-file -e`, forbidden by AC1 — see module docstring
    negative-spec) is needed here.

    Best-effort: returns False (never raises) on any read/write failure or a
    frontmatter-less file, mirroring the retired composite's
    `_set_deployment_state`, reimplemented locally for the same
    import-budget reason `_archive_dest` below gives.
    """
    try:
        text = handoff_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _LOG.debug(
            "boot_backstop: could not read %s for deployment_state stamp: %s",
            handoff_path, exc,
        )
        return False
    split = split_frontmatter(text)
    if split is None:
        return False
    if read_fm_field(split.fm_text, "deployment_state") is not None:
        new_fm = replace_fm_field(split.fm_text, "deployment_state", "shipped")
    else:
        new_fm = insert_fm_field(
            split.fm_text, "deployment_state", "shipped", after_key="status"
        )
    new_text = rebuild(split, new_fm)
    try:
        handoff_path.write_text(new_text, encoding="utf-8", newline="\n")
    except OSError as exc:
        _LOG.debug(
            "boot_backstop: could not write %s for deployment_state stamp: %s",
            handoff_path, exc,
        )
        return False
    return True


def _append_warn_marker(
    worktree: Path,
    handoff_filename: str,
    sid: str,
    disposition_note: str,
    verb: str = "archived",
) -> None:
    """DR-084 (d): append a WARN marker for one consumed handoff to
    tasks/orphan-sweep-notes.md — /workday-start Step 0.8 reads (and
    rotates) this file to surface orphaned workstream handoffs archived
    without closure ceremony, and (b) skip-and-surface candidates awaiting
    adjudication.

    Creates the file with a header if it does not yet exist. Fails silently
    on any I/O error — this runs on the session-boot path and must never let
    a marker write wedge the boot.

    Dedupe: before appending, checks whether a row for the same
    (handoff_filename, sid, verb) triple already exists in the file's
    CURRENT on-disk contents, and skips the write if so —
    `disposition_note` is deliberately NOT part of the dedup key. Reads
    ONLY the live file at call time, never history or a side-index (a
    rotated-out row is no longer "already logged" from the new file's point
    of view, and a post-rotation re-log of the same triple must still
    happen). Mirrors the retired composite's `_append_warn_marker`,
    reimplemented locally for the same import-budget reason `_archive_dest`
    below gives.

    verb: "archived" (default, for moved items) or "skipped" (for (b)
    skip-and-surface candidates, never archived at all).
    """
    marker_dir = worktree / _ORPHAN_SWEEP_NOTES_RELPATH[0]
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.debug("boot_backstop: could not create %s: %s", marker_dir, exc)
        return

    marker_path = marker_dir / _ORPHAN_SWEEP_NOTES_RELPATH[1]
    dedup_fragment = f"| {verb} {handoff_filename} (consumed_by={sid}"
    try:
        existing_text = marker_path.read_text(encoding="utf-8") if marker_path.exists() else ""
    except OSError:
        # Degrade toward risking a duplicate row rather than dropping the
        # record — this must never wedge the session boot.
        existing_text = None

    if existing_text is not None and dedup_fragment in existing_text:
        return

    try:
        if not marker_path.exists():
            marker_path.write_text(
                "# Orphan sweep notes\n\n"
                "Archive events from session.boot_sweep. "
                "/workday-start Step 0.8 reads and rotates.\n\n",
                encoding="utf-8", newline="\n",
            )
        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with marker_path.open("a", encoding="utf-8") as fh:
            fh.write(
                f"- {ts} | {verb} {handoff_filename} "
                f"(consumed_by={sid}, {disposition_note})\n"
            )
    except OSError as exc:
        _LOG.debug("boot_backstop: could not write orphan-sweep-notes.md: %s", exc)


def _archive_dest(worktree: Path, handoff_path: Path) -> Path:
    """Derive archive/handoffs/YYYY-MM/<filename>, falling back to the flat
    archive/handoffs/<filename> form when the filename carries no leading
    YYYY-MM-DD prefix.

    Deliberately a LOCAL reimplementation of
    coordinator_core.ops.fleet._common.handoff_archive_dest, not an import
    of it: that module also defines archive_and_commit/rm_and_commit, the
    history-walking family this module's own negative-spec forbids the boot
    path from reaching, and AC3d bounds this module's own import set — this
    chunk's declared `reads:` is session/scope.py only. The two functions
    MUST stay byte-identical in behavior; see that function's own docstring
    for the naming convention this mirrors, and update both together if the
    convention ever changes.
    """
    stem_parts = handoff_path.name.split("-")
    yyyymm: Optional[str] = None
    if len(stem_parts) >= 2:
        try:
            year = int(stem_parts[0])
            month = int(stem_parts[1])
            if 1900 <= year <= 2100 and 1 <= month <= 12:
                yyyymm = f"{year:04d}-{month:02d}"
        except ValueError:
            # Non-numeric prefix — falls back to the flat archive path below.
            pass
    if yyyymm:
        return worktree / "archive" / "handoffs" / yyyymm / handoff_path.name
    return worktree / "archive" / "handoffs" / handoff_path.name


def enumerate_handoffs(worktree: Path) -> List[Path]:
    """Sorted list of every live state/handoffs/*.md file — deterministic
    order for a reproducible candidate set and a reproducible commit diff.
    """
    live_root = worktree / "state" / "handoffs"
    if not live_root.is_dir():
        return []
    return sorted(live_root.glob("*.md"))


def select_candidates(handoffs: List[Path]) -> Tuple[List[Path], List[dict]]:
    """Frontmatter-select the terminal subset of `handoffs`.

    Returns (candidates, unreadable) — `unreadable` carries one dict per
    path this could not classify (missing/unparseable frontmatter, or an
    OSError reading the file), degrading safe by EXCLUDING that path from
    `candidates` rather than guessing.
    """
    candidates: List[Path] = []
    unreadable: List[dict] = []
    for path in handoffs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            unreadable.append({"path": str(path), "reason": str(exc)})
            continue
        split = split_frontmatter(text)
        if split is None:
            unreadable.append({"path": str(path), "reason": "no parseable frontmatter"})
            continue
        if _is_archival_candidate(split.fm_text):
            candidates.append(path)
    return candidates, unreadable


def relocate_candidates(
    worktree: Path, candidates: List[Path], session_id: str
) -> Tuple[List[dict], List[dict]]:
    """Move every candidate to its archive destination via
    coordinator_core.session.scope.relocate_touched_path (AC1b) — never a
    bare os.replace/shutil.move/Path.rename.

    Returns (moved, failed): `moved` carries {"from": <repo-rel>, "to":
    <repo-rel>} for every successfully relocated path (posix-separated,
    repo-relative); `failed` carries {"path": <repo-rel>, "reason": str}
    for one whose move raised. A single failure does not abort the batch —
    the remaining candidates still get their chance, mirroring the retired
    composite's per-item failure tolerance.
    """
    moved: List[dict] = []
    failed: List[dict] = []
    for src in candidates:
        dst = _archive_dest(worktree, src)
        src_rel = src.relative_to(worktree).as_posix()
        dst_rel = dst.relative_to(worktree).as_posix()
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            session_scope.relocate_touched_path(
                session_id, src_rel, dst_rel, cwd=str(worktree)
            )
        except OSError as exc:
            failed.append({"path": src_rel, "reason": str(exc)})
            continue
        moved.append({"from": src_rel, "to": dst_rel})
    return moved, failed


def _git(worktree: Path, args: List[str]) -> subprocess.CompletedProcess:
    """The one subprocess seam this module uses for its two MUTATING git
    calls (`add -A`, `commit`) — never a query.
    `no_console_creationflags()` suppresses the Windows conhost popup a
    child process otherwise allocates (this repo's first-class platform).
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _commit_relocations(worktree: Path, moved: List[dict]) -> dict:
    """ONE scoped `git add -A` over exactly the two directories this module
    ever touches, plus ONE `git commit` — never `git mv` (git derives every
    rename as R100 from the add alone; spike-verified 18/18 on the live
    corpus) and never a per-file spawn.

    Returns {"committed": bool, "sha": None, "reason": str|None}. `sha` is
    always None — this chunk does not need it and a `git rev-parse HEAD`
    read would be a third, unneeded spawn; a caller wanting the sha can
    resolve it separately. `committed: False` with a `reason` on either git
    call's non-zero exit — this never raises; a commit failure is reported,
    not swallowed, and is not fatal to the archival that already landed on
    disk (the move already happened; only the record of it is unstaged).
    """
    if not moved:
        return {"committed": False, "sha": None, "reason": "no candidates"}

    add = _git(worktree, ["add", "-A", "--", "state/handoffs", "archive/handoffs"])
    if add.returncode != 0:
        return {
            "committed": False,
            "sha": None,
            "reason": f"git add -A failed: {add.stderr.strip()}",
        }

    message = f"session.boot_sweep backstop: archive {len(moved)} terminal handoff(s)"
    commit = _git(worktree, ["commit", "-m", message])
    if commit.returncode != 0:
        return {
            "committed": False,
            "sha": None,
            "reason": f"git commit failed: {commit.stderr.strip()}",
        }

    return {"committed": True, "sha": None, "reason": None}


async def _apply_dr084_disposition(
    worktree: Path, candidates: List[Path]
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Layer the DR-084 (a)/(b)/(e) semantics onto C4a's plain terminal-status
    candidate set, over the live+archive `dag_index` corpus (AC5, C4b).

    For each candidate, in order:
      1. (a) 30-minute claimed_at recency floor — too-recent candidates are
         silently excluded (no WARN marker; this is a normal, expected
         disposition, not an adjudication surface).
      2. Heir classification (`archive_handoffs._classify_heir_children`,
         REUSE) over the combined live+archive dag_index:
           - "error" (or an incomplete dag_index — a partial scan cannot
             safely partition heir/fork-only/childless) -> retained,
             fail-closed, surfaced in `retained`.
           - "heir" -> (e) heir (b)-suppression: deployment_state is stamped
             "shipped" unconditionally on the candidate's own frontmatter
             (`_stamp_deployment_state_shipped`) and the candidate proceeds
             to archival — it NEVER reaches the (b) in_flight skip-and-
             surface disposition below.
           - "fork-only" / "childless" -> (b): a non-heir candidate whose
             frontmatter literally reads `deployment_state: in_flight` is
             neither archived nor flipped — it is pulled out, surfaced in
             `awaiting_adjudication` with the `_AWAITING_ADJUDICATION_REASON`
             token, and left untouched in state/handoffs/ (the durable
             adjudication queue). Every other non-heir candidate proceeds to
             archival unchanged.

    Returns (to_archive, awaiting_adjudication, retained):
      to_archive             — [{"path": Path, "heir": bool, "detail": str,
                                "sid": str}, ...] — candidates to hand to
                                `relocate_candidates`, each already carrying
                                any (e) frontmatter stamp this function
                                applied. `sid` is the claimed_by/consumed_by
                                value read BEFORE any stamp, for the (d)
                                WARN marker.
      awaiting_adjudication  — [{"path": Path, "sid": str}, ...] — (b)
                                skip-and-surface candidates.
      retained               — [{"path": Path, "reason": str}, ...] — heir
                                classification failures / dag_index
                                incompleteness (fail-closed retains).

    No git QUERY spawn anywhere in this function (AC1) — see module
    docstring negative-spec for why `archive_handoffs._is_terminal` itself
    is deliberately NOT called here.
    """
    dag_scan_errors: List[str] = []
    dag_index = _collect_all_handoff_paths(worktree, scan_errors=dag_scan_errors)
    dag_incomplete = bool(dag_scan_errors)

    to_archive: List[dict] = []
    awaiting_adjudication: List[dict] = []
    retained: List[dict] = []

    for handoff_path in candidates:
        try:
            text = handoff_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            retained.append({"path": handoff_path, "reason": f"unreadable: {exc}"})
            continue
        split = split_frontmatter(text)
        fm_text = split.fm_text if split is not None else ""

        if _is_claimed_at_too_recent(fm_text):
            continue

        sid = (
            _strip_quotes(read_fm_field(fm_text, "claimed_by"))
            or _strip_quotes(read_fm_field(fm_text, "consumed_by"))
            or "unknown"
        )

        if dag_incomplete:
            retained.append({
                "path": handoff_path,
                "reason": "dag_index incomplete (" + "; ".join(dag_scan_errors) + ") — "
                "retained (fail-closed)",
            })
            continue

        heir_kind, heir_detail = await _classify_heir_children(handoff_path, dag_index)
        if heir_kind == "error":
            retained.append({"path": handoff_path, "reason": heir_detail})
            continue

        if heir_kind == "heir":
            _stamp_deployment_state_shipped(handoff_path)
            to_archive.append(
                {"path": handoff_path, "heir": True, "detail": heir_detail, "sid": sid}
            )
            continue

        deployment_state = _strip_quotes(read_fm_field(fm_text, "deployment_state")).lower()
        if deployment_state == "in_flight":
            awaiting_adjudication.append({"path": handoff_path, "sid": sid})
            continue

        to_archive.append({"path": handoff_path, "heir": False, "detail": "", "sid": sid})

    return to_archive, awaiting_adjudication, retained


def _resolve_repo_root_mismatch(param_root: Optional[str], common_dir: Path) -> Optional[str]:
    """D3 consistency check: if params.repo_root is provided, validate it
    resolves to the same git common dir as the handler's engine-supplied
    common_dir arg. Returns None on success (no mismatch, or param_root not
    provided); returns a human-readable reason string on genuine mismatch.

    Reimplemented locally against coordinator_core.lifecycle.git_common_dir
    (walk-only, zero-spawn per that function's own docstring) rather than
    importing coordinator_core.ops.fleet._common.check_repo_root, for the
    same import-budget reason `_archive_dest` above gives.

    NEGATIVE-SPEC: params.repo_root is NEVER the worktree-root resolution
    source — the socket selects the repo; the handler derives the worktree
    via main_worktree_root(common_dir). This is an optional caller-side
    consistency assertion only (contract §3.3).
    """
    if param_root is None:
        return None
    try:
        candidate_common = git_common_dir(Path(param_root).resolve())
    except (RuntimeError, OSError) as exc:
        return (
            f"repo_root-mismatch: could not resolve git_common_dir for "
            f"params.repo_root={param_root!r}: {exc}"
        )
    if candidate_common.resolve() != common_dir.resolve():
        return (
            f"repo_root-mismatch: params.repo_root resolves to common_dir "
            f"{candidate_common} but socket-authoritative common_dir is "
            f"{common_dir} — the socket selects the repo; params.repo_root "
            f"cannot override it"
        )
    return None


def _build_error_result(reason: str) -> dict:
    """Build a setup-error result (exit_code:1) for session.boot_sweep.

    Used for pre-handler failures (missing repo_root, D3 mismatch). Custom
    boot_backstop envelope — NOT the fleet mode/dry_run shape, and
    deliberately not the retired composite's much wider envelope (C2's
    consumer scan is what decides which of THAT shape's fields must survive
    the rebuild; this chunk's own envelope is scoped to its own mechanism).
    """
    _LOG.error("session.boot_sweep (backstop): setup error: %s", reason)
    return {
        "exit_code": 1,
        "warnings": [],
        "handoffs": {"archived": [], "failed": [], "skipped": []},
        "commit": {"committed": False, "sha": None, "reason": reason},
    }


@register_op("session.boot_sweep")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.boot_sweep — rebuilt boot-time archival backstop (C4a + C4b).

    Enumerates state/handoffs/*.md, selects the terminal subset by
    frontmatter alone (`_is_archival_candidate`), layers the DR-084
    (a)/(b)/(e) consumed-handoff semantics on top
    (`_apply_dr084_disposition`, AC5), relocates every surviving candidate to
    archive/handoffs/ via session_scope.relocate_touched_path, appends any
    (d) WARN markers, then commits the whole archival batch as one scoped
    `git add -A` plus one `git commit`. No git QUERY spawn anywhere in the
    selection/move/DR-084-classification path (AC1); the two git calls in
    `_commit_relocations` are the archival record's own MUTATING cost,
    budgeted separately (AC4/AC4b) — see this module's own docstring.

    params:
      repo_root (str, optional): D3 consistency check only — NOT the path
          source (see `_resolve_repo_root_mismatch`).
      session_id (str, optional): the firing session's id, threaded to
          `relocate_touched_path` so a relocated path's touch-claim (if any)
          is restated on its new location rather than stranded. Falls back
          to `coordinator_core.contract.apply_base.resolve_explicit_session_id`'s
          own environment read, then to `_FALLBACK_SESSION_ID`.

    repo_root handler arg: git common dir (_OP_KEY_SCOPE="common_dir").
    Worktree root derived via main_worktree_root(common_dir) — never from
    params.repo_root (D3 check only, contract §3.3 doctrine).

    Result envelope:
      exit_code 0 — enumeration/selection/move/commit all completed with no
                    per-item failure (including the zero-candidate case).
      exit_code 1 — setup error (missing repo_root, D3 mismatch).
      exit_code 2 — one or more per-item move failures, OR at least one
                    candidate moved but the commit did not land (the move
                    already happened; only its record is unstaged).
      handoffs.archived    — [{"from": repo-rel, "to": repo-rel}, ...]
      handoffs.failed      — [{"path": repo-rel, "reason": str}, ...]
      handoffs.skipped     — [{"path": repo-rel, "reason": str}, ...] — (b)
                    skip-and-surface candidates (`reason` is the literal
                    `_AWAITING_ADJUDICATION_REASON` token) and any
                    fail-closed heir-classification retains. Never a move
                    target; never affects exit_code.
      commit               — {"committed": bool, "sha": None, "reason": str|None}
      warnings             — top-level list of structured scan-failure
                    notices that degraded safe rather than failing the
                    sweep (an unreadable/unparseable handoff, or a
                    fail-closed DR-084 retain). Never affects exit_code.
    """
    if repo_root is None:
        _LOG.error("session.boot_sweep (backstop): repo_root handler arg is None")
        return _build_error_result("repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root

    mismatch = _resolve_repo_root_mismatch(params.get("repo_root"), common_dir)
    if mismatch:
        return _build_error_result(mismatch)

    worktree = main_worktree_root(common_dir)

    session_id = (
        _resolve_explicit_session_id(params.get("session_id")) or _FALLBACK_SESSION_ID
    )

    handoffs = enumerate_handoffs(worktree)
    candidates, unreadable = select_candidates(handoffs)
    to_archive, awaiting_adjudication, retained = await _apply_dr084_disposition(
        worktree, candidates
    )

    moved, failed = relocate_candidates(
        worktree, [item["path"] for item in to_archive], session_id
    )
    commit_result = _commit_relocations(worktree, moved)

    # (d) WARN markers — for every SUCCESSFULLY moved archival candidate
    # (never for a failed move), plus every (b) skip-and-surface candidate
    # (independent of archival — these never reach relocate_candidates at
    # all).
    moved_srcs = {item["from"] for item in moved}
    for item in to_archive:
        src_rel = item["path"].relative_to(worktree).as_posix()
        if src_rel not in moved_srcs:
            continue
        if item["heir"]:
            note = f"succeeded by {item['detail']}, deployment_state stamped shipped"
        else:
            note = "no deployment_state change"
        _append_warn_marker(
            worktree, item["path"].name, item["sid"], note, verb="archived"
        )
    for item in awaiting_adjudication:
        _append_warn_marker(
            worktree,
            item["path"].name,
            item["sid"],
            disposition_note="awaiting human adjudication or DR-084 continued semantics",
            verb="skipped",
        )

    warnings = [
        {"scope": "handoffs", "reason": f"unreadable ({item['path']}): {item['reason']}"}
        for item in unreadable
    ] + [
        {"scope": "handoffs", "reason": f"{item['path']}: {item['reason']}"}
        for item in retained
    ]

    skipped = [
        {
            "path": item["path"].relative_to(worktree).as_posix(),
            "reason": _AWAITING_ADJUDICATION_REASON,
        }
        for item in awaiting_adjudication
    ] + [
        {
            "path": item["path"].relative_to(worktree).as_posix(),
            "reason": item["reason"],
        }
        for item in retained
    ]

    any_failed = bool(failed) or (bool(moved) and not commit_result["committed"])
    return {
        "exit_code": 2 if any_failed else 0,
        "warnings": warnings,
        "handoffs": {"archived": moved, "failed": failed, "skipped": skipped},
        "commit": commit_result,
    }
