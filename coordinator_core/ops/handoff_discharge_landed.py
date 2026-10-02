"""
coordinator_core.ops.handoff_discharge_landed — handoff.discharge_landed op.

Purpose: stamp and archive every live baton whose governing plan has landed
(`status: implemented`/`landed`), in ONE batched commit, so a mise-en-place
run ends with its landed plans' batons `shipped` and moved to
`archive/handoffs/YYYY-MM/` — closing the gap `handoff.archive_transition`
(killed, DR-344, -32006) left behind: nothing today stamps a baton `shipped`
on plan-landing alone.

Spec backlink: docs/plans/2026-09-28-batch-discharge-landed-batons.md § C1
(Design steps 1-6), spike verdict docs/research/2026-09-28-discharge-landed-
spike.md (landing-sha source, budget headroom).

Design (mirrors the plan's own numbering):
  1. Scope — `plan_ids` given -> exactly those plans (repo-relative paths).
     Absent -> every plan doc under docs/plans/ at status implemented/landed.
  2. Resolve — a baton joins a target plan via a STRONG link
     (`governing_plan`, `origin_plan_id`, `plan_ids`) matched against each
     target plan's own repo-relative path or its `plan_id:` frontmatter
     value. A `deliverable_id`-only match is used ONLY when it resolves to
     exactly one target plan (the same weak-basis discipline
     `roadmap/plan_gate.py :: _best_plan` enforces since pvcs-01..04) — a
     multi-hit on either basis is refused with every candidate plan named,
     never silently picked.
  3. Stamp — `shipped_in` is set to the plan's OWN `status: implemented`/
     `landed` flip commit (`git log -1 -S'status: <status>' --format=%H
     --follow -- <plan_path>`, ONE spawn per DISTINCT target plan, cached —
     never per baton; see the spike's landing-sha finding for why
     `execution_authorized_sha` and a spine row's `disposition_ref` are both
     wrong sources). `deployment_state` -> "shipped".
  4. Archive — every stamped baton is queued as a `restage_src=True` Move
     (this handler authored the on-disk content on purpose, immediately
     before queuing) and landed via `ops.fleet._common.archive_and_commit`
     in ONE commit, reused unchanged.
  5. Concurrency — one O_EXCL repo-level lock (own lock file, distinct from
     `fleet.archive_completed_handoffs`'s sweep lock — a different corpus
     question). Contention refuses the WHOLE batch, never half-applies.
  6. Report — `{discharged: [...], already_done: [...], refused: [{baton,
     plan, reason}]}`; nothing dropped.

Negative-spec:
  - Does NOT touch `handoff.archive_transition` — killed, stays killed
    (DR-344, "kill means kill forever").
  - Does NOT widen the handoff `status` enum (DR-084) — a baton carrying a
    `status` outside {"open", "claimed", "consumed"} is a malformed record,
    refused with reason, never coerced.
  - Does NOT let a `deliverable_id` multi-hit pick a winner (the pvcs-01..04
    defect) — refused with every candidate plan named.
  - Does NOT spawn a git process per baton anywhere on this path — the
    `git log -S` landing-sha lookup is cached per DISTINCT target plan, and
    the archive+commit step is `archive_and_commit`'s existing batched
    single-commit mechanism (at most one `git hash-object -w --stdin-paths`
    spawn for the whole `restage_src=True` batch).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.roadmap.blitz_land import LandingRefused, _set_field
from coordinator_core.session.claimed_write import replace_text
from coordinator_core.ops.fleet._common import (
    Move,
    archive_and_commit,
    check_repo_root,
    collect_live_handoff_paths,
    handoff_archive_dest,
    rel_id,
)

_LOG = logging.getLogger(__name__)

# DR-084's frozen handoff `status` vocabulary — a value outside this set is a
# malformed record (e.g. `status: complete`), never coerced or widened.
_VALID_HANDOFF_STATUSES = frozenset({"open", "claimed", "consumed"})

_STRONG_PLAN_LINK_FIELDS: Tuple[str, ...] = ("governing_plan", "origin_plan_id", "plan_ids")

_LANDED_STATUSES = frozenset({"implemented", "landed"})

_LOCK_STALE_S = 120.0



# ---------------------------------------------------------------------------
# Single-flight lock — own lock file, deliberately distinct from
# archive_terminal_handoffs's sweep lock (a different corpus question: that
# lock guards the terminal-handoff SWEEP, this one guards the landed-plan
# DISCHARGE join; sharing one lock file would serialize two unrelated ops for
# no correctness reason).
# ---------------------------------------------------------------------------


def _lock_path(common_dir: Path) -> Path:
    return common_dir / "coordinator-sessions" / "handoff-discharge-landed.lock"


def _acquire_lock(common_dir: Path) -> Optional[Path]:
    """Best-effort O_EXCL acquire. None means "another instance holds it" —
    the caller's contract is to refuse the WHOLE batch, never half-apply.

    A stale lock (writer crashed between acquire/release) self-heals after
    `_LOCK_STALE_S`, mirroring archive_terminal_handoffs._acquire_sweep_lock.
    """
    path = _lock_path(common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return path
    except FileExistsError:
        try:
            age_s = time.time() - path.stat().st_mtime
        except OSError:
            return None
        if age_s <= _LOCK_STALE_S:
            return None
        try:
            path.unlink()
        except OSError:
            return None
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return path
        except OSError:
            return None
    except OSError as exc:
        _LOG.warning(
            "handoff_discharge_landed: lock acquire failed for %s — %s; "
            "degrading to 'contended' (fail-closed-to-skip)", path, exc,
        )
        return None


def _release_lock(lock_path: Optional[Path]) -> None:
    if lock_path is None:
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Target-plan scope (Design step 1)
# ---------------------------------------------------------------------------


def _plan_status(meta: dict) -> str:
    return str(meta.get("status") or "").strip().lower()


def _scan_landed_plans(worktree_root: Path) -> Dict[str, dict]:
    """Every plan doc under docs/plans/ whose `status` is implemented/landed,
    keyed by repo-relative posix path, valued by its already-read frontmatter
    — the "plan_ids absent" scope.
    """
    plans_dir = worktree_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return {}
    out: Dict[str, dict] = {}
    try:
        entries = sorted(plans_dir.rglob("*.md"))
    except OSError as exc:
        _LOG.warning("handoff_discharge_landed: cannot scan %s — %s", plans_dir, exc)
        return {}
    for p in entries:
        meta = _read_meta(str(p)) or {}
        if _plan_status(meta) in _LANDED_STATUSES:
            out[rel_id(p, worktree_root)] = meta
    return out


# ---------------------------------------------------------------------------
# Baton -> target-plan resolution (Design step 2)
# ---------------------------------------------------------------------------


def _strong_field_values(meta: dict) -> Set[str]:
    values: Set[str] = set()
    for field in _STRONG_PLAN_LINK_FIELDS:
        raw = meta.get(field)
        if raw is None:
            continue
        if isinstance(raw, (list, tuple)):
            for item in raw:
                if item:
                    values.add(str(item).strip())
        else:
            values.add(str(raw).strip())
    values.discard("")
    return values


def _resolve_baton_plan(
    meta: dict,
    plan_identifiers: Dict[str, Set[str]],
    plan_deliverable_ids: Dict[str, str],
) -> Tuple[Optional[str], List[str]]:
    """Returns (matched_plan_rel_or_None, candidate_plans).

    `candidate_plans` is populated only on a genuine multi-hit — the set this
    resolution DECLINED to reduce, named for the refusal report (mirrors
    `roadmap.plan_gate._plan_link_ambiguity`).
    """
    strong_values = _strong_field_values(meta)
    strong_hits = sorted(
        {plan_rel for plan_rel, ids in plan_identifiers.items() if strong_values & ids}
    )
    if len(strong_hits) == 1:
        return strong_hits[0], []
    if len(strong_hits) > 1:
        return None, strong_hits

    baton_deliverable_id = meta.get("deliverable_id")
    if not baton_deliverable_id:
        return None, []
    baton_deliverable_id = str(baton_deliverable_id).strip()
    weak_hits = sorted(
        plan_rel
        for plan_rel, did in plan_deliverable_ids.items()
        if did == baton_deliverable_id
    )
    if len(weak_hits) == 1:
        return weak_hits[0], []
    if len(weak_hits) > 1:
        return None, weak_hits
    return None, []


# ---------------------------------------------------------------------------
# Landing-sha resolution (Design step 3) — cached per DISTINCT plan
# ---------------------------------------------------------------------------


def _plan_landing_shas(worktree_root: Path, plan_rels: List[str]) -> Dict[str, Optional[str]]:
    """Each plan's own flip-to-landed commit sha, for every plan in ONE `git log` spawn.

    Newest commit whose diff touches a `status: implemented|landed` line in that plan
    path. The plan's current status is landed, so the newest such touch is the flip INTO
    it. A plan with no matching commit maps to None. Never spawn per plan: at repo scope
    the plan count is the batch size, and the op's budget is whole-batch.
    """
    result: Dict[str, Optional[str]] = {rel: None for rel in plan_rels}
    if not plan_rels:
        return result
    proc = run_git(
        [
            "log", "--format=%x00%H", "--name-only",
            "-G^status: *(implemented|landed) *$",
            "--", *plan_rels,
        ],
        cwd=str(worktree_root),
    )
    if proc.returncode != 0:
        if proc.timed_out or proc.returncode == 127:
            _LOG.warning("handoff_discharge_landed: git log -G failed — rc=%s", proc.returncode)
        return result
    for record in proc.stdout.split("\x00"):
        lines = [ln.strip() for ln in record.splitlines() if ln.strip()]
        if not lines:
            continue
        sha, paths = lines[0], lines[1:]
        for rel in paths:
            if rel in result and result[rel] is None:
                result[rel] = sha
    return result


# ---------------------------------------------------------------------------
# Already-discharged predicate
# ---------------------------------------------------------------------------


def _already_discharged(meta: dict) -> bool:
    deployment_state = str(meta.get("deployment_state") or "").strip().lower()
    if deployment_state not in HANDOFF_TERMINAL_DEPLOYMENT:
        return False
    if deployment_state != "shipped":
        return True
    shipped_in = meta.get("shipped_in")
    return bool(str(shipped_in).strip()) if shipped_in else False


# ---------------------------------------------------------------------------
# Stamp — rewrite shipped_in/deployment_state in place (Design step 3)
# ---------------------------------------------------------------------------


def _stamp_text(text: str, landing_sha: str) -> str:
    text = _set_field(text, "deployment_state", "shipped")
    text = _set_field(text, "shipped_in", landing_sha)
    return text


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _refuse(reason: str) -> dict:
    return {
        "exit_code": 1,
        "discharged": [], "already_done": [], "refused": [],
        "reason": reason,
    }


@register_op("handoff.discharge_landed")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """handoff.discharge_landed — scoped resolve, batch stamp+archive, lock,
    refusal report. See module docstring for the full design.

    SYNCHRONOUS handler (mirrors fleet.archive_completed_handoffs' own
    rationale — the sync branch of `ipc._dispatch` already offloads to a
    thread and applies the per-op timeout; declaring this `async def` would
    take on that obligation a second time for no benefit). The single git
    commit step drives `archive_and_commit` (a coroutine, unchanged) via one
    `asyncio.run(...)` boundary, only when there is something to commit.

    `repo_root`: the engine-injected common_dir when this op's key is
    scoped to it; falls back to `params.get("repo_root")` (resolved via
    `git_common_dir`) so this handler works correctly when called directly
    (tests, a CLI wrapper) ahead of any op-scope-table wiring.
    """
    params = params or {}

    common_dir = repo_root if isinstance(repo_root, Path) else None
    if common_dir is None:
        raw_root = params.get("repo_root")
        if not raw_root:
            return _refuse("repo_root is required (engine-injected or params.repo_root)")
        try:
            common_dir = git_common_dir(Path(raw_root).resolve())
        except (RuntimeError, OSError) as exc:
            return _refuse(f"cannot resolve git_common_dir for repo_root={raw_root!r}: {exc}")

    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return _refuse(mismatch)

    worktree_root = main_worktree_root(common_dir)

    plan_ids_param = params.get("plan_ids")
    if plan_ids_param is not None and not isinstance(plan_ids_param, list):
        return _refuse(
            f"plan_ids must be a list or null, got {type(plan_ids_param).__name__!r}"
        )

    lock_path = _acquire_lock(common_dir)
    if lock_path is None:
        return {
            "exit_code": 0,
            "discharged": [], "already_done": [], "refused": [],
            "contended": True,
        }

    try:
        return _discharge(worktree_root, plan_ids_param)
    finally:
        _release_lock(lock_path)


def _discharge(worktree_root: Path, plan_ids_param: Optional[List[str]]) -> dict:
    if plan_ids_param:
        target_meta = {
            rel: _read_meta(str(worktree_root / rel)) or {}
            for rel in sorted({str(p).strip().replace("\\", "/") for p in plan_ids_param if p})
        }
    else:
        target_meta = _scan_landed_plans(worktree_root)

    discharged: List[dict] = []
    already_done: List[dict] = []
    refused: List[dict] = []

    if not target_meta:
        return {"exit_code": 0, "discharged": discharged, "already_done": already_done, "refused": refused}

    # A plan's matchable identifiers: its repo-relative path and its own
    # `plan_id:` value — either is legal in a baton's strong link fields.
    plan_identifiers: Dict[str, Set[str]] = {}
    plan_deliverable_ids: Dict[str, str] = {}
    plan_status: Dict[str, str] = {}
    for p, meta in target_meta.items():
        ids: Set[str] = {p}
        if meta.get("plan_id"):
            ids.add(str(meta["plan_id"]).strip())
        plan_identifiers[p] = ids
        if meta.get("deliverable_id"):
            plan_deliverable_ids[p] = str(meta["deliverable_id"]).strip()
        status = _plan_status(meta)
        plan_status[p] = status if status in _LANDED_STATUSES else "implemented"

    landing_shas: Optional[Dict[str, Optional[str]]] = None

    def _landing_sha_for(plan_rel: str) -> Optional[str]:
        nonlocal landing_shas
        if landing_shas is None:
            landing_shas = _plan_landing_shas(worktree_root, sorted(target_meta))
        return landing_shas.get(plan_rel)

    try:
        live_paths = collect_live_handoff_paths(worktree_root)
    except OSError as exc:
        return {
            "exit_code": 1,
            "discharged": discharged, "already_done": already_done, "refused": refused,
            "reason": f"cannot scan state/handoffs/: {exc}",
        }

    moves: List[Move] = []

    for path in live_paths:
        baton_id = rel_id(path, worktree_root)
        meta = _read_meta(str(path)) or {}

        matched_plan, candidates = _resolve_baton_plan(
            meta, plan_identifiers, plan_deliverable_ids,
        )
        if candidates:
            refused.append({
                "baton": baton_id, "plan": None,
                "reason": f"multi-hit: baton links {len(candidates)} target plans "
                          f"({', '.join(candidates)}) — cannot tell which is its own",
            })
            continue
        if matched_plan is None:
            continue  # not a baton for any target plan — out of scope, not reported

        status = str(meta.get("status") or "").strip().lower()
        if status and status not in _VALID_HANDOFF_STATUSES:
            refused.append({
                "baton": baton_id, "plan": matched_plan,
                "reason": f"malformed-status: status={status!r} is not a legal handoff status "
                          f"{sorted(_VALID_HANDOFF_STATUSES)} (DR-084)",
            })
            continue

        if _already_discharged(meta):
            already_done.append({"baton": baton_id, "plan": matched_plan})
            continue

        landing_sha = _landing_sha_for(matched_plan)
        if not landing_sha:
            refused.append({
                "baton": baton_id, "plan": matched_plan,
                "reason": f"landing-sha-unresolvable: no `status: {plan_status[matched_plan]}` "
                          f"flip commit found for {matched_plan}",
            })
            continue

        try:
            old_text = path.read_text(encoding="utf-8")
            new_text = _stamp_text(old_text, landing_sha)
        except (OSError, LandingRefused) as exc:
            refused.append({
                "baton": baton_id, "plan": matched_plan,
                "reason": f"stamp-failed: {exc}",
            })
            continue

        try:
            replace_text(path, new_text)
        except OSError as exc:
            refused.append({
                "baton": baton_id, "plan": matched_plan,
                "reason": f"stamp-write-failed: {exc}",
            })
            continue

        dst = handoff_archive_dest(worktree_root, path)
        moves.append(Move(src=path, dst=dst, candidate_id=baton_id, restage_src=True))

    if moves:
        import asyncio

        n = len(moves)
        subject = (
            f"handoff: discharge {n} landed baton(s)\n\n"
            f"Discharged via handoff.discharge_landed."
        )
        acted, failed = asyncio.run(
            archive_and_commit(worktree_root=worktree_root, moves=moves, subject=subject)
        )
        for item in acted:
            discharged.append({"id": item["id"]})
        for item in failed:
            refused.append({
                "baton": item.get("id"), "plan": None,
                "reason": item.get("reason", "archive-and-commit-failed"),
            })

    exit_code = 2 if any(r.get("reason", "").startswith(("stamp", "archive")) for r in refused if r.get("plan") is None) else 0
    return {
        "exit_code": exit_code,
        "discharged": discharged,
        "already_done": already_done,
        "refused": refused,
    }
