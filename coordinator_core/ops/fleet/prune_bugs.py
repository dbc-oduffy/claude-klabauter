"""
coordinator_core.ops.fleet.prune_bugs — fleet.prune_closed_bugs op handler.

Purpose: archive closed bug-backlog YAML entries from state/bug-backlog/*.yaml
into archive/bug-backlog/YYYY-MM/ under a confirm→act (dry_run:true / dry_run:false)
wire contract.

Terminality predicate: `status: closed` on state/bug-backlog/*.yaml (plain YAML,
no leading `---` fence).
Archive destination: archive/bug-backlog/YYYY-MM/ where YYYY-MM is derived from
the filename prefix (YYYY-MM-DD-slug.yaml); falls back to the `created:`
frontmatter field.

Discovery is two-stage (docs/plans/2026-09-07-fleet-prune-closed-bugs-v2-rebuild.md
C2/C3): stage 1 is a frontmatter-bounded substring scan (stop reading each file at
its closing `---`, or at the first `---` for a file with no leading fence — which
is every bug-backlog file on this corpus, so stage 1 in practice reads to EOF for
these files, bounded only by the file's own length) for the literal text
`status: closed`; stage 2 is `yaml.safe_load` confirmation on stage-1 candidates
ONLY, never the whole corpus. Measured at state/audits/2026-09-11-prune-closed-
bugs-discovery-budget.md: well under the 200ms process-time line at both the live
and ~2x corpus corners, spawn count 0.

Self-registration: @register_op fires at import time.  coordinator_core/ops/__init__.py
imports this module so the registration fires at start_server() (P045-C5).

Spec backlinks:
  - Plan (v2 rebuild): docs/plans/2026-09-07-fleet-prune-closed-bugs-v2-rebuild.md (C3)
  - Plan (original):   docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md § C4
  - Blueprint:    tasks/fleet-ops-pcore-11/blueprint.md §12a
  - Wire contract (FROZEN): coordinator_core/contract/cockpit-invoke-producer-contract.md
                             §2.1, §2.2 op-specific terminality, §3, §5
  - DR-211:       docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1-D4, five bounds)
  - DR-208:       docs/decisions/DR-208-invoke-op-authz-model.md (MUTATING classification)
  - DR-344:       docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md (500ms bar)
  - Kill ledger:  .coordinator-local/kill-ledger.md ## K-021, K-105 table row

Negative-spec:
  - Does NOT operate on the legacy prose bug-backlog.md — that path cannot accept
    candidate_ids, and cannot perform per-item D1 re-verify or surface per-item
    failed[] (plan Decision 1).
  - Does NOT use blocking subprocess.run, and has no subprocess/git call site at
    all — the act path makes exactly ONE `archive_and_commit` call (DR-211 D4
    async mandate; V2-FROM-ZERO reuse mandate).
  - Does NOT call `coordinator_core/git/commit.py :: commit_paths` directly, and
    does NOT reimplement, fork, or wrap `_common.py::archive_and_commit`,
    `_is_identical_duplicate`, or `_REASON_DEST_CONFLICT` — those are reused
    verbatim, as the eight sibling archive ops do.
  - Does NOT modify rag's relational store (DR-211 D5 five bounds).
  - Does NOT use params.repo_root as worktree source — worktree is derived via
    main_worktree_root(common_dir) (plan Key Decision 5).
  - Does NOT gate on any session/claim/holder field — bug-backlog entries are not
    session-scoped; terminality is `status:` alone.
  - Does NOT full-read every corpus file for stage-1 discovery — the scan is
    frontmatter-bounded, and full `yaml.safe_load` is deferred to stage 2,
    confirmed candidates only.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

import yaml
from coordinator_core.ipc import register_op
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
    rel_id,
    validate_params,
)

_LOG = logging.getLogger(__name__)

_TERMINAL_STATUS = frozenset({"closed"})

_STAGE1_NEEDLE = "status: closed"

# Filename date-prefix pattern: YYYY-MM-DD-slug.yaml
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")


def _read_plain_yaml(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        _LOG.warning("prune_bugs: _read_plain_yaml failed for %s: %s", path, exc)
        return {}


def _is_terminal(status: Optional[str]) -> bool:
    return status in _TERMINAL_STATUS


def _archive_month(path: Path) -> str:
    """Derive the YYYY-MM archive sub-directory from a bug YAML path.

    Primary source: filename prefix YYYY-MM-DD-slug.yaml (matches the plan/handoff
    archive-path convention used by the shell sweeps).
    Fallback: `created:` frontmatter field (YYYY-MM-DD or YYYY-MM).
    Last resort: "unknown" — never raises.

    The archive destination is archive/bug-backlog/<YYYY-MM>/<filename>.
    """
    m = _DATE_PREFIX_RE.match(path.name)
    if m:
        return m.group(1)

    meta = _read_plain_yaml(path)
    if meta:
        created = meta.get("created")
        if created:
            created_str = str(created)
            if re.match(r"^\d{4}-\d{2}", created_str):
                return created_str[:7]

    _LOG.warning("prune_bugs: could not derive YYYY-MM for %s; using 'unknown'", path.name)
    return "unknown"


def _enumerate_bugs(worktree_root: Path) -> List[Path]:
    bug_dir = worktree_root / "state" / "bug-backlog"
    if not bug_dir.is_dir():
        return []
    return sorted(bug_dir.glob("*.yaml"))


def _stage1_frontmatter_bounded_scan(path: Path) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline()
            fence_opened = first_line.strip() == "---"
            if fence_opened:
                for line in fh:
                    if line.strip() == "---":
                        return False
                    if _STAGE1_NEEDLE in line:
                        return True
                return False
            if _STAGE1_NEEDLE in first_line:
                return True
            for line in fh:
                if _STAGE1_NEEDLE in line:
                    return True
            return False
    except Exception as exc:
        _LOG.warning("prune_bugs: stage-1 scan failed for %s: %s", path, exc)
        return False


def _discover_candidates(worktree_root: Path) -> List[Path]:
    stage1_candidates = [p for p in _enumerate_bugs(worktree_root) if _stage1_frontmatter_bounded_scan(p)]

    confirmed: List[Path] = []
    for path in stage1_candidates:
        meta = _read_plain_yaml(path)
        if not meta:
            continue
        status = meta.get("status")
        if status is None:
            continue
        if _is_terminal(status):
            confirmed.append(path)
    return confirmed


def _candidate_dict(path: Path, worktree_root: Path) -> dict:
    meta = _read_plain_yaml(path)
    return {
        "id": rel_id(path, worktree_root),
        "title": meta.get("title") or path.stem,
        "status": meta.get("status") or "closed",
        "family": "bug",
        "terminal_since": None,
        "note": None,
    }


@register_op("fleet.prune_closed_bugs")
async def _handler(params: dict, repo_root=None) -> dict:
    """fleet.prune_closed_bugs — archive closed bug-backlog YAML entries.

    Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md
    Terminality: status: closed on state/bug-backlog/*.yaml (two-stage discovery:
    frontmatter-bounded substring scan, then yaml.safe_load confirm on candidates).
    Archive dest: archive/bug-backlog/YYYY-MM/ (YYYY-MM from filename prefix or created:).

    dry_run:true  → candidates[] of confirmed-closed bugs; mutates nothing.
    dry_run:false → D1 re-verify each candidate_id at T3; archive + commit acted
                    set through ONE _common.archive_and_commit call; return
                    acted/skipped/failed with exit_code 0 or 2.

    repo_root arg: the git common dir delivered by _OP_KEY_SCOPE="common_dir".
                   Handlers MUST NOT use ctx.repo_root (None in the global service).
                   Worktree root is derived via main_worktree_root(repo_root).
                   params.repo_root is the D3 consistency check only — NOT the
                   worktree-root resolution source.
    """
    result = validate_params(params)
    if isinstance(result, dict):
        return result
    mode, dry_run, candidate_ids = result

    if repo_root is not None:
        common_dir = Path(repo_root)
        mismatch = check_repo_root(params.get("repo_root"), common_dir)
        if mismatch:
            return build_setup_error_result(mode, dry_run, mismatch)
        worktree = main_worktree_root(common_dir)
    else:
        _LOG.error(
            "fleet.prune_closed_bugs: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production"
        )
        return build_setup_error_result(
            mode, dry_run,
            "repo_root is None; cannot derive worktree root (keying-table misconfiguration)",
        )

    if dry_run:
        confirmed = _discover_candidates(worktree)
        candidates = [_candidate_dict(path, worktree) for path in confirmed]
        return build_dry_run_result(mode, candidates)

    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []
    moves: List[Move] = []

    bug_dir_safe = (worktree / "state" / "bug-backlog").resolve()

    for cid in candidate_ids:
        src = worktree / cid

        resolved_src = src.resolve()
        if not resolved_src.is_relative_to(bug_dir_safe):
            _LOG.warning(
                "prune_bugs: rejecting path-traversal candidate_id %r "
                "(resolved %s escapes state/bug-backlog/)", cid, resolved_src,
            )
            failed.append({"id": cid, "reason": "path-traversal: candidate_id escapes state/bug-backlog/"})
            continue
        src = resolved_src

        if not src.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        meta = _read_plain_yaml(src)
        status_at_t3 = meta.get("status") if meta else None
        if not _is_terminal(status_at_t3):
            _LOG.info(
                "fleet.prune_closed_bugs: D1 drift — %s status=%r at T3, skipping",
                cid, status_at_t3,
            )
            skipped.append({"id": cid, "reason": f"drifted-open: status={status_at_t3!r}"})
            continue

        ym = _archive_month(src)
        dst = worktree / "archive" / "bug-backlog" / ym / src.name

        force = False
        if dst.exists():
            if not _is_identical_duplicate(src, dst):
                _LOG.warning(
                    "prune_bugs: %s NOT archived — a DIFFERENT file already "
                    "occupies the archive destination %s. Reconcile the two copies "
                    "before the next sweep.",
                    cid,
                    rel_id(dst, worktree),
                )
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                continue
            force = True

        moves.append(Move(src=src, dst=dst, candidate_id=cid, force=force))

    if moves:
        commit_subject = (
            f"fleet: prune {len(moves)} closed bug "
            f"{'entry' if len(moves) == 1 else 'entries'} [fleet.prune_closed_bugs]"
        )
        new_acted, new_failed = await archive_and_commit(worktree, moves, commit_subject)
        acted.extend(new_acted)
        failed.extend(new_failed)

    return build_act_result(mode, acted, skipped, failed)
