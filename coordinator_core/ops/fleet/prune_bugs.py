"""
coordinator_core.ops.fleet.prune_bugs — fleet.prune_closed_bugs op handler.

Purpose: git-mv closed bug-backlog YAML entries from state/bug-backlog/*.yaml
into archive/bug-backlog/YYYY-MM/ under a confirm→act (dry_run:true / dry_run:false)
wire contract.

Terminality predicate: frontmatter `status: closed` on state/bug-backlog/*.yaml.
Archive destination: archive/bug-backlog/YYYY-MM/ where YYYY-MM is derived from
the filename prefix (YYYY-MM-DD-slug.yaml); falls back to the `created:` frontmatter
field.  No existing shell mechanism exists for the structured YAML bug-backlog
(blueprint §12a; the legacy prose bug-backlog.md pruning path operated on a
different file, NOT state/bug-backlog/*.yaml).

Self-registration: @register_op fires at import time.  coordinator_core/ops/__init__.py
imports this module so the registration fires at start_server().

Spec backlinks:
  - Plan (C4):    docs/plans/2026-07-04-pcore-11-fleet-invoke-ops.md § C4
  - Blueprint:    tasks/fleet-ops-pcore-11/blueprint.md §12a
  - Wire contract (FROZEN): coordinator_core/contract/cockpit-invoke-producer-contract.md
                             §2.1, §2.2 op-specific terminality, §3, §5
  - DR-211:       docs/decisions/DR-211-fleet-op-substrate-write-boundary.md (D1–D4, five bounds)
  - DR-208:       docs/decisions/DR-208-invoke-op-authz-model.md (MUTATING classification)

Negative-spec:
  - Does NOT operate on the legacy prose bug-backlog.md — that path cannot accept
    candidate_ids, and cannot perform per-item D1 re-verify or surface per-item
    failed[] (plan Decision 1).
  - Does NOT use blocking subprocess.run (DR-211 D4 async mandate).
  - Does NOT use git add -A or git add . — scoped exact-pathspec only (DR-211 D3 Invariant 4).
  - Does NOT modify rag's relational store (DR-211 D5 five bounds).
  - Does NOT use params.repo_root as worktree source — worktree is derived via
    main_worktree_root(common_dir) (plan Key Decision 5).
"""

from __future__ import annotations
import sys

import logging
import re
from pathlib import Path
from typing import List, Optional

import yaml
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    Move,
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

# Terminality predicate for bugs.
_TERMINAL_STATUS = frozenset({"closed"})

# Filename date-prefix pattern: YYYY-MM-DD-slug.yaml
_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")


# ---------------------------------------------------------------------------
# Plain-YAML reader — bug-backlog files are plain YAML (no --- fences)
# ---------------------------------------------------------------------------

def _read_plain_yaml(path: Path) -> dict:
    """Read a plain-YAML bug-backlog file and return its content as a dict.

    Review: code-reviewer (F1) — replaces _read_meta() which requires '---' fences;
    real state/bug-backlog/*.yaml files are plain YAML with no delimiters.
    Returns {} on any parse error or if the file does not contain a mapping.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        print(f"skip: _read_plain_yaml: with open(path, \"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {}


# ---------------------------------------------------------------------------
# Terminality helpers
# ---------------------------------------------------------------------------

def _is_terminal(status: Optional[str]) -> bool:
    """Return True iff the bug's status is in the terminal set (status: closed)."""
    return status in _TERMINAL_STATUS


def _archive_month(path: Path) -> str:
    """Derive the YYYY-MM archive sub-directory from a bug YAML path.

    Primary source: filename prefix YYYY-MM-DD-slug.yaml (matches the plan/handoff
    archive-path convention used by the shell sweeps).
    Fallback: `created:` frontmatter field (YYYY-MM-DD or YYYY-MM).
    Last resort: "unknown" — never raises.

    The archive destination is archive/bug-backlog/<YYYY-MM>/<filename>.
    """
    # Primary: filename prefix
    m = _DATE_PREFIX_RE.match(path.name)
    if m:
        return m.group(1)

    # Fallback: created: plain-YAML field
    meta = _read_plain_yaml(path)
    if meta:
        created = meta.get("created")
        if created:
            created_str = str(created)
            # Accept YYYY-MM-DD or YYYY-MM
            if re.match(r"^\d{4}-\d{2}", created_str):
                return created_str[:7]

    _LOG.warning("prune_bugs: could not derive YYYY-MM for %s; using 'unknown'", path.name)
    return "unknown"


def _enumerate_bugs(worktree_root: Path) -> List[Path]:
    """Return all *.yaml files under state/bug-backlog/ sorted by name."""
    bug_dir = worktree_root / "state" / "bug-backlog"
    if not bug_dir.is_dir():
        return []
    return sorted(bug_dir.glob("*.yaml"))


