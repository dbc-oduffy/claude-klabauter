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
from coordinator_core.state_root import StateRootError, coordinator_state_root

__all__ = ["resolve_priority", "load_priority_ledger", "PriorityResolveCache"]

_EDGE_KINDS: Set[str] = {"predecessor", "additional_predecessors"}
_NONE_SENTINEL = "none"
_MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")


# ---------------------------------------------------------------------------
# Ledger loading — live + archive union, keyed by target_id (filename stem).
# ---------------------------------------------------------------------------


def _collect_ledger_dir(dir_path: str) -> Dict[str, dict]:
    """Parse every ``*.yaml`` file directly under *dir_path* into a
    ``target_id -> entry`` map. ``target_id`` is the filename stem — the
    ledger schema's filename-as-identity invariant (there is no separate
    ``id`` field on the entry itself). Missing/unreadable directories yield
    ``{}``; a single unparsable file is skipped (best-effort), not fatal.
    """
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
    ``repos.project_makima`` not configured on this machine, or the
    sandboxed-test condition of a monkeypatched HOME/COORDINATOR_SETTINGS_HOME)
    is likewise treated as "no ledger entries", not a fatal condition: the
    ledger is new, most machines have none yet, and a machine without central
    resolution configured must still be able to emit. Yields ``{}`` and emits
    a ``UserWarning`` (rather than staying silent) so a genuinely
    misconfigured machine remains observable — mirrors the fallback-warning
    idiom in ``ops/emit/envelope.py``'s ``resolve_coordinator_root``. Only
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
                "empty. This usually means repos.project_makima is unset in the "
                "machine-local registry, or a partially-installed machine — verify "
                "`machine-local get repos.project_makima` resolves before trusting "
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

    # Live entries applied last so they win on a target_id collision.
    merged.update(_collect_ledger_dir(live_dir))

    return merged


# ---------------------------------------------------------------------------
# Node identity — pluggable, defaults to a best-effort (repo, basename) mirror
# of the handoff_id derivation authored at emit time (C4). A caller with its
# own authoritative derivation (e.g. C4's exact wire logic) MUST pass its own
# node_id_fn so ledger lookups key on the SAME target_id it emits — this
# default exists so the resolver is usable standalone / in tests, not as a
# second source of truth for handoff_id shape.
# ---------------------------------------------------------------------------


def _default_node_id(meta: dict, node_path: str, repo_root: Optional[str]) -> Optional[str]:
    authored = meta.get("handoff_id")
    if authored:
        return str(authored)
    basename = os.path.basename(node_path)
    if repo_root:
        repo_name = os.path.basename(os.path.normpath(repo_root))
        if repo_name:
            return f"{repo_name}:{basename}"
    return basename


NodeIdFn = Callable[[dict, str], Optional[str]]


# ---------------------------------------------------------------------------
# Predecessor-spine parent map — reuses dag.handoff_edges / dag.resolve_target
# (the same primitives walk_forward itself uses internally) to recover, for
# each node walk_forward already discovered, its immediate predecessor +
# additional_predecessors targets. This is NOT a second traversal: it only
# ever looks at nodes walk_forward already visited, replaying the same
# edge-kind field reads walk_forward used to discover them, so fan-in
# (multiple direct parents) can be detected at each node — a flat
# {path: frontmatter} map alone cannot distinguish "single ancestor" from
# "converging ancestors that must agree".
# ---------------------------------------------------------------------------


