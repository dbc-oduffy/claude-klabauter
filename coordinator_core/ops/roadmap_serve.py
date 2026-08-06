"""
coordinator_core.ops.roadmap_serve — JSON-RPC "roadmap.serve" operation.

Purpose: Read-only live single-initiative view — serves one roadmap's DAG +
scalars (nodes, edges, roll_up, critical_path) by calling assemble_roadmap_dag()
on the live handoff frontmatter.  No emit-cycle dependency.

This is cockpit's single-initiative live-render surface (claude-klabauter serves live
single; rag serves durable fleet/cross-initiative queries via the edge table).

Self-registration: importing this module calls
``register_op("roadmap.serve", _handler)`` as a side-effect.  Add this module to
``coordinator_core/ops/__init__.py`` to trigger registration at start_server() time.

Worktree resolution (mirrors handoff_children.py):
  - When ``repo_root`` is provided (router-supplied git common dir), the worktree
    root is derived via ``main_worktree_root(repo_root)``.
  - If ``repo_root`` is absent the op returns an empty well-formed payload with a
    logged warning rather than raising — an unknown worktree is not a 500.

shipped_sha precision (F1 — the Staff Engineer review):
  raw stub frontmatter carries ``shipped_in`` as a bare SHA scalar
  (e.g. ``shipped_in: 0983062``).  On this live serve path, ``shipped_in`` is
  carried through as-is and exposed as ``shipped_sha`` WITHOUT running
  ``_stamp_shipped_sha`` git ancestor verification.  ``_stamp_shipped_sha``
  (with proper shape normalisation) is reserved for the gated C7 emit path only,
  where batch git I/O is acceptable.  This op MUST NOT call any git subprocess —
  zero-spawn hot-path SLA.

Zero-node guard (F3): a call for an unknown/unrecognised roadmap_id returns a
well-formed empty payload ``{nodes:[], edges:[], roll_up:{…null}, critical_path:[]}``
rather than raising.

Spec backlink: docs/plans/2026-07-05-claude-klabauter-served-initiative-roadmap-read-model.md § C5

Negative-spec:
  - Does NOT call ``_stamp_shipped_sha`` or any git subprocess on this path.
  - Does NOT read the roadmap from OVERVIEW.md / STUB-INDEX.md / STUB-NUMBERING.md —
    live authoritative source is stub handoff frontmatter (blocks/blocked_by arrays).
  - Does NOT store both edge directions — stores blocks, derives blocked_by.
  - Does NOT emit phantom nodes for dangling edges.
  - Does NOT use ``_simple_yaml_load`` for stub handoff frontmatter — only
    ``_parse_frontmatter`` (coordinator_core.dag) is used (via roadmap_dag.py).
  - Does NOT serve state/initiatives/ or the returned DAG as a fleet-wide/cross-initiative
    query surface — state/ IS claude-klabauter's own disk-truth custody for its initiatives (per
    docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md); this op's negative
    scope is that it does not additionally act as rag's fleet-wide query surface over that data.
  - critical_path is NOT a duration-weighted CPM path (F7); it is the unweighted
    longest-chain by node count.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag

_LOG = logging.getLogger(__name__)

# Review: code-reviewer — removed module-level _EMPTY_DAG mutable constant; dict(_EMPTY_DAG) was
# a shallow copy: nested lists (nodes, edges, critical_path) and the roll_up dict would be shared
# across calls if any caller mutated the returned dict before it was serialised. Inline construction
# at each return site is the cleanest fix — no import of copy.deepcopy needed.


@register_op("roadmap.serve")
async def _handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "roadmap.serve" handler.

    Returns the live DAG + scalars for a single roadmap, assembled from live and
    archived stub handoff frontmatter (no emit-cycle dependency).

    Params:
        roadmap_id: str — identifier of the roadmap to serve.

    Returns:
        {
            "roadmap_id": str,
            "nodes":        [{stub_id, status, sprint, wave, shipped_sha, roadmap_id}],
            "edges":        [{from, to, type:"blocks", roadmap_id}],
            "roll_up":      {total, by_status:{status:count}, pct_shipped: float|null},
            "critical_path": [stub_id, ...],
        }

    Zero-node guard (F3): returns a well-formed empty payload rather than raising
    when roadmap_id is unknown or has no materialized handoff stubs.

    shipped_sha precision (F1): bare SHA scalar from frontmatter, carried as-is —
    NO git ancestor verification on this serve path (zero-spawn SLA).

    Worktree resolution mirrors handoff_children.py:
    - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
    - Neither → return empty payload with logged warning
    """
    roadmap_id: Optional[str] = params.get("roadmap_id")
    if not roadmap_id or not isinstance(roadmap_id, str):
        _LOG.warning(
            "roadmap.serve: missing or invalid 'roadmap_id' param — "
            "returning empty payload"
        )
        return {"nodes": [], "edges": [], "roll_up": {"total": 0, "by_status": {}, "pct_shipped": None}, "critical_path": []}

    # Derive the worktree root from the router-supplied common dir.
    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)  # router common_dir → worktree root
    else:
        _LOG.warning(
            "roadmap.serve: no repo_root resolved — "
            "repo_root arg absent; returning empty payload for roadmap_id=%r",
            roadmap_id,
        )
        return {"nodes": [], "edges": [], "roll_up": {"total": 0, "by_status": {}, "pct_shipped": None}, "critical_path": []}

    # assemble_roadmap_dag is a pure derivation helper (no git subprocess, no spawn).
    # It carries shipped_in as-is (bare SHA scalar) without _stamp_shipped_sha (F1).
    dag = assemble_roadmap_dag(roadmap_id=roadmap_id, worktree_root=worktree_root)

    return {
        "roadmap_id": roadmap_id,
        "nodes": dag["nodes"],
        "edges": dag["edges"],
        "roll_up": dag["roll_up"],
        "critical_path": dag["critical_path"],
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        # Propagate assemble_roadmap_dag's scan_incomplete/scan_errors signal —
        # dropping it here would silently defeat the roadmap_dag.py fix: a
        # partial DAG from an unreadable handoff subtree must not read as a
        # clean result to cockpit.
        "scan_incomplete": dag.get("scan_incomplete", False),
        "scan_errors": dag.get("scan_errors", []),
        # --- end Tier 2 ---
    }