def _candidate_dict(path: Path, worktree_root: Path) -> dict:
    """Build a candidate entry dict for a terminal bug file (dry_run:true output).

    contract §2.1 :176-215 — fields: id, title, status, family, terminal_since, note.
    - id: repo-relative source path (the wire key, matches Channel-B provenance.path).
    - title: from frontmatter `title:` field; falls back to filename stem.
    - status: from frontmatter (should be "closed").
    - family: "bug".
    - terminal_since: null — bug entries do not carry a date-of-closure; degrade gracefully.
    - note: null for bugs (handoff-only field, contract §2.1).
    """
    meta = _read_plain_yaml(path)
    return {
        "id": rel_id(path, worktree_root),
        "title": meta.get("title") or path.stem,
        "status": meta.get("status") or "closed",
        "family": "bug",
        "terminal_since": None,  # not tracked in bug entries; degrade gracefully per contract
        "note": None,
    }


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_op("fleet.prune_closed_bugs")
async def _handler(params: dict, repo_root=None) -> dict:
    """fleet.prune_closed_bugs — archive closed bug-backlog YAML entries.

    Wire contract: coordinator_core/contract/cockpit-invoke-producer-contract.md
    Terminality: frontmatter status: closed on state/bug-backlog/*.yaml.
    Archive dest: archive/bug-backlog/YYYY-MM/ (YYYY-MM from filename prefix or created:).

    dry_run:true  → candidates[] of closed bugs; mutates nothing.
    dry_run:false → D1 re-verify each candidate_id at T3; git-mv + commit acted set;
                    return acted/skipped/failed with exit_code 0 or 2.

    repo_root arg: the git common dir delivered by _OP_KEY_SCOPE="common_dir".
                   Handlers MUST NOT use ctx.repo_root (None in the global service).
                   Worktree root is derived via main_worktree_root(repo_root).
                   params.repo_root is the D3 consistency check only — NOT the
                   worktree-root resolution source (plan Key Decision 5).
    """
    # ---- param validation (mode fail-closed, candidate_ids required on act) ----
    result = validate_params(params)
    if isinstance(result, dict):
        return result  # exit_code:1 setup-error envelope
    mode, dry_run, candidate_ids = result

    # ---- D3 repo_root consistency check (contract §3.3) ----
    if repo_root is not None:
        common_dir = Path(repo_root)
        mismatch = check_repo_root(params.get("repo_root"), common_dir)
        if mismatch:
            return build_setup_error_result(mode, dry_run, mismatch)
        worktree = main_worktree_root(common_dir)
    else:
        # repo_root is None only in unit tests that bypass the keying table.
        # Fail loudly in production; test fixtures supply an explicit value.
        _LOG.error(
            "fleet.prune_closed_bugs: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production"
        )
        return build_setup_error_result(
            mode, dry_run,
            "repo_root is None; cannot derive worktree root (keying-table misconfiguration)",
        )

    # ---- dry_run:true — preview (read-only; mutates nothing) ----
    if dry_run:
        bug_paths = _enumerate_bugs(worktree)
        candidates = []
        for path in bug_paths:
            meta = _read_plain_yaml(path)
            status = meta.get("status")
            if _is_terminal(status):
                candidates.append(_candidate_dict(path, worktree))
        return build_dry_run_result(mode, candidates)

    # ---- dry_run:false — act ----
    # candidate_ids guaranteed non-empty by validate_params.
    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []
    moves: List[Move] = []

    # Resolve the allowed root once outside the loop (CRITICAL 2 path-traversal fix).
    bug_dir_safe = (worktree / "state" / "bug-backlog").resolve()

    for cid in candidate_ids:
        src = worktree / cid

        # Path-traversal containment guard (CRITICAL 2): reject candidate_id that
        # resolves outside state/bug-backlog/ — covers absolute-path override and ../ traversal.
        # Must run BEFORE any file read or git op on src.
        resolved_src = src.resolve()
        if not resolved_src.is_relative_to(bug_dir_safe):
            _LOG.warning(
                "prune_bugs: rejecting path-traversal candidate_id %r "
                "(resolved %s escapes state/bug-backlog/)", cid, resolved_src,
            )
            failed.append({"id": cid, "reason": "path-traversal: candidate_id escapes state/bug-backlog/"})
            continue
        src = resolved_src

        # Already-archived (source gone): idempotent replay per DR-211 D2(i).
        if not src.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        # D1 act-time re-verify: re-read status at T3 (contract §3.1 :231-267).
        meta = _read_plain_yaml(src)
        status_at_t3 = meta.get("status")
        if not _is_terminal(status_at_t3):
            _LOG.info(
                "fleet.prune_closed_bugs: D1 drift — %s status=%r at T3, skipping",
                cid, status_at_t3,
            )
            skipped.append({"id": cid, "reason": f"drifted-open: status={status_at_t3!r}"})
            continue

        # Derive archive destination path.
        ym = _archive_month(src)
        dst = worktree / "archive" / "bug-backlog" / ym / src.name

        if dst.exists():
            # Destination already present (concurrent archive or replay edge-case).
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        moves.append(Move(src=src, dst=dst, candidate_id=cid))

    if moves:
        # TOCTOU note: a narrow window exists between the per-candidate D1 terminality
        # re-verify above and the git-mv inside archive_and_commit below.  This is the
        # accepted DR-211 D1-at-act residual; the T3 re-verify already narrows the window
        # to the call-site gap (same shape as archive_plans).
        # Review: code-reviewer (F8) — aligned prefix+count format with sibling handlers.
        commit_subject = (
            f"fleet: prune {len(moves)} closed bug "
            f"{'entry' if len(moves) == 1 else 'entries'} [fleet.prune_closed_bugs]"
        )
        new_acted, new_failed = await archive_and_commit(worktree, moves, commit_subject)
        acted.extend(new_acted)
        failed.extend(new_failed)

    return build_act_result(mode, acted, skipped, failed)