def _build_parent_map(
    nodes: Dict[str, dict],
    handoff_dir: str,
    repo_root: str,
    git_history_cache: Optional[Set[str]] = None,  # unused when include_history_tier=False below; kept for signature parity with dag.resolve_target
) -> Dict[str, List[str]]:
    """Mirrors ``dag.walk_forward``'s own internal edge-resolution call shape
    exactly: a single, fixed *handoff_dir* for every ``resolve_target`` call
    regardless of which node is currently being expanded (walk_forward never
    re-derives ``handoff_dir`` per visited node either — see its source,
    ``resolve_target(raw_ref, handoff_dir, repo_root, ...)`` inside the DFS
    loop). ``resolve_target``'s own candidate list already tries
    repo_root-anchored ``state/handoffs`` / ``archive/handoffs`` fallbacks,
    so an ancestor living in a different directory than the start node still
    resolves correctly. Using a per-node directory here instead would
    silently diverge from what ``walk_forward`` itself used to discover
    these very nodes.

    Every ``resolve_target`` call below passes ``include_history_tier=False``
    — this function's loop discards the ``'git-history'`` sentinel
    identically to ``None`` (see the ``if target and target != "git-history"``
    check), so tier 3 (the ``git log`` subprocess fallback) has never
    produced a distinguishable outcome for any caller of THIS function.
    Skipping it removes the subprocess spawn per unresolved edge target
    entirely rather than merely caching it. *git_history_cache* is still
    accepted and threaded through for signature parity with
    ``dag.resolve_target`` and any other internal caller that reuses this
    same *nodes*/*handoff_dir*/*repo_root* shape, but with tier 3 skipped it
    is inert here — never consulted, never populated on a miss.
    """
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
    """Ledger ``priority`` field, with the ``"none"`` explicit-clear sentinel
    normalized to ``None`` (no priority value, but the entry itself still
    counts as "explicit" to the caller — see module docstring).
    """
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
    """Returns ``(value, source_target_id, ambiguous)`` for the nearest
    explicit ledger entry reachable from *path* (itself included) by walking
    ONLY predecessor/additional_predecessors edges.

    ``source_target_id is not None`` means "an explicit entry was found
    somewhere on this branch" — true even when its priority VALUE is
    ``None`` (the clear sentinel); that distinguishes "found an explicit
    none" from "found nothing at all" for the caller's fan-in comparison.
    """
    if path in memo:
        return memo[path]
    if path in in_progress:
        # Defensive cycle guard — dag.walk_forward already reports
        # terminatedEarly='lineage-cycle' for a genuine back-edge and does
        # not re-push a gray node, so this branch is not expected to fire
        # in practice; treat a re-entrant hit as "nothing found" rather
        # than recursing forever.
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


