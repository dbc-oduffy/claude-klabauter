"""
coordinator_core.ops.emit.priority_resolve — nearest-explicit-ancestor priority
resolution over the predecessor spine.

Purpose: given a node N (a handoff, identified by its ledger ``target_id`` and
its on-disk path), resolve N's ``effective_priority`` by walking the
predecessor chain. Sited beside ``deliverable_status.py`` (ops/emit/) as a
plain emit-time derivation — no ``register_op``, no registration quad
(verified: neither ``deliverable_status.py`` nor ``enrich.py`` carries one).
Not a dispatchable JSON-RPC op; it must not become one — that would drag in
an IPC surface with no external caller.

THE RATIFIED RESOLUTION ALGORITHM — for node N, ``effective_priority`` is the
FIRST NON-NULL of:
  1. an explicit ledger entry on N                          -> origin: "explicit"
  2. walking ``predecessor`` (+ ``additional_predecessors``) upward, the
     FIRST ancestor with an explicit entry                  -> origin: "inherited"
     halting unconditionally at ``predecessor: none`` / null
  3. ``suggested_priority`` on N itself                      -> origin: "suggested"
  4. null                                                     -> origin: "none"
Fan-in with parents at DIFFERING priorities yields NO VALUE and
origin: "ambiguous". Detect-then-fail-loud — never silently pick one parent.

THE ACCEPTANCE ORACLE — worked example, asserted verbatim in this module's
test battery:
    A  (explicit: high)
    └── B  (explicit: low)      <- mid-chain PM override
        └── C  (no explicit call)
    C resolves to  low   (nearest explicit ancestor = B)
    C does NOT resolve to  high   (the "top of chain" reading is RETIRED from
    the spec). Any implementation that yields ``high`` for C is wrong, however
    plausible its reading of older prose.

NEGATIVE-SPEC — ``forked_from``, ``origin_session``, ``origin_handoff``,
``origin_plan_id``, ``origin_goal_id``, and ``supersedes`` are NON-EDGES for
priority resolution. They are real lineage edges (``origin_handoff`` is even
registered in ``dag.EDGE_KIND_META``) and an implementer told to "walk the
lineage DAG" will traverse them and inherit straight across a fork while
believing they rode existing structure. THE PRIORITY WALK TRAVERSES
``predecessor`` (+ ``additional_predecessors``) ONLY.

SPINOFF WALL: a spinoff has ``predecessor: none``, so step 2 halts there
STRUCTURALLY — ``dag.handoff_edges`` already drops the ``none``/``null``
sentinel when collecting raw edge-target strings (dag.py's
``EDGE_KIND_FIELD_ALIASES`` sentinel-exclusion), so a spinoff simply has no
predecessor edge to walk. This module carries NO special-case spinoff branch;
the wall falls out of the spine's shape, not a hand-authored guard.

``priority: none`` on an ancestor is the ledger's EXPLICIT-CLEAR SENTINEL
(see ``coordinator/schemas/priority-ledger.schema.json``, DoE-claude repo): a
real authored assignment that terminates the upward walk, NOT an absence. An
ancestor with ``priority: none`` IS "the nearest explicit ancestor" for step
2 — it just carries no priority VALUE (``effective_priority`` resolves to
``None`` with ``origin: "inherited"``, sourced to that ancestor), and the
walk stops there rather than falling through to a still-more-distant
ancestor or to N's own ``suggested_priority``.

Ledger reads: BOTH live and archived ledger entries at the resolved central
root (``load_priority_ledger`` below) — precedent: the review-coverage-gate
reads ``state/review-trail/`` plus ``archive/review-trail/``
(``coordinator_core.ops.list_review_trail_records``). An archived explicit
ancestor must not silently drop a live descendant's inherited priority.
Never a hardcoded ``state/priority-ledger/`` literal — always resolved
through ``coordinator_core.state_root.coordinator_state_root``.

SINGLE-IMPLEMENTATION-BY-IMPORT: this module exposes exactly ONE pure
resolution entrypoint, ``resolve_priority``. The caller (the emission path,
``ops/emit/sections/handoffs.py``) imports THIS function; it may not
hand-roll a second walk. Two implementations of this algorithm is precisely
the failure the worked example above was written to prevent. (C6,
2026-07-30: the orientation cache's own former caller,
``orientation/regenerate_cache.py``'s retired ``_emit_priorities``, is gone
— boot no longer carries a handoff-derived ``## Priorities`` view at all, so
this is the sole caller now, not one of two.)

Spec backlink: DoE-claude DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § C5.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import yaml

from coordinator_core.dag import (
    as_history_membership_set,
    build_git_history_cache,
    handoff_edges,
    read_handoff_meta,
    resolve_target,
    scan_repo_handoff_corpus,
    walk_forward,
)
from coordinator_core.ops.emit.context import resolve_repo_name
from coordinator_core.state_root import StateRootError, coordinator_state_root

__all__ = ["resolve_priority", "load_priority_ledger", "PriorityResolveCache"]

_EDGE_KINDS: Set[str] = {"predecessor", "additional_predecessors"}
_NONE_SENTINEL = "none"
_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")


def _collect_ledger_dir(dir_path: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not dir_path or not os.path.isdir(dir_path):
        return out
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return out
    for name in entries:
        if not name.endswith(".yaml"):
            continue
        full = os.path.join(dir_path, name)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "r", encoding="utf-8") as fh:
                parsed = yaml.safe_load(fh.read())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(parsed, dict):
            continue
        target_id = name[: -len(".yaml")]
        out[target_id] = parsed
    return out


def load_priority_ledger(state_root: Optional[str] = None) -> Dict[str, dict]:
    """Load the full priority-ledger — live entries UNIONED with every
    month-sharded archive entry — keyed by ``target_id``.

    *state_root*, when given, is the already-resolved central ``.../state``
    directory (test-injection seam — bypasses ``coordinator_state_root``
    entirely, mirroring the override params other emit-time readers accept
    for isolation). When omitted, resolves via
    ``coordinator_state_root(central=True)``.

    Live entries win on a target_id collision against an archived entry of
    the same id (same shape as review-trail's live-then-archive ordering
    convention) — this ledger is authored one-file-per-target, so a
    collision should not occur in practice; live-wins is the conservative
    choice if it ever does.

    An unresolvable central state root (``StateRootError`` — e.g.
    ``repos.claude_klabauter`` not configured on this machine, or the
    sandboxed-test condition of a monkeypatched HOME/COORDINATOR_SETTINGS_HOME)
    is likewise treated as "no ledger entries", not a fatal condition: the
    ledger is new, most machines have none yet, and a machine without central
    resolution configured must still be able to emit. Yields ``{}`` and emits
    a ``UserWarning`` (rather than staying silent) so a genuinely
    misconfigured machine remains observable — mirrors the fallback-warning
    idiom in ``ops/emit/resolvers.py``'s ``resolve_coordinator_root``. Only
    ``StateRootError`` (and its ``CrossCuttingStateRoot`` subclass) is caught
    here — a narrow catch, deliberately not ``Exception``, so a genuine ledger
    read fault (corruption, a malformed entry, a permissions error) still
    propagates instead of being silently reported as "no priorities".
    """
    if state_root is None:
        try:
            state_root = coordinator_state_root(central=True)
        except StateRootError as exc:
            import warnings

            warnings.warn(
                f"load_priority_ledger(): central state root unresolvable "
                f"({type(exc).__name__}: {exc}); treating the priority ledger as "
                "empty. This usually means repos.claude_klabauter is unset in the "
                "machine-local registry, or a partially-installed machine — verify "
                "`machine-local get repos.claude_klabauter` resolves before trusting "
                "an emission that omits priorities.",
                stacklevel=2,
            )
            return {}

    normalized = state_root.rstrip("/\\")
    live_dir = os.path.join(normalized, "priority-ledger")
    if os.path.basename(normalized) == "state":
        archive_base = os.path.dirname(normalized)
    else:
        archive_base = normalized
    archive_root = os.path.join(archive_base, "archive", "priority-ledger")

    merged: Dict[str, dict] = {}

    if os.path.isdir(archive_root):
        try:
            month_dirs = sorted(os.listdir(archive_root))
        except OSError:
            month_dirs = []
        for month in month_dirs:
            if not _MONTH_DIR_RE.match(month):
                continue
            merged.update(_collect_ledger_dir(os.path.join(archive_root, month)))

    merged.update(_collect_ledger_dir(live_dir))

    return merged


def _default_node_id(meta: dict, node_path: str, repo_name: Optional[str]) -> Optional[str]:
    authored = meta.get("handoff_id")
    if authored:
        return str(authored)
    basename = os.path.basename(node_path)
    if repo_name:
        return f"{repo_name}:{basename}"
    return basename


def _resolve_default_repo_name(repo_root: Optional[str]) -> Optional[str]:
    if not repo_root:
        return None
    try:
        return resolve_repo_name(Path(repo_root))
    except RuntimeError:
        return None


NodeIdFn = Callable[[dict, str], Optional[str]]


def _build_parent_map(
    nodes: Dict[str, dict],
    handoff_dir: str,
    repo_root: str,
    git_history_cache: Optional[Set[str]] = None,
) -> Dict[str, List[str]]:
    parent_map: Dict[str, List[str]] = {}
    for path, meta in nodes.items():
        raw_edges = handoff_edges(meta, _EDGE_KINDS)
        parents: List[str] = []
        for raw_ref in raw_edges:
            target = resolve_target(
                raw_ref,
                handoff_dir,
                repo_root,
                git_history_cache=git_history_cache,
                include_history_tier=False,
            )
            if target and target != "git-history":
                parents.append(target)
        parent_map[path] = parents
    return parent_map


def _priority_value(entry: dict) -> Optional[str]:
    value = entry.get("priority")
    if value == _NONE_SENTINEL:
        return None
    return value


def _nearest_explicit(
    path: str,
    nodes: Dict[str, dict],
    parent_map: Dict[str, List[str]],
    ledger: Dict[str, dict],
    node_id_fn: NodeIdFn,
    memo: Dict[str, Tuple[Optional[str], Optional[str], bool]],
    in_progress: Set[str],
) -> Tuple[Optional[str], Optional[str], bool]:
    if path in memo:
        return memo[path]
    if path in in_progress:
        return (None, None, False)

    in_progress.add(path)
    meta = nodes.get(path, {})
    node_id = node_id_fn(meta, path)
    entry = ledger.get(node_id) if node_id else None

    if entry is not None:
        result = (_priority_value(entry), node_id, False)
        in_progress.discard(path)
        memo[path] = result
        return result

    found: List[Tuple[Optional[str], str]] = []
    ambiguous = False
    for parent_path in parent_map.get(path, []):
        pv, psrc, pamb = _nearest_explicit(
            parent_path, nodes, parent_map, ledger, node_id_fn, memo, in_progress
        )
        if pamb:
            ambiguous = True
            break
        if psrc is not None:
            found.append((pv, psrc))

    in_progress.discard(path)

    if ambiguous:
        result = (None, None, True)
    elif not found:
        result = (None, None, False)
    else:
        distinct_values = {v for v, _ in found}
        if len(distinct_values) > 1:
            result = (None, None, True)
        else:
            result = (found[0][0], found[0][1], False)

    memo[path] = result
    return result


# NEGATIVE-SPEC — why bypassing walk_forward() entirely (when a cache is
# given) is byte-identical, not merely faster, STRUCTURALLY (not by corpus
#   2. _nearest_explicit walks ancestors EXCLUSIVELY via parent_map edges
#   traversal itself — never which ancestors are REACHABLE via parent_map
#      pair would have produced an entry for, the IDENTICAL value — it is
# dag.py's own process-lifetime caches (_FRONTMATTER_CACHE, _EVER_TRACKED_CACHE)
# see dag.py's _EVER_TRACKED_CACHE comment block for THEIR invalidation


class PriorityResolveCache:

    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self._corpus_paths: List[str] = scan_repo_handoff_corpus(repo_root)
        self._corpus_nodes: Dict[str, dict] = {
            p: read_handoff_meta(p) for p in self._corpus_paths
        }
        self._parent_maps: Dict[str, Dict[str, List[str]]] = {}
        self._git_history_cache: Optional[Set[str]] = as_history_membership_set(
            build_git_history_cache(repo_root)
        )

    def nodes(self) -> Dict[str, dict]:
        return self._corpus_nodes

    def parent_map_for(self, handoff_dir: str) -> Dict[str, List[str]]:
        key = os.path.normpath(handoff_dir)
        cached = self._parent_maps.get(key)
        if cached is None:
            cached = _build_parent_map(
                self._corpus_nodes,
                handoff_dir,
                self.repo_root,
                git_history_cache=self._git_history_cache,
            )
            self._parent_maps[key] = cached
        return cached


def resolve_priority(
    start_path: str,
    start_target_id: str,
    *,
    node_id_fn: Optional[NodeIdFn] = None,
    ledger_entries: Optional[Dict[str, dict]] = None,
    handoff_dir: Optional[str] = None,
    repo_root: Optional[str] = None,
    cache: Optional[PriorityResolveCache] = None,
) -> Dict[str, Any]:
    abs_start = os.path.abspath(start_path)
    resolved_handoff_dir = handoff_dir or os.path.dirname(abs_start)

    resolved_repo_root = repo_root or os.path.normpath(
        os.path.join(resolved_handoff_dir, "..", "..")
    )

    if cache is not None:
        if os.path.normpath(cache.repo_root) != os.path.normpath(resolved_repo_root):
            raise ValueError(
                "resolve_priority(): cache.repo_root "
                f"({cache.repo_root!r}) does not match this call's resolved "
                f"repo_root ({resolved_repo_root!r}) — a PriorityResolveCache "
                "is only valid for the single repo corpus it was built against."
            )
        nodes: Dict[str, dict] = cache.nodes()
        parent_map = cache.parent_map_for(resolved_handoff_dir)
    else:
        walk = walk_forward(
            abs_start,
            edge_kinds=_EDGE_KINDS,
            handoff_dir=resolved_handoff_dir,
            repo_root=repo_root,
        )
        nodes = walk["nodes"]
        parent_map = _build_parent_map(nodes, resolved_handoff_dir, resolved_repo_root)

    if node_id_fn is None:
        default_repo_name = _resolve_default_repo_name(resolved_repo_root)
        node_id_fn = lambda meta, path: _default_node_id(  # noqa: E731
            meta, path, default_repo_name
        )

    if ledger_entries is None:
        ledger_entries = load_priority_ledger()

    own_entry = ledger_entries.get(start_target_id)
    if own_entry is not None:
        return {
            "effective_priority": _priority_value(own_entry),
            "origin": "explicit",
            "source_id": start_target_id,
        }

    memo: Dict[str, Tuple[Optional[str], Optional[str], bool]] = {}
    in_progress: Set[str] = set()
    found: List[Tuple[Optional[str], str]] = []
    ambiguous = False
    for parent_path in parent_map.get(abs_start, []):
        pv, psrc, pamb = _nearest_explicit(
            parent_path, nodes, parent_map, ledger_entries, node_id_fn, memo, in_progress
        )
        if pamb:
            ambiguous = True
            break
        if psrc is not None:
            found.append((pv, psrc))

    if not ambiguous and found:
        distinct_values = {v for v, _ in found}
        if len(distinct_values) > 1:
            ambiguous = True

    if ambiguous:
        return {"effective_priority": None, "origin": "ambiguous", "source_id": None}

    if found:
        value, source_id = found[0]
        return {"effective_priority": value, "origin": "inherited", "source_id": source_id}

    start_meta = nodes.get(abs_start, {})
    suggested = start_meta.get("suggested_priority")
    if suggested:
        return {"effective_priority": str(suggested), "origin": "suggested", "source_id": None}

    return {"effective_priority": None, "origin": "none", "source_id": None}
