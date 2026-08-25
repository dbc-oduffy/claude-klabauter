"""
coordinator_core.ops.fleet.archive_sizings — fleet.archive_terminal_sizings op.

Purpose: git-mv terminal sizing-objects (status ∈ SIZING_TERMINAL_STATUS) from
state/sizings/*.yaml into archive/sizings/YYYY-MM/ under the confirm→act
(dry_run:true / dry_run:false) wire contract. Registers as
"fleet.archive_terminal_sizings" via @register_op.

Modeled on coordinator_core.ops.fleet.archive_plans for the MOVE shape
(same _handle_preview / _handle_act split, same _derive_yyyy_mm-from-filename
approach for the archive destination) — see that module's docstring for the
mechanics this one intentionally mirrors. Two things are deliberately NOT
mirrored:

1. The collision predicate. archive_plans carries only the source-gone
   idempotent-replay form; this family additionally distinguishes a
   byte-identical destination (converge via force-move) from a genuinely
   differing one (skip, never clobber) using the shared
   coordinator_core.ops.fleet._common._is_identical_duplicate /
   _REASON_DEST_CONFLICT predicate — see archive_handoffs.py for the
   worked pattern this module's dest-collision block is copied from.
2. Terminality itself is status-only, never inferred. See "Never-infer
   boundary" below.

Never-infer boundary (DR-293; AC3):
This module NEVER writes or infers a `status:` value. It reads the
frontmatter `status:` field of each sizing-object and, when it names a
terminal value, moves the file — nothing more. A non-terminal, malformed, or
unparseable record is left untouched on disk — never flipped, never
silently dropped from consideration in the sense of being reinterpreted as
terminal. At T3 act-time, a record that drifts non-terminal between preview
and act is surfaced in the result envelope with a named skip reason
(`terminality-drift:...`). At T1 preview, a non-terminal/malformed record is
simply not enumerated as a candidate — `build_dry_run_result` carries no
skip list at T1 (`skipped: []` is hardcoded empty there); the record's
never-flipped guarantee holds, but "surfaced with a reason" is a T3-only
property, not a T1 one. `cascade_backstop_sweep` and this family's own
governing boundary are explicit on this point: a sweep that decides for
itself that work is "finished" is exactly the failure DR-293 forbids. Every
code path in this module is read-then-move or refuse-to-move; none is
read-then-write.

Forward-pointer refusal gate (AC6):
A sizing record MAY carry a `plan:` FK naming the plan it was consumed into.
When present, this module reads that plan's OWN `status:` and requires it to
be plan-terminal (coordinator_core.lifecycle_constants.PLAN_TERMINAL_STATUS)
before the sizing may move — even though the sizing's own status is already
terminal. This is a REFUSAL, not an inference: the family declines to act
and concludes nothing about the plan's true state. A null/absent `plan:` FK
(the common case — every dispatch-routed sizing) carries no such constraint.
Absent-target and unreadable-target both refuse-in-place with a named
reason, never treated as "no constraint" — a dangling FK is exactly the
ambiguous case this gate exists to fail closed on.

Terminality predicate: frontmatter status ∈ SIZING_TERMINAL_STATUS
(coordinator_core.lifecycle_constants — the family's ONLY terminality
source; no literal status list appears in this module).

Archive destination: archive/sizings/YYYY-MM/ (YYYY-MM from the sizing
record's OWN FILENAME prefix, e.g. 2026-08-13-foo.yaml → 2026-08 — never
from today's date; mirrors archive_plans._derive_yyyy_mm).

Spec backlinks:
  - Plan (C1): docs/plans/2026-08-13-terminal-sizings-boot-sweep-family.md
  - DR-293:    the ruling naming this family's shape and never-infer boundary
  - Sibling collision fix: docs/plans/2026-08-13-fleet-archive-dest-collision-vs-idempotent-replay.md
    (moved _is_identical_duplicate / _REASON_DEST_CONFLICT into _common.py;
    this family consumes that shared export rather than re-implementing it)

Negative-spec:
  - Does NOT write, flip, upgrade, or infer a `status:` on any record —
    sizing or plan. Read-then-move or refuse-to-move only.
  - Does NOT copy archive_plans.py's dest-collision predicate (bare
    `dst.exists()` → "already-archived") — that shape was removed from
    three other fleet families as a defect (widened "already-archived"
    past its AC12-pinned source-gone meaning). See _common._is_identical_duplicate.
  - Does NOT clobber a destination whose bytes differ from the source —
    a differing dst is real archived history.
  - Does NOT use git add -A or git add . — scoped exact-pathspec only via
    archive_and_commit (DR-211 D3 Invariant 4, inherited unchanged).
  - Does NOT reconcile pre-existing archive residue — this module adds the
    mechanism only; sweeping already-stranded records is a separate corpus
    remediation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import PLAN_TERMINAL_STATUS, SIZING_TERMINAL_STATUS
from coordinator_core.ops.fleet._common import (
    Move,
    _REASON_DEST_CONFLICT,
    _is_identical_duplicate,
    archive_and_commit,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    parse_frontmatter_status,
    rel_id,
    validate_params,
)

_TERMINAL_STATUSES: frozenset = SIZING_TERMINAL_STATUS

# Regex to extract YYYY-MM from a filename prefix (e.g. 2026-08-13-foo.yaml → "2026-08").
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")

# Distinct, named refusal reason for AC6's forward-pointer gate — never
# conflated with any dest-collision or terminality-drift reason string.
_REASON_FORWARD_PLAN_NOT_TERMINAL = "forward-plan-not-terminal"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _derive_yyyy_mm(fname: str) -> Optional[str]:
    """Derive YYYY-MM from a sizing-object filename prefix.

    E.g. "2026-08-13-foo.yaml" → "2026-08".
    Returns None when the filename carries no YYYY-MM-DD prefix (ungated skip).
    """
    m = _DATE_PREFIX_RE.match(fname)
    return m.group(1) if m else None


def _extract_title(path: Path) -> Optional[str]:
    """Return the 'title' field from YAML frontmatter, or None if absent/unreadable."""
    meta = _read_meta(str(path))
    return meta.get("title") if meta else None


def _read_plan_fk(path: Path) -> Optional[str]:
    """Return the sizing record's `plan:` FK value, or None if absent/null/unreadable.

    Read-only — this function never writes or infers anything about the
    referenced plan; it only extracts the pointer itself.
    """
    meta = _read_meta(str(path))
    if not meta:
        return None
    plan_fk = meta.get("plan")
    return plan_fk if plan_fk else None


def _forward_plan_refusal_reason(
    worktree_root: Path, plan_fk: str
) -> Optional[str]:
    """Evaluate AC6's forward-pointer refusal gate for a non-null `plan:` FK.

    Returns None when the sizing may proceed (the FK's target plan is itself
    plan-terminal). Returns a non-None reason string when the sizing must be
    refused-in-place — covering both "the plan is not terminal" and "the FK
    cannot be resolved/read at all" (a dangling or unreadable FK is treated
    as a refusal, never as 'no constraint').

    This function only READS the target plan's status — it never writes to
    it and never concludes anything about it beyond "terminal or not, right
    now" for the purpose of this refusal.
    """
    plan_path = worktree_root / plan_fk
    if not plan_path.is_file():
        # Try resolving relative to docs/plans/ when the FK is a bare filename.
        candidate = worktree_root / "docs" / "plans" / plan_fk
        if candidate.is_file():
            plan_path = candidate
        else:
            return (
                f"{_REASON_FORWARD_PLAN_NOT_TERMINAL}: plan FK {plan_fk!r} "
                f"could not be resolved to a file"
            )

    plan_status = parse_frontmatter_status(plan_path)
    if plan_status not in PLAN_TERMINAL_STATUS:
        return (
            f"{_REASON_FORWARD_PLAN_NOT_TERMINAL}: plan {plan_fk!r} status is "
            f"{plan_status!r}, not terminal"
        )
    return None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@register_op("fleet.archive_terminal_sizings")
async def _archive_terminal_sizings(
    params: dict, repo_root=None
) -> dict:
    """JSON-RPC 'fleet.archive_terminal_sizings' handler.

    dry_run:true  → T1 preview: enumerate terminal sizings (status-only,
                    AC6-refused ones excluded); return candidates[] (mutates
                    nothing).
    dry_run:false → T3 act: re-verify each candidate_id, git-mv into
                    archive/sizings/YYYY-MM/, commit.

    repo_root arg is the git common dir (from _OP_KEY_SCOPE = "common_dir").
    Worktree root is derived engine-side via main_worktree_root(common_dir).
    params.repo_root is the optional D3 consistency check ONLY — NOT the
    worktree source (mirrors archive_plans.py's Key Decision 5).
    """
    validated = validate_params(params)
    if isinstance(validated, dict):
        return validated  # setup-error envelope already built
    mode, dry_run, candidate_ids = validated

    if repo_root is None:
        return build_setup_error_result(
            mode, dry_run,
            "fleet.archive_terminal_sizings: repo_root arg is None — "
            "common_dir not supplied by engine (check _OP_KEY_SCOPE = 'common_dir')",
        )
    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)

    d3_error = check_repo_root(params.get("repo_root"), common_dir)
    if d3_error:
        return build_setup_error_result(mode, dry_run, d3_error)

    sizings_dir = worktree_root / "state" / "sizings"
    if not sizings_dir.is_dir():
        # No sizings directory — return empty result (consumer-project guard).
        if dry_run:
            return build_dry_run_result(mode, [])
        return build_act_result(mode, [], [], [])

    if dry_run:
        return await _handle_preview(mode, worktree_root, sizings_dir, common_dir)
    else:
        return await _handle_act(mode, worktree_root, sizings_dir, candidate_ids, common_dir)


async def _handle_preview(
    mode: str, worktree_root: Path, sizings_dir: Path, common_dir: Path
) -> dict:
    """T1 preview: enumerate terminal sizings, apply the cannot-derive-date
    and forward-pointer-refusal guards, return candidates.
    """
    candidates: List[dict] = []

    for path in sorted(sizings_dir.glob("*.yaml")):
        status = parse_frontmatter_status(path)
        if status not in _TERMINAL_STATUSES:
            continue  # not terminal — never flipped, never surfaced as a candidate

        # cannot-derive-date guard (T1 filter, mirrors archive_plans): a
        # terminal sizing with no YYYY-MM-DD prefix has no archive
        # destination — never present it as an archivable candidate.
        if _derive_yyyy_mm(path.name) is None:
            continue

        # AC6 forward-pointer refusal gate (T1 filter): a live plan FK holds
        # the sizing in place even though its own status is terminal.
        plan_fk = _read_plan_fk(path)
        if plan_fk is not None:
            refusal = _forward_plan_refusal_reason(worktree_root, plan_fk)
            if refusal is not None:
                continue

        rel_path = rel_id(path, worktree_root)
        title = _extract_title(path) or path.stem
        candidates.append({
            "id": rel_path,
            "title": title,
            "status": status,
            "family": "sizing",
            "terminal_since": None,
            "note": None,
        })

    return build_dry_run_result(mode, candidates)


async def _handle_act(
    mode: str,
    worktree_root: Path,
    sizings_dir: Path,
    candidate_ids: List[str],
    common_dir: Path,
) -> dict:
    """T3 act: per-candidate re-verify + AC6 forward-pointer-refusal guard +
    dest-collision handling + git-mv + commit.

    For each candidate_id:
    1. Source gone → skipped reason:"already-archived" (idempotent replay,
       AC12-pinned — see _common._REASON_DEST_CONFLICT's docstring).
    2. Act-time terminality re-verify: drifted non-terminal → skipped
       reason:"terminality-drift:...".
    3. AC6 forward-pointer refusal: live plan FK → skipped with the
       distinct _REASON_FORWARD_PLAN_NOT_TERMINAL reason.
    4. No YYYY-MM prefix → skipped reason:"cannot-derive-date".
    5. Dest-collision: differing dst → skipped reason:_REASON_DEST_CONFLICT
       (never "already-archived", never clobbered); byte-identical dst →
       force-move (converge).
    6. Otherwise: build Move and add to batch.

    After all checks, calls archive_and_commit once for the full batch (ONE
    commit).
    """
    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []

    candidate_moves: Dict[str, Move] = {}

    sizings_dir_safe = sizings_dir.resolve()

    for cid in candidate_ids:
        sizing_path = worktree_root / cid

        resolved = sizing_path.resolve()
        if not resolved.is_relative_to(sizings_dir_safe):
            failed.append({
                "id": cid,
                "reason": "path-traversal: candidate_id escapes state/sizings/",
            })
            continue
        sizing_path = resolved

        # Already-archived: source gone → idempotent skip (AC12-pinned string;
        # do NOT widen this reason string — see module docstring).
        if not sizing_path.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        # Act-time terminality re-verify.
        status = parse_frontmatter_status(sizing_path)
        if status not in _TERMINAL_STATUSES:
            skipped.append({
                "id": cid,
                "reason": f"terminality-drift: status is now {status!r}",
            })
            continue

        # AC6 forward-pointer refusal gate at T3 (applied at both T1 and T3).
        plan_fk = _read_plan_fk(sizing_path)
        if plan_fk is not None:
            refusal = _forward_plan_refusal_reason(worktree_root, plan_fk)
            if refusal is not None:
                skipped.append({"id": cid, "reason": refusal})
                continue

        yyyy_mm = _derive_yyyy_mm(sizing_path.name)
        if yyyy_mm is None:
            skipped.append({
                "id": cid,
                "reason": f"cannot-derive-date: filename {sizing_path.name!r} "
                          f"has no YYYY-MM-DD prefix",
            })
            continue

        dst = worktree_root / "archive" / "sizings" / yyyy_mm / sizing_path.name

        force = False
        if dst.exists():
            if not _is_identical_duplicate(sizing_path, dst):
                # A DIFFERENT file already occupies the archive destination —
                # never "already-archived" (that string is AC12-pinned to the
                # source-gone case), never clobbered.
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                continue
            # Byte-identical duplicate: converge by archiving over it.
            force = True

        candidate_moves[cid] = Move(src=sizing_path, dst=dst, candidate_id=cid, force=force)

    if not candidate_moves:
        return build_act_result(mode, acted, skipped, failed)

    flat_moves: List[Move] = list(candidate_moves.values())

    n_sizings = len(candidate_moves)
    subject = f"fleet: archive {n_sizings} terminal sizing-object(s) [fleet.archive_terminal_sizings]"
    raw_acted, raw_failed = await archive_and_commit(worktree_root, flat_moves, subject)

    raw_acted_ids: Set[str] = {a["id"] for a in raw_acted}
    raw_failed_by_id: Dict[str, str] = {f["id"]: f["reason"] for f in raw_failed}

    for cid in candidate_moves:
        if cid in raw_failed_by_id:
            failed.append({"id": cid, "reason": raw_failed_by_id[cid]})
        elif cid in raw_acted_ids:
            acted.append({"id": cid, "archived": True})
        else:
            failed.append({"id": cid, "reason": "unexpected: absent from acted and failed"})

    return build_act_result(mode, acted, skipped, failed)