# ---------------------------------------------------------------------------
# PriorityResolveCache — per-emit-run cache, SHARED across many resolve_priority()
# calls against the same repo corpus (C6b perf hoist).
#
# Problem this replaces: called once per handoff (e.g. 360x for a real corpus),
# resolve_priority() used to pay for a full dag.walk_forward() DFS PLUS a
# dag.build_handoff_id_index() corpus scan+parse PLUS a _build_parent_map()
# build on every single call, even though all three are invariant for the
# whole emit run — walk_forward's own docstring names build_handoff_id_index
# as something to "build once per scan set", and it was instead being rebuilt
# once per handoff over the identical directory scan (see the dispatch brief's
# profile: build_handoff_id_index at 12.1s / 360 calls, _build_parent_map at
# 22.6s / 360 calls, walk_forward at 15.7s / 360 calls, of a 38.4s aggregate).
#
# NEGATIVE-SPEC — why bypassing walk_forward() entirely (when a cache is
# given) is byte-identical, not merely faster, STRUCTURALLY (not by corpus
# agreement — a byte-diff over one corpus's records is confirmatory evidence,
# never the argument itself; a diff can only fail to show a divergence that
# happens not to be exercised by the sample under test):
#   1. _build_parent_map's own resolve_target() call (its per-node parent
#      lookup) has NEVER been passed id_index — not in the pre-cache
#      walk_forward-based path, not here (see _build_parent_map above: no
#      id_index kwarg at its resolve_target() call site, in either version
#      of this file).
#   2. _nearest_explicit walks ancestors EXCLUSIVELY via parent_map edges
#      (`parent_map.get(path, [])`) — it never consults `nodes` for
#      reachability, only for a found node's own meta (ledger key
#      derivation). `nodes` (whether walk_forward's DFS-limited set or this
#      cache's full-corpus set) therefore cannot change WHICH ancestors are
#      visited, only what's available to look up once an ancestor is
#      already reachable through parent_map.
#   Therefore (1) + (2): walk_forward's id-index-aware DFS only ever
#   affected which nodes got recorded into `nodes` — a lookup table, not the
#   traversal itself — never which ancestors are REACHABLE via parent_map
#   edges, because that reachability is fully re-derived from each path's
#   own frontmatter (handoff_edges + a non-id-index-aware resolve_target)
#   independent of whatever `nodes` dict happens to be sitting nearby. An
#   id-shaped predecessor_id/origin_handoff_id ref was already unreachable
#   through parent_map before this cache existed; this cache does not change
#   that (preserved on purpose, per the dispatch brief's "do not fix it
#   while you are in there" instruction) — it just stops paying to build an
#   id_index nothing downstream ever consults. Guarded by
#   test_priority_resolve_cache.py::test_id_shaped_predecessor_ref_cached_and_uncached_agree.
#   3. _build_parent_map computes parent_map[path] from (path, meta,
#      handoff_dir, repo_root) alone — it does not depend on what ELSE is in
#      its input `nodes` dict. A parent_map built over the FULL on-disk corpus
#      therefore has, for every path a per-call walk_forward()+_build_parent_map()
#      pair would have produced an entry for, the IDENTICAL value — it is
#      simply also computed for extra paths nothing will ever look up.
#   4. The corpus-wide meta map (`nodes()`) is every handoff's real frontmatter
#      via the SAME content-hash-cached read (dag.read_handoff_meta ==
#      dag._read_meta) walk_forward itself used — a strict superset of what
#      walk_forward's DFS would have visited, so a `nodes.get(path, {})` miss
#      that the old code could theoretically hit (falling back to `{}`) cannot
#      happen here; the value returned is the same either way whenever both
#      paths agree, and this path never returns a WORSE (emptier) answer.
#
# Cache scope is per-run, NOT process-lifetime (dispatch brief's explicit
# constraint) — construct one instance per emit() invocation and let it be
# garbage-collected at the end; never stash an instance on a module global.
# dag.py's own process-lifetime caches (_FRONTMATTER_CACHE, _EVER_TRACKED_CACHE)
# are a separate, lower layer this cache sits above and does not replace —
# see dag.py's _EVER_TRACKED_CACHE comment block for THEIR invalidation
# contract, which this class has no bearing on.
#
# Keyed per handoff_dir, not globally, because _build_parent_map's own
# docstring establishes edge resolution is fixed to a single handoff_dir per
# walk (mirroring walk_forward's own fixed-handoff_dir call shape) — a live
# handoff (handoff_dir = state/handoffs) and a month-archived one
# (handoff_dir = archive/handoffs/YYYY-MM) are NOT interchangeable and each
# get their own parent-map build the first time that handoff_dir is seen.
# ---------------------------------------------------------------------------


