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

