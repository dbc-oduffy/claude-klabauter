"""
coordinator_core.ops.roadmap_dag — roadmap DAG assembler (pure derivation helper).

Purpose: Builds per-roadmap {nodes, edges, roll_up, critical_path} from live and
archived stub handoff frontmatter, filtered by roadmap_id. Consumed by
roadmap_serve.py (C5) and, later, the gated emission path (C7). Does NOT register
any op — that is the caller's responsibility.

Scalars (C4):
  roll_up       — {total, by_status: {<status>: count}, pct_shipped: float | null}
                  Zero-node guard (F3): pct_shipped=null when total==0.
  critical_path — unweighted longest-chain of stub_ids by node count (NOT a
                  duration-weighted CPM path; no per-stub duration estimates available
                  — F7 Patrik review naming precision). Empty list when total==0.
                  Cycle guard: cyclic nodes are quarantined with a logged warning
                  and excluded from the traversal — no infinite loop.

Frontmatter parser (F0 — Patrik review): uses coordinator_core.dag._parse_frontmatter
(which has _parse_inline_list/_parse_yaml_list_block, purpose-built for this
frontmatter shape including blocks/blocked_by YAML arrays).  Do NOT use
_simple_yaml_load for stub handoff frontmatter — it is scalar-only and
corrupts blocks/blocked_by YAML arrays to strings (e.g. "[strang-06]" rather
than ["strang-06"]).  _simple_yaml_load is correct only for flat
state/initiatives/*.yaml files.

shipped_sha precision (F1 — Patrik review): raw stub frontmatter carries shipped_in
as a bare SHA scalar (e.g. shipped_in: 0983062), NOT a {sha: ...} dict.  On the
live serve path (C5), shipped_in is carried through as-is and exposed as shipped_sha
WITHOUT running _stamp_shipped_sha git ancestor verification.  _stamp_shipped_sha
(with proper shape normalization) is reserved for the gated C7 emit path only, where
batch git I/O is acceptable.

Node identity (F6 — Patrik review): each emitted node and edge carries roadmap_id
explicitly.  A single repo can hold multiple roadmaps; without roadmap_id, edges
across roadmaps that reuse a stub_id namespace merge incorrectly in the fleet store.

main_worktree_root precision (lesson: common-dir-keyed-ops-must-derive-the-wor):
callers must pass worktree_root as the result of main_worktree_root(common_dir) — NOT
ctx.repo_root.  See roadmap_serve.py.

Spec backlink: pln-makima-served-initiative-roadm-8e0492 § C3/C4

Negative-spec:
  - Does NOT register any op.
  - Does NOT call _stamp_shipped_sha (git I/O) on the live serve path.
  - Does NOT store both edge directions — stores blocks, derives blocked_by.
  - Does NOT emit phantom nodes for dangling edges.
  - Does NOT use _simple_yaml_load for stub handoff frontmatter.
  - Does NOT serve state/initiatives/ as a fleet-wide/cross-initiative query surface —
    state/ IS makima's own disk-truth custody (per
    docs/decisions/DR-236-state-is-disk-truth-workstate-store-is-pro.md); this module's
    negative scope is that it does not additionally act as rag's fleet-wide query surface.
  - critical_path is NOT a duration-weighted CPM path (F7); it is unweighted
    longest-chain by node count.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from coordinator_core.dag import _parse_frontmatter

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filesystem scanner — mirrors handoff_children.py._collect_handoff_paths
# ---------------------------------------------------------------------------


def _collect_stub_paths(worktree_root: Path) -> tuple[List[Path], List[str]]:
    """Scan live and archived stub handoff paths.

    Scans two subtrees, matching handoff_children.py._collect_handoff_paths:
      - state/handoffs/*.md      — live handoffs
      - archive/handoffs/**/*.md — archived handoffs (includes month-foldered subdirs)

    Args:
        worktree_root: The main worktree root (NOT the git common dir / .git dir).
                       Callers must derive this via main_worktree_root(common_dir)
                       before calling.

    Returns (paths, scan_errors):
      paths        — list of resolved Path objects successfully enumerated.
      scan_errors  — human-readable strings, one per subtree that could not be
                     scanned (empty when both subtrees scanned cleanly).
    Never raises.  A subtree scan failure is logged as a warning and recorded in
    scan_errors rather than silently dropped — an unreadable subtree is NOT the
    same as "no handoffs here" and must not be indistinguishable from it (see
    module docstring / assemble_roadmap_dag's scan_incomplete signal).
    """
    paths: List[Path] = []
    scan_errors: List[str] = []

    # Live handoffs: state/handoffs/*.md
    #
    # NOTE: uses iterdir(), NOT glob("*.md") — Path.glob()'s selector silently
    # swallows PermissionError while walking (verified: unreadable dir → glob()
    # yields an empty iterator, no exception), which made the previous
    # `except OSError` here dead code for the exact permission-denied case it
    # was meant to guard.  iterdir() raises OSError as expected.
    state_dir = worktree_root / "state" / "handoffs"
    if state_dir.is_dir():
        try:
            entries = list(state_dir.iterdir())
        except OSError as exc:
            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            _LOG.warning(
                "roadmap_dag: cannot scan live handoff dir %s — %s; skipping "
                "(subtree dropped — DAG will be missing any handoffs under it)",
                state_dir,
                exc,
            )
            scan_errors.append(f"{state_dir}: {exc}")
            # --- end Tier 2 ---
        else:
            for p in entries:
                if p.suffix == ".md" and p.is_file():
                    paths.append(p.resolve())

    # Archived handoffs: archive/handoffs/**/*.md (includes month-foldered subdirs)
    #
    # NOTE: uses os.walk(onerror=...), NOT rglob("*.md") — same glob-swallows-
    # PermissionError issue as above, and os.walk's onerror hook is the
    # standard way to observe (rather than silently skip) an unreadable
    # directory encountered mid-recursion.
    archive_dir = worktree_root / "archive" / "handoffs"
    if archive_dir.is_dir():
        walk_errors: List[OSError] = []
        for dirpath, _dirnames, filenames in os.walk(
            archive_dir, onerror=walk_errors.append
        ):
            for fn in filenames:
                if fn.endswith(".md"):
                    p = Path(dirpath) / fn
                    if p.is_file():
                        paths.append(p.resolve())
        if walk_errors:
            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            for exc in walk_errors:
                _LOG.warning(
                    "roadmap_dag: cannot scan archived handoff dir %s — %s; "
                    "skipping (subtree dropped — DAG will be missing any "
                    "handoffs under it)",
                    getattr(exc, "filename", archive_dir),
                    exc,
                )
                scan_errors.append(f"{getattr(exc, 'filename', archive_dir)}: {exc}")
            # --- end Tier 2 ---

    return paths, scan_errors


# ---------------------------------------------------------------------------
# Array coercion helper
# ---------------------------------------------------------------------------


def _ensure_list(value: Any) -> List[str]:
    """Coerce a frontmatter value for blocks/blocked_by to a list of strings.

    _parse_frontmatter correctly parses YAML array fields (inline [a, b] and
    block-list forms) as Python lists.  This helper normalises the result and
    guards defensively against the non-list case (which should not occur when
    _parse_frontmatter is used correctly, but would silently corrupt the DAG
    if _simple_yaml_load were mistakenly used instead).

    None / absent → [] (treated as empty array per spec graceful-absent rule).
    list           → items cast to str, None items dropped.
    anything else  → logged warning, treated as [] (corruption guard).
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    # Non-list value indicates a misuse of a scalar-only parser (e.g. _simple_yaml_load).
    _LOG.warning(
        "roadmap_dag: expected list for blocks/blocked_by field, got %r (%s) "
        "— treating as empty array (guard against scalar-parser misuse)",
        value,
        type(value).__name__,
    )
    return []


# ---------------------------------------------------------------------------
# Single-initiative scalars — C4: roll_up + critical_path
# ---------------------------------------------------------------------------


def _compute_roll_up(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute roll-up counts by deployment_state status and pct_shipped.

    Zero-node guard (F3 — Patrik review): when total == 0, pct_shipped is None
    (serialised as JSON null) to avoid ZeroDivisionError.

    Args:
        nodes: List of node dicts from the assembler pass (each with 'status').

    Returns:
        {
          "total":      int,
          "by_status":  {status_str: count, ...},
          "pct_shipped": float (0.0–100.0) | None,
        }
        pct_shipped is the percentage of nodes whose status == "shipped".
    """
    total = len(nodes)
    if total == 0:
        return {"total": 0, "by_status": {}, "pct_shipped": None}

    by_status: Dict[str, int] = {}
    for node in nodes:
        status = str(node.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1

    shipped_count = by_status.get("shipped", 0)
    pct_shipped = round(shipped_count / total * 100, 1)

    return {
        "total": total,
        "by_status": by_status,
        "pct_shipped": pct_shipped,
    }


def _compute_critical_path(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
) -> List[str]:
    """Compute the unweighted longest-chain (by node count) in the blocks DAG.

    "Unweighted longest-chain" means: the longest path by number of nodes,
    where each node has equal weight 1.  This is NOT a duration-weighted CPM
    critical path — no per-stub duration estimates are available.  It is a
    reasonable proxy for dependency depth (F7 — Patrik review naming precision).

    Cycle guard: Kahn's BFS topological sort identifies cyclic nodes (any node
    that Kahn's cannot process retains non-zero in-degree, indicating it
    participates in a cycle).  Cyclic nodes are quarantined: a WARNING is logged
    naming them, and they are excluded from the longest-path DP.  The traversal
    terminates without an infinite loop regardless of cycle shape or size.

    Args:
        nodes: List of node dicts (each with 'stub_id').
        edges: List of edge dicts (each with 'from', 'to', type='blocks').
               C3 guarantees dangling edges have already been removed — both
               endpoints of every edge are present in the node set.

    Returns:
        List of stub_ids forming the longest chain, earliest dependency first
        (e.g. ["strang-01", "strang-02", "strang-03"]).  Empty list when
        nodes is empty or all nodes are cyclic.
    """
    if not nodes:
        return []

    stub_ids: Set[str] = {n["stub_id"] for n in nodes}

    # Build forward adjacency list and in-degree map for Kahn's algorithm.
    # Edge {from: A, to: B, type: "blocks"} means A → B in the DAG
    # (A must complete before B can start).
    children: Dict[str, List[str]] = {sid: [] for sid in stub_ids}
    in_degree: Dict[str, int] = {sid: 0 for sid in stub_ids}

    for edge in edges:
        frm = edge["from"]
        to_ = edge["to"]
        # C3 drops dangling edges, but guard defensively
        if frm in stub_ids and to_ in stub_ids:
            children[frm].append(to_)
            in_degree[to_] += 1

    # Kahn's BFS topological sort.
    # Nodes not processed = still have non-zero in-degree = cyclic.
    #
    # Both the initial queue seed and each round's newly-ready nodes are sorted
    # (not taken in raw `stub_ids`/dict iteration order) — `stub_ids` is a
    # `Set[str]`, and CPython's set/dict iteration order for str keys depends
    # on the process's string-hash seed (PYTHONHASHSEED, randomized by default
    # every process). Left unsorted, a tie in dist[] below (multiple
    # equal-length longest chains — common: this repo has real ties on both
    # its live roadmaps) would have its winner decided by a per-process hash
    # seed rather than by anything about the DAG, so two back-to-back
    # `emit()` runs over an UNCHANGED corpus could each report a different
    # critical_path. Sorting collapses that to a single deterministic
    # tie-break (lexicographically-first stub_id among equal-length chains).
    remaining = dict(in_degree)
    queue: deque[str] = deque(sorted(sid for sid in stub_ids if remaining[sid] == 0))
    topo_order: List[str] = []

    while queue:
        node = queue.popleft()
        topo_order.append(node)
        newly_ready: List[str] = []
        for child in children[node]:
            remaining[child] -= 1
            if remaining[child] == 0:
                newly_ready.append(child)
        for child in sorted(newly_ready):
            queue.append(child)

    # Identify and quarantine cyclic nodes
    cyclic_nodes = stub_ids - set(topo_order)
    if cyclic_nodes:
        _LOG.warning(
            "roadmap_dag: cycle detected — stub_id(s) %s participate in a circular "
            "blocks/blocked_by reference chain; quarantined from critical_path "
            "computation (malformed stub frontmatter — check blocks arrays for "
            "circular references)",
            sorted(cyclic_nodes),
        )

    if not topo_order:
        return []

    # DP longest-path over topological order.
    # dist[node] = number of nodes in the longest path ending at this node.
    # predecessor[node] = the node immediately before it in that path.
    dist: Dict[str, int] = {sid: 1 for sid in topo_order}
    predecessor: Dict[str, Optional[str]] = {sid: None for sid in topo_order}

    for node in topo_order:
        for child in children[node]:
            if child in dist:  # child is non-cyclic (in topo_order)
                candidate = dist[node] + 1
                if candidate > dist[child]:
                    dist[child] = candidate
                    predecessor[child] = node

    # End node = node with the greatest distance value. Explicit lexicographic
    # secondary key so a tie (multiple equal-length longest chains — real on
    # this repo's own roadmaps) resolves the same way regardless of topo_order
    # internals, on top of (not instead of) the sorted-queue determinism fix
    # above — belt-and-suspenders against reintroducing hash-seed sensitivity.
    end_node = max(topo_order, key=lambda sid: (dist[sid], sid))

    # Reconstruct path by walking predecessors back to the root
    path: List[str] = []
    current: Optional[str] = end_node
    while current is not None:
        path.append(current)
        current = predecessor[current]

    path.reverse()
    return path


# ---------------------------------------------------------------------------
# Core assembler — nodes + edges (C3) + roll_up + critical_path (C4)
# ---------------------------------------------------------------------------


def assemble_roadmap_dag(roadmap_id: str, worktree_root: Path) -> Dict[str, Any]:
    """Build per-roadmap DAG from live and archived stub handoff frontmatter.

    Scans state/handoffs/*.md and archive/handoffs/**/*.md, filters stubs whose
    frontmatter roadmap_id matches the requested roadmap_id, and assembles:

    nodes — [{stub_id, status, sprint, wave, shipped_sha, roadmap_id}]
      status      = deployment_state (raw frontmatter value, not normalized)
      shipped_sha = shipped_in value as-is (bare SHA scalar); no git verification
                    on the live serve path (F1 — _stamp_shipped_sha is reserved
                    for the gated C7 emit path only)
      roadmap_id  = explicit on each node for fleet store identity (F6)

    edges — [{from, to, type: "blocks", roadmap_id}]
      One direction stored (blocks); blocked_by is the derived reverse per rag's
      edge-table pattern.  roadmap_id explicit on each edge (F6).

    roll_up — {total, by_status: {status: count}, pct_shipped: float | null}
      pct_shipped is null (None) when total == 0 (F3 zero-node guard).

    critical_path — list of stub_ids forming the longest blocks-chain by node count.
      Unweighted longest-chain — NOT a duration-weighted CPM path; no per-stub
      duration estimates available (F7).  Empty list when total == 0 (F3).
      Cycle guard: cyclic nodes are quarantined with a logged warning and excluded
      from the traversal (no infinite loop).

    Graceful-absent handling:
      - Stubs with no roadmap_id field are silently skipped (not roadmap stubs).
      - Stubs with roadmap_id matching but missing stub_id are quarantined with
        a logged warning and skipped.
      - Absent blocks/blocked_by keys are treated as empty arrays.

    Dangling-edge handling (F4 — Patrik review):
      - A blocks entry naming a stub_id not present in the node set is a dangling
        edge.  It is dropped with a logged warning.  No phantom node is emitted.
        The returned edge list contains only edges whose both endpoints are in
        the node set — safe for the critical_path traversal.

    Zero-node guard (F3 — Patrik review):
      If no stubs match roadmap_id, returns a well-formed empty payload:
      {nodes: [], edges: [], roll_up: {total:0, by_status:{}, pct_shipped:null},
       critical_path: []}.  A live serve call for an unknown roadmap_id must not 500.

    Args:
        roadmap_id:    Roadmap identifier to filter stubs by (string).
        worktree_root: Main worktree root Path.  Must be the result of
                       main_worktree_root(common_dir) — do NOT pass a raw
                       common_dir / .git path (lesson:
                       common-dir-keyed-ops-must-derive-the-wor).

    Returns:
        dict with keys:
          'nodes'          — list of node dicts
          'edges'          — list of edge dicts
          'roll_up'        — {total, by_status, pct_shipped}
          'critical_path'  — list of stub_ids (longest-chain)
          'scan_incomplete' — bool (Tier 2). True when a live- or archived-handoff
                              subtree could not be scanned (see _collect_stub_paths).
                              Callers MUST treat True as "this DAG may be missing
                              nodes/edges", not as "these are genuinely all the
                              handoffs" — an empty/partial result under a scan
                              failure is not equivalent to a clean empty roadmap.
          'scan_errors'     — list[str] (Tier 2). One entry per unscannable subtree.
    """
    stub_paths, scan_errors = _collect_stub_paths(worktree_root)

    # -----------------------------------------------------------------------
    # Pass 1: collect nodes matching roadmap_id
    # Also cache frontmatter for the edge-building pass (avoids double file read).
    # -----------------------------------------------------------------------
    nodes: List[Dict[str, Any]] = []
    stub_id_set: Set[str] = set()
    # stub_id → frontmatter dict for edge-building pass
    stub_fm_map: Dict[str, Dict[str, Any]] = {}

    for path in stub_paths:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _LOG.warning("roadmap_dag: cannot read %s — %s; skipping", path, exc)
            continue

        fm = _parse_frontmatter(content)
        if not fm:
            continue

        # Filter by roadmap_id — stubs without roadmap_id are silently skipped
        # (they are not roadmap stubs; no warning warranted for absence)
        stub_roadmap_id = fm.get("roadmap_id")
        if not stub_roadmap_id:
            continue
        if str(stub_roadmap_id) != str(roadmap_id):
            continue

        # Validate stub_id — required for node identity
        raw_stub_id = fm.get("stub_id")
        if not raw_stub_id:
            _LOG.warning(
                "roadmap_dag: stub at %s matches roadmap_id=%r but has no stub_id "
                "— quarantined and skipped (improvement-queue: "
                "roadmap-planning-emits-handoffs-that-fai)",
                path,
                roadmap_id,
            )
            continue

        stub_id = str(raw_stub_id)

        # Deduplicate — keep first occurrence (live stubs are appended before archived; live wins)
        # Review: code-reviewer — "most recently scanned" was backwards; live stubs are appended first by _collect_stub_paths
        if stub_id in stub_id_set:
            _LOG.warning(
                "roadmap_dag: duplicate stub_id=%r found at %s "
                "— keeping first occurrence, skipping duplicate",
                stub_id,
                path,
            )
            continue

        stub_id_set.add(stub_id)
        stub_fm_map[stub_id] = fm

        nodes.append(
            {
                "stub_id": stub_id,
                "status": fm.get("deployment_state"),
                "sprint": fm.get("sprint"),
                "wave": fm.get("wave"),
                # shipped_in is a bare SHA scalar in frontmatter; carry through as-is,
                # coerced to str — an all-digit short SHA parses as int via the YAML
                # scalar cascade (dag.py _parse_scalar), and the contract
                # (roadmap_dag_node.shipped_sha) declares str | None.
                # Do NOT call _stamp_shipped_sha on the live serve path (F1).
                "shipped_sha": str(fm.get("shipped_in")) if fm.get("shipped_in") is not None else None,
                "roadmap_id": roadmap_id,  # explicit identity (F6)
            }
        )

    # -----------------------------------------------------------------------
    # Pass 2: build edges from cached frontmatter, dropping dangling edges
    # Only edges whose both endpoints are in stub_id_set are emitted — this
    # ensures the C4 critical_path traversal operates on a consistent edge set.
    # -----------------------------------------------------------------------
    edges: List[Dict[str, Any]] = []

    for stub_id, fm in stub_fm_map.items():
        raw_blocks = fm.get("blocks")
        blocks = _ensure_list(raw_blocks)

        for target_stub_id in blocks:
            target_stub_id = target_stub_id.strip()
            if not target_stub_id:
                continue

            if target_stub_id not in stub_id_set:
                # Dangling edge — planned-only stub (not yet materialized as a handoff)
                # or a typo in the blocks array.  Drop with a warning (F4).
                _LOG.warning(
                    "roadmap_dag: stub_id=%r has blocks entry %r which is not present "
                    "in the materialized node set (planned-only stub or typo) "
                    "— dangling edge dropped",
                    stub_id,
                    target_stub_id,
                )
                continue

            edges.append(
                {
                    "from": stub_id,
                    "to": target_stub_id,
                    "type": "blocks",
                    "roadmap_id": roadmap_id,  # explicit identity (F6)
                }
            )

    roll_up = _compute_roll_up(nodes)
    critical_path = _compute_critical_path(nodes, edges)

    return {
        "nodes": nodes,
        "edges": edges,
        "roll_up": roll_up,
        "critical_path": critical_path,
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        "scan_incomplete": len(scan_errors) > 0,
        "scan_errors": scan_errors,
        # --- end Tier 2 ---
    }