class PriorityResolveCache:
    """Per-emit-run cache for ``resolve_priority()`` — see the module comment
    block immediately above for the full correctness rationale. Construct one
    instance per emit run (e.g. once at the top of
    ``sections/handoffs.py::collect()``) and pass it to every
    ``resolve_priority(..., cache=...)`` call in that run; never share an
    instance across runs.
    """

    def __init__(self, repo_root: str):
        self.repo_root = repo_root
        self._corpus_paths: List[str] = scan_repo_handoff_corpus(repo_root)
        self._corpus_nodes: Dict[str, dict] = {
            p: read_handoff_meta(p) for p in self._corpus_paths
        }
        self._parent_maps: Dict[str, Dict[str, List[str]]] = {}
        # Single upfront `git log --all --name-only` pass (dag.build_git_history_cache),
        # in place of a per-unresolved-edge `git log --all -- <path>` subprocess spawn
        # inside every _build_parent_map() tier-3 fallback below. Best-effort: a None
        # cache (git failure, not a repo, timeout) falls back unchanged to dag.py's
        # own per-call resolution. Stripped via `dag.as_history_membership_set` —
        # this cache is built ONCE in this constructor and reused for the REST of
        # one emit run (per-emit-run cache, per this class's own docstring), so a
        # target pruned/committed after the snapshot was taken must still resolve
        # correctly later in the same run; trusting `.complete` across that reuse
        # window would fast-reject such a miss instead of falling through to the
        # real per-call git check. See `dag.as_history_membership_set`'s docstring
        # for the full rationale — a HIT is unaffected either way, only a MISS
        # changes, and only in the falls-through direction.
        self._git_history_cache: Optional[Set[str]] = as_history_membership_set(
            build_git_history_cache(repo_root)
        )

    def nodes(self) -> Dict[str, dict]:
        """The full corpus's ``{abs_path: frontmatter}`` map, built once."""
        return self._corpus_nodes

    def parent_map_for(self, handoff_dir: str) -> Dict[str, List[str]]:
        """The corpus-wide predecessor-spine parent map for *handoff_dir*,
        built (and cached) the first time this handoff_dir is requested.
        """
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


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


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
    """Resolve N's ``effective_priority`` per the four-step algorithm (module
    docstring). ``start_path`` is N's on-disk path (used to walk the
    predecessor spine via ``dag.walk_forward``); ``start_target_id`` is N's
    own ledger key.

    *ledger_entries*, when given, is used verbatim instead of loading via
    ``load_priority_ledger()`` — the test-injection seam. *node_id_fn*, when
    given, overrides the default (repo, basename)-mirroring id derivation
    (see ``_default_node_id``) — pass the SAME derivation the caller used to
    key *start_target_id* itself, so ancestor lookups are consistent.

    *cache*, when given, is a ``PriorityResolveCache`` built ONCE for the
    whole emit run (see its docstring for the correctness argument) — its
    corpus-wide meta map and per-handoff_dir parent map are used in place of
    a fresh ``dag.walk_forward()`` + ``_build_parent_map()`` pair, which is
    what still runs when *cache* is omitted (the pre-C6b behaviour, unchanged,
    still the default for any caller not opting in). *cache.repo_root* MUST
    match the effective *repo_root* for this call — a mismatch raises
    ``ValueError`` rather than silently resolving against the wrong corpus.

    Returns ``{"effective_priority": str | None, "origin": "explicit" |
    "inherited" | "suggested" | "ambiguous" | "none", "source_id": str | None}``.
    ``source_id`` is the ledger ``target_id`` the value was sourced from
    (``start_target_id`` for "explicit", the ancestor's target_id for
    "inherited", ``None`` otherwise).
    """
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
        node_id_fn = lambda meta, path: _default_node_id(  # noqa: E731
            meta, path, resolved_repo_root
        )

    if ledger_entries is None:
        ledger_entries = load_priority_ledger()

    # Step 1 — explicit entry on N itself.
    own_entry = ledger_entries.get(start_target_id)
    if own_entry is not None:
        return {
            "effective_priority": _priority_value(own_entry),
            "origin": "explicit",
            "source_id": start_target_id,
        }

    # Step 2 — nearest explicit ancestor over the predecessor spine, across
    # ALL of N's direct parents (predecessor + additional_predecessors);
    # differing results across parents is fan-in ambiguity.
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

    # Step 3 — suggested_priority on N itself.
    start_meta = nodes.get(abs_start, {})
    suggested = start_meta.get("suggested_priority")
    if suggested:
        return {"effective_priority": str(suggested), "origin": "suggested", "source_id": None}

    # Step 4 — nothing found anywhere.
    return {"effective_priority": None, "origin": "none", "source_id": None}
