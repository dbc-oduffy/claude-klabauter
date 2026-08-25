"""
coordinator_core.ops.session.sweep_consumed_handoffs — session.sweep_consumed_handoffs op.

Purpose: on-demand, single-family archival sweep for CONSUMED handoffs only.

Repointed (C5, docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md § C5): the
retired composite's boot_sweep._sweep_consumed_handoffs internal — which reached
git-history-walking machinery (fleet/_common.archive_and_commit) and best-effort
shipped_in stamping — is gone along with coordinator_core/ops/session/boot_sweep.py. This
op now selects `status: consumed` handoffs directly and layers the surviving DR-084
(a)/(b)/(e) semantics on top via the SAME reusable primitives
coordinator_core.ops.session.boot_backstop built for the rebuilt boot-time backstop
(`_classify_heir_children`, `_is_claimed_at_too_recent`, `_stamp_deployment_state_shipped`,
`relocate_candidates`, `_commit_relocations`, `_append_warn_marker`) — REUSE over
reimplementation, per CLAUDE.md Engineering Defaults, the same call this plan's own C4b
made for the boot path. (c) best-effort shipped_in stamping and (f) the heir pre-stamp
pass do NOT travel here: DR-353 (docs/decisions/DR-353-shipped-in-stamping-leaves-the-
boot-path.md) retires that cost class from this family, and this on-demand op reaches the
exact same DR-084 substrate the boot path now does — carrying (c)/(f) here while boot
lacks them would leave two divergent behavioral contracts for "sweep consumed handoffs"
depending on which door a caller uses.

Negative-spec (repointing decision, recorded for the next reader): boot_backstop's own
`_apply_dr084_disposition` SILENTLY excludes a recency-floor-too-recent candidate (no
skip entry, no WARN marker — see that function's own docstring) because the automatic
boot occasion treats that as ordinary, non-adjudicatable noise. This op's own pre-existing
test contract (test_sweep_consumed_handoffs.py, predating this repoint) requires an
on-demand caller to SEE a recency-floor skip in `consumed_handoffs.skipped` with a
"recency floor" reason — an EM invoking this by hand needs to know why a candidate they
expected to see archived did not move. This op therefore does its OWN classification pass
(`_classify_disposition` below) rather than calling `_apply_dr084_disposition` directly,
reusing that function's constituent primitives (heir classification, recency check, heir
stamp) without its silent-exclusion disposition. The two functions are deliberately not
merged: boot_backstop.py is out of this chunk's `writes:` scope (C5), and the two callers'
skip-visibility contracts are genuinely different, not an accidental drift to reconcile.

Also NOT carried forward: the former two-repo `state_common_dir` split (AC7 of the
retired plan this composite came from). boot_backstop's own rebuild is single-worktree
only (see that module's negative-spec) and this op now shares its DR-084 substrate, so a
`state_common_dir` naming a genuinely separate state repo now returns a setup error
rather than silently degrading to a code path that no longer exists. A `state_common_dir`
that resolves to the SAME worktree (the unified-state, byte-identical-to-today case that
covers this repo's actual deployment) is unaffected.

Why this op exists at all (C21, 2026-07-23): before it, the ONLY manual route to archive
consumed handoffs was the composite session.boot_sweep, which also ran the
unintegrated-findings reap (a tracked git-rm delete of state/review-trail/findings/*.md
sidecars) — so an EM who wanted "just archive the consumed batons" had to accept an
unrelated destructive sweep alongside it. This op isolates exactly one family; that
reason for existing is unchanged by the repoint.

params:
  repo_root (str, optional): D3 consistency check only — NOT the path source (matches
      session.boot_sweep's own param; see that op's docstring for the doctrine).
  state_common_dir (str, optional): must resolve to the SAME worktree as `repo_root`'s
      common dir (unified-state collapse) — see negative-spec above. A genuinely
      different state repo is a setup error post-repoint.
  dry_run (bool, optional, default false): preview candidates without mutating anything.
      Never writes a WARN marker, never stamps a heir's deployment_state, never moves or
      commits — the classification pass alone (`_classify_disposition`) determines the
      preview, and that pass never mutates disk by construction.
  session_id (str, optional): threaded to `relocate_touched_path` so a relocated path's
      touch-claim (if any) is restated on its new location. Falls back to
      `coordinator_core.contract.apply_base.resolve_explicit_session_id`'s own
      environment read, then to boot_backstop's `_FALLBACK_SESSION_ID`.

Result envelope (own shape, NOT the fleet mode/dry_run/candidates wire envelope — mirrors
session.boot_sweep's own negative-spec: this is a session.* op, not a fleet.* op):
  exit_code 0 — completed, no per-item failures.
  exit_code 1 — setup error (missing repo_root, D3 mismatch, unsupported state_common_dir).
  exit_code 2 — one or more per-item failures (DETERMINATE-PARTIAL), including a batch
      whose move(s) succeeded but whose commit did not land.
  consumed_handoffs.archived  — acted [{"id": repo-rel}, ...] on a live run, WOULD-archive
      candidates [{"id": repo-rel, "heir": bool}, ...] on a dry run.
  consumed_handoffs.skipped   — recency-floor skips + DR-084 awaiting-adjudication skips,
      NEVER silently dropped — every skip carries an "id" and a "reason".
  consumed_handoffs.failed    — always [] on a dry run (nothing was attempted).
  warnings — structured scan-failure notices (e.g. an unreadable handoffs subtree, or a
      fail-closed heir-classification retain) — never affects exit_code.

Anti-scope: does NOT run terminal-plans, shipped-handoffs, actioned-memos, or
unintegrated-findings-reap — those each have their own single-family CLI; see
coordinator/bin/sweep-terminal-plans.py, sweep-shipped-handoffs.py, sweep-actioned-
memos.py, and this op's own sibling CLI coordinator/bin/sweep-consumed-handoffs.py. Does
NOT build an occasion registry — this ships a command, not a claim about when it fires
(see plan C21 anti-scope note). Does NOT walk git history and does NOT best-effort-stamp
shipped_in — see the repoint note above.

Self-registration: importing this module calls
register_op("session.sweep_consumed_handoffs", _handler) as a side-effect.
Add to coordinator_core/ops/__init__.py to trigger registration.

Spec backlinks:
  - pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C21 (original)
  - docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md § C5 (this repoint)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.contract.apply_base import (
    resolve_explicit_session_id as _resolve_explicit_session_id,
)
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root
from coordinator_core.ops.fleet.archive_handoffs import (
    _classify_heir_children,
    _collect_all_handoff_paths,
)
from coordinator_core.ops.session.boot_backstop import (
    _AWAITING_ADJUDICATION_REASON,
    _FALLBACK_SESSION_ID,
    _append_warn_marker,
    _git,
    _is_claimed_at_too_recent,
    _stamp_deployment_state_shipped,
    _strip_quotes,
    relocate_candidates,
)

_LOG = logging.getLogger(__name__)

_CONSUMED_STATUS = "consumed"


def _build_result(
    archived: list,
    skipped: list,
    failed: list,
    warnings: Optional[list] = None,
) -> dict:
    return {
        "exit_code": 2 if failed else 0,
        "warnings": warnings or [],
        "consumed_handoffs": {
            "archived": archived,
            "skipped": skipped,
            "failed": failed,
        },
    }


def _build_error_result(message: str) -> dict:
    return {
        "exit_code": 1,
        "error": message,
        "warnings": [],
        "consumed_handoffs": {"archived": [], "skipped": [], "failed": []},
    }


def _enumerate_consumed_candidates(worktree: Path) -> Tuple[List[Path], List[dict]]:
    """Sorted `status: consumed` handoffs under state/handoffs/*.md, plus a list of
    paths this could not classify (missing/unparseable frontmatter, or an OSError
    reading the file) — degrading safe by excluding, never guessing.
    """
    live_root = worktree / "state" / "handoffs"
    if not live_root.is_dir():
        return [], []
    candidates: List[Path] = []
    unreadable: List[dict] = []
    for path in sorted(live_root.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            unreadable.append({"path": path, "reason": str(exc)})
            continue
        split = split_frontmatter(text)
        if split is None:
            unreadable.append({"path": path, "reason": "no parseable frontmatter"})
            continue
        status = _strip_quotes(read_fm_field(split.fm_text, "status")).lower()
        if status == _CONSUMED_STATUS:
            candidates.append(path)
    return candidates, unreadable


def _read_sid(handoff_path: Path) -> str:
    """Best-effort claimed_by/consumed_by read for the (d) WARN marker's own
    `consumed_by=` field — mirrors boot_backstop's `_apply_dr084_disposition` inline
    read. Returns "unknown" on any read/parse failure rather than raising.
    """
    try:
        text = handoff_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    split = split_frontmatter(text)
    fm_text = split.fm_text if split is not None else ""
    return (
        _strip_quotes(read_fm_field(fm_text, "claimed_by"))
        or _strip_quotes(read_fm_field(fm_text, "consumed_by"))
        or "unknown"
    )


def _commit_consumed_relocations(
    worktree: Path, moved: List[dict], notes_touched: bool
) -> dict:
    """ONE scoped `git add -A` plus ONE `git commit` covering exactly the paths this
    op ever touches — `state/handoffs`, `archive/handoffs`, and (when a (d)/(b) WARN
    marker was actually appended this run) `tasks/orphan-sweep-notes.md`.

    Deliberately NOT `boot_backstop._commit_relocations`: that helper scopes its own
    `git add -A` to `state/handoffs`/`archive/handoffs` only, because the boot-time
    hot path defers the WARN-marker file to a later ceremony commit (no `git status
    clean` contract on that path — see that module's own tests). This op's
    pre-existing test contract (`test_sweep_consumed_handoffs.py`, predating this
    repoint) requires a clean working tree after a live run, so the marker file must
    be staged and committed in the SAME commit as the archival it documents.
    """
    if not moved and not notes_touched:
        return {"committed": False, "sha": None, "reason": "no candidates"}

    add_paths = ["state/handoffs", "archive/handoffs"]
    if notes_touched:
        add_paths.append("tasks/orphan-sweep-notes.md")

    add = _git(worktree, ["add", "-A", "--", *add_paths])
    if add.returncode != 0:
        return {
            "committed": False,
            "sha": None,
            "reason": f"git add -A failed: {add.stderr.strip()}",
        }

    message = f"session.sweep_consumed_handoffs: archive {len(moved)} consumed handoff(s)"
    commit = _git(worktree, ["commit", "-m", message])
    if commit.returncode != 0:
        return {
            "committed": False,
            "sha": None,
            "reason": f"git commit failed: {commit.stderr.strip()}",
        }

    return {"committed": True, "sha": None, "reason": None}


async def _classify_disposition(
    worktree: Path, candidates: List[Path]
) -> Tuple[List[dict], List[dict], List[dict], List[dict]]:
    """Read-only DR-084 (a)/(b)/(e)-aware classification over `candidates` — never
    mutates disk. Reuses `boot_backstop`'s recency check and heir-classification
    primitive; does NOT call `boot_backstop._stamp_deployment_state_shipped` (that
    write is deferred to the handler, which skips it entirely under dry_run) and does
    NOT silently drop a recency-floor skip the way `boot_backstop._apply_dr084_
    disposition` does — see this module's docstring negative-spec for why.

    Returns (to_archive, awaiting_adjudication, recency_skipped, retained):
      to_archive             — [{"path": Path, "heir": bool}, ...]
      awaiting_adjudication  — [{"path": Path}, ...] — (b) skip-and-surface candidates.
      recency_skipped        — [{"path": Path}, ...] — (a) too-recent candidates.
      retained               — [{"path": Path, "reason": str}, ...] — heir
                                classification failures / dag_index incompleteness /
                                unreadable files (fail-closed retains, surfaced as
                                warnings by the caller, not as a per-item skip).
    """
    dag_scan_errors: List[str] = []
    dag_index = _collect_all_handoff_paths(worktree, scan_errors=dag_scan_errors)
    dag_incomplete = bool(dag_scan_errors)

    to_archive: List[dict] = []
    awaiting_adjudication: List[dict] = []
    recency_skipped: List[dict] = []
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
            recency_skipped.append({"path": handoff_path})
            continue

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
            to_archive.append({"path": handoff_path, "heir": True})
            continue

        deployment_state = _strip_quotes(read_fm_field(fm_text, "deployment_state")).lower()
        if deployment_state == "in_flight":
            awaiting_adjudication.append({"path": handoff_path})
            continue

        to_archive.append({"path": handoff_path, "heir": False})

    return to_archive, awaiting_adjudication, recency_skipped, retained


@register_op("session.sweep_consumed_handoffs")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """session.sweep_consumed_handoffs — on-demand single-family consumed-handoff sweep.

    See module docstring for the full behavioral contract and the repoint's
    negative-spec. The worktree/D3 resolution glue below is a deliberate small
    duplication of session.boot_backstop._handler's own equivalent block — same
    tradeoff that module's own docstring already names for its local
    reimplementations (import-budget / independently-evolving on-demand op).
    """
    if repo_root is None:
        _LOG.error("session.sweep_consumed_handoffs: repo_root handler arg is None")
        return _build_error_result("repo_root handler arg is None")

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        return _build_error_result(mismatch)

    # Unified-state only, post-repoint — see module docstring negative-spec.
    raw_state = params.get("state_common_dir")
    if raw_state is not None:
        state_common_path = (
            Path(raw_state) if not isinstance(raw_state, Path) else raw_state
        )
        if not (state_common_path / "HEAD").exists():
            return _build_error_result(
                f"state_common_dir does not appear to be a valid git common dir "
                f"(no HEAD file at {state_common_path}/HEAD) — "
                f"pass the .git common dir form (e.g. /path/to/repo/.git), "
                f"not the worktree root"
            )
        state_worktree_candidate = main_worktree_root(state_common_path)
        if state_worktree_candidate.resolve() != worktree.resolve():
            return _build_error_result(
                "state_common_dir naming a separate state repo is no longer supported "
                "— the rebuilt DR-084 substrate this op now shares with "
                "session.boot_sweep (coordinator_core.ops.session.boot_backstop) "
                "operates against a single worktree only"
            )

    dry_run = bool(params.get("dry_run", False))

    candidates, enum_unreadable = _enumerate_consumed_candidates(worktree)
    to_archive, awaiting_adjudication, recency_skipped, retained = await _classify_disposition(
        worktree, candidates
    )

    archived: List[dict] = []
    failed: List[dict] = []

    if dry_run:
        archived = [
            {"id": item["path"].relative_to(worktree).as_posix(), "heir": item["heir"]}
            for item in to_archive
        ]
    else:
        session_id = (
            _resolve_explicit_session_id(params.get("session_id")) or _FALLBACK_SESSION_ID
        )
        for item in to_archive:
            if item["heir"]:
                _stamp_deployment_state_shipped(item["path"])

        moved, move_failed = relocate_candidates(
            worktree, [item["path"] for item in to_archive], session_id
        )
        moved_srcs = {m["from"] for m in moved}

        # WARN markers BEFORE the commit (not after, unlike boot_backstop's own
        # ordering) — see _commit_consumed_relocations' docstring for why this op
        # must stage tasks/orphan-sweep-notes.md in the same commit as the move.
        for item in to_archive:
            src_rel = item["path"].relative_to(worktree).as_posix()
            if src_rel not in moved_srcs:
                continue
            sid = _read_sid(item["path"])
            note = (
                "deployment_state stamped shipped" if item["heir"]
                else "no deployment_state change"
            )
            _append_warn_marker(worktree, item["path"].name, sid, note, verb="archived")

        for item in awaiting_adjudication:
            sid = _read_sid(item["path"])
            _append_warn_marker(
                worktree,
                item["path"].name,
                sid,
                disposition_note="awaiting human adjudication or DR-084 continued semantics",
                verb="skipped",
            )

        notes_touched = bool(moved) or bool(awaiting_adjudication)
        commit_result = _commit_consumed_relocations(worktree, moved, notes_touched)

        archived = [{"id": m["from"]} for m in moved]
        failed = [{"id": f["path"], "reason": f["reason"]} for f in move_failed]
        if moved and not commit_result["committed"]:
            failed.append({
                "id": "commit",
                "reason": commit_result["reason"] or "commit failed",
            })

    skipped = [
        {
            "id": item["path"].relative_to(worktree).as_posix(),
            "reason": _AWAITING_ADJUDICATION_REASON,
        }
        for item in awaiting_adjudication
    ] + [
        {
            "id": item["path"].relative_to(worktree).as_posix(),
            "reason": "consumed_at within 30min recency floor",
        }
        for item in recency_skipped
    ]

    warnings = [
        {
            "scope": "consumed_handoffs",
            "reason": f"unreadable ({item['path'].relative_to(worktree).as_posix()}): {item['reason']}",
        }
        for item in enum_unreadable
    ] + [
        {
            "scope": "consumed_handoffs",
            "reason": f"{item['path'].relative_to(worktree).as_posix()}: {item['reason']}",
        }
        for item in retained
    ]

    return _build_result(archived, skipped, failed, warnings)
