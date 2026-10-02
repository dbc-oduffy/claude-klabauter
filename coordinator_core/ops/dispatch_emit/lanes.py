"""Write-disjoint lane and byte-bounded part partitioner for a mise inventory.

Pure functions over chunk-table rows: no file writes, no git, no compose.
Invariants: no file is written by rows of two different lanes; every row is in
exactly one part; identical input yields identical sorted-key output; a part
is released only by closed dispositions in the master inventory.
"""

from __future__ import annotations

import heapq
import posixpath
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set

from coordinator_core.ops.dispatch_emit.inventory_mint import (
    _DEP_KIND_LIVE,
    _DEP_KIND_ROUTED_OUT,
    _raw_disposition_kind,
    _split_footprint,
    _split_id_list,
    _strip_backtick,
    parse_chunk_table,
)
from coordinator_core.ops.dispatch_emit.wave_map import (
    WaveCycleError,
    _normalize_path,
    _paths_overlap,
)

_INVENTORY_DIR = "state/mise-inventory"
_HUB = "hub"


@dataclass(frozen=True)
class LaneParams:
    lanes: int = 3
    hot_files: int = 40
    byte_cap: int = 524288
    headroom: float = 0.95


class LaneMapStaleError(ValueError):
    """The master inventory's row-id set differs from the pinned lane map."""


class PartNotReadyError(ValueError):
    """A part has a live cross-part dependency."""


class RowOverBudgetError(ValueError):
    """A single row exceeds the part byte budget."""


def lane_map_path(run_id: str) -> str:
    return f"{_INVENTORY_DIR}/{run_id}.lanes.json"


def _ancestors(path: str) -> List[str]:
    """Strict ancestors of a normalized path, matching ``wave_map._is_ancestor``."""
    if path == ".":
        return []
    out: List[str] = []
    cur = path
    while True:
        parent = posixpath.dirname(cur)
        if not parent or parent == cur:
            break
        out.append(parent)
        cur = parent
    if not path.startswith("/"):
        out.append(".")
    return out


class _UnionFind:
    def __init__(self, items: Iterable[str]) -> None:
        self._parent = {i: i for i in items}

    def find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            if rb < ra:
                ra, rb = rb, ra
            self._parent[rb] = ra


def _row_id(row: Mapping[str, str]) -> str:
    return _strip_backtick(row["id"])


def _row_plan(row: Mapping[str, str]) -> str:
    return _strip_backtick(row["spec path"])


def _row_writes(row: Mapping[str, str]) -> List[str]:
    writes, writes_under = _split_footprint(_row_id(row), row["footprint"])
    return sorted({_normalize_path(p) for p in (*writes, *writes_under)})


def _lane_id(index: int) -> str:
    """a..z, aa, ab, ...; never ``hub``."""
    while True:
        n, out = index, ""
        while True:
            out = chr(ord("a") + n % 26) + out
            n = n // 26 - 1
            if n < 0:
                break
        if out != _HUB:
            return out
        index += 1


def _lane_ids(count: int) -> List[str]:
    ids: List[str] = []
    index = 0
    while len(ids) < count:
        lane = _lane_id(index)
        index += 1
        ids.append(lane)
    return ids


def _topological(row_ids: Sequence[str], deps: Mapping[str, Set[str]]) -> List[str]:
    """Kahn order over in-set ``deps``, ties broken by row id."""
    members = set(row_ids)
    pending = {r: {d for d in deps[r] if d in members} for r in row_ids}
    dependents: Dict[str, List[str]] = defaultdict(list)
    for r, ds in pending.items():
        for d in ds:
            dependents[d].append(r)
    heap = [r for r, ds in pending.items() if not ds]
    heapq.heapify(heap)
    order: List[str] = []
    while heap:
        r = heapq.heappop(heap)
        order.append(r)
        for nxt in dependents[r]:
            pending[nxt].discard(r)
            if not pending[nxt]:
                heapq.heappush(heap, nxt)
    if len(order) != len(members):
        stuck = sorted(members - set(order))
        raise WaveCycleError(f"dependency cycle among rows: {stuck}")
    return order


def partition(
    rows: Sequence[Mapping[str, str]],
    *,
    params: LaneParams,
    row_bytes: Mapping[str, int],
    fixed_bytes: int,
    bound_plans: frozenset[str],
    run_id: str,
    source_inventory: str,
    start_sha: Optional[str],
) -> dict:
    """Lane-map dict (``mise-lane-map.schema.json``) over all rows, live and closed."""
    ids = [_row_id(r) for r in rows]
    if len(set(ids)) != len(ids):
        seen_ids: Set[str] = set()
        dup = sorted({i for i in ids if i in seen_ids or seen_ids.add(i)})
        raise ValueError(f"duplicate chunk-table row ids: {dup}")
    by_id = {_row_id(r): r for r in rows}
    writes = {i: _row_writes(by_id[i]) for i in ids}
    plan = {i: _row_plan(by_id[i]) for i in ids}
    deps = {i: {d for d in _split_id_list(by_id[i]["deps"]) if d in by_id and d != i} for i in ids}

    counts: Dict[str, int] = defaultdict(int)
    for i in ids:
        for w in writes[i]:
            counts[w] += 1
    ranked = sorted(counts, key=lambda p: (-counts[p], p))
    hot = ranked[: params.hot_files]
    hot_set = set(hot)
    hot_overlap = set(hot_set)
    for h in hot_set:
        hot_overlap.update(_ancestors(h))

    uf = _UnionFind(ids)
    for i in ids:
        for d in deps[i]:
            uf.union(i, d)
    first_of_plan: Dict[str, str] = {}
    for i in ids:
        if plan[i] in bound_plans:
            uf.union(i, first_of_plan.setdefault(plan[i], i))
    exact: Dict[str, List[str]] = defaultdict(list)
    for i in ids:
        for w in writes[i]:
            if w not in hot_set:
                exact[w].append(i)
    for p, holders in exact.items():
        for other in holders[1:]:
            uf.union(holders[0], other)
        for a in _ancestors(p):
            if a in exact:
                uf.union(holders[0], exact[a][0])

    seeds = [i for i in ids if any(w in hot_overlap for w in writes[i])]
    hub_roots = {uf.find(i) for i in seeds}
    components: Dict[str, List[str]] = defaultdict(list)
    for i in ids:
        components[uf.find(i)].append(i)

    def weight(members: Sequence[str]) -> int:
        return sum(row_bytes[m] for m in members)

    hub_rows = sorted(m for root in hub_roots for m in components[root])
    free = [sorted(members) for root, members in components.items() if root not in hub_roots]
    free.sort(key=lambda members: (-weight(members), members[0]))

    lane_rows: Dict[str, List[str]] = {}
    lane_count = max(1, params.lanes)
    names = _lane_ids(lane_count)
    buckets: List[List[str]] = [[] for _ in range(lane_count)]
    loads = [0] * lane_count
    for members in free:
        target = min(range(lane_count), key=lambda k: (loads[k], k))
        buckets[target].extend(members)
        loads[target] += weight(members)
    for name, bucket in zip(names, buckets):
        if bucket:
            lane_rows[name] = bucket
    if hub_rows:
        lane_rows[_HUB] = hub_rows

    budget = params.byte_cap * params.headroom
    lanes_out: List[dict] = []
    part_of_row: Dict[str, str] = {}
    lane_order = [n for n in names if n in lane_rows] + ([_HUB] if _HUB in lane_rows else [])
    for lane in lane_order:
        ordered = _topological(sorted(lane_rows[lane]), deps)
        chunks: List[List[str]] = []
        current: List[str] = []
        used = 0
        for r in ordered:
            cost = row_bytes[r] + fixed_bytes
            if cost > budget:
                raise RowOverBudgetError(
                    f"row {r!r}: {row_bytes[r]} row bytes + {fixed_bytes} fixed bytes exceeds "
                    f"the part budget {int(budget)} (byte_cap {params.byte_cap} x headroom "
                    f"{params.headroom})"
                )
            if current and used + row_bytes[r] + fixed_bytes > budget:
                chunks.append(current)
                current, used = [], 0
            current.append(r)
            used += row_bytes[r]
        if current:
            chunks.append(current)
        parts: List[dict] = []
        for n, chunk in enumerate(chunks, start=1):
            pid = lane if len(chunks) == 1 else f"{lane}-p{n}"
            parts.append(
                {
                    "id": pid,
                    "after": [parts[-1]["id"]] if parts else [],
                    "rows": chunk,
                    "inventory": f"{_INVENTORY_DIR}/{run_id}-{pid}.md",
                    "script": None,
                    "bytes": 0,
                }
            )
            for r in chunk:
                part_of_row[r] = pid
        lanes_out.append({"id": lane, "kind": "hub" if lane == _HUB else "component", "parts": parts})

    review: Dict[str, str] = {}
    for lane in lanes_out:
        for part in lane["parts"]:
            for r in part["rows"]:
                if plan[r] in bound_plans:
                    review.setdefault(plan[r], part["id"])

    lane_of = {r: lane for lane, members in lane_rows.items() for r in members}
    _assert_invariants(ids, writes, deps, lane_of, part_of_row)

    return {
        "schema_version": 1,
        "run_id": run_id,
        "source_inventory": source_inventory,
        "start_sha": start_sha,
        "params": {
            "lanes": params.lanes,
            "hot_files": params.hot_files,
            "byte_cap": params.byte_cap,
            "headroom": params.headroom,
        },
        "hot_files": hot,
        "lanes": lanes_out,
        "falsifier_review_part": dict(sorted(review.items())),
    }


def _assert_invariants(
    ids: Sequence[str],
    writes: Mapping[str, Sequence[str]],
    deps: Mapping[str, Set[str]],
    lane_of: Mapping[str, str],
    part_of_row: Mapping[str, str],
) -> None:
    if set(part_of_row) != set(ids):
        raise AssertionError("partition: a row is in no part")
    for r in ids:
        for d in deps[r]:
            if lane_of[d] != lane_of[r]:
                raise AssertionError(f"partition: dependency {r!r} -> {d!r} crosses lanes")
    holders: Dict[str, Set[str]] = defaultdict(set)
    for r in ids:
        for w in writes[r]:
            holders[w].add(lane_of[r])
    for p, lanes in holders.items():
        seen = set(lanes)
        for a in _ancestors(p):
            if a in holders:
                if not _paths_overlap(p, a):
                    raise AssertionError(f"partition: index/overlap disagree on {p!r}, {a!r}")
                seen |= holders[a]
        if len(seen) > 1:
            raise AssertionError(f"partition: {p!r} is written from lanes {sorted(seen)}")


def _lane_map_rows(lane_map: Mapping) -> List[str]:
    return [r for lane in lane_map["lanes"] for part in lane["parts"] for r in part["rows"]]


def _check_fresh(lane_map: Mapping, master_ids: Iterable[str]) -> None:
    pinned, current = set(_lane_map_rows(lane_map)), set(master_ids)
    if pinned == current:
        return
    raise LaneMapStaleError(
        f"master inventory row set differs from the pinned lane map: "
        f"added {sorted(current - pinned)}, removed {sorted(pinned - current)}; "
        f"delete {lane_map_path(lane_map['run_id'])} to re-partition"
    )


def _parts_by_id(lane_map: Mapping) -> Dict[str, dict]:
    return {part["id"]: part for lane in lane_map["lanes"] for part in lane["parts"]}


def _after_closure(parts: Mapping[str, dict], part_id: str) -> List[str]:
    seen: List[str] = []
    stack = list(parts[part_id]["after"])
    while stack:
        p = stack.pop()
        if p not in seen:
            seen.append(p)
            stack.extend(parts[p]["after"])
    return seen


def ready_parts(lane_map: dict, dispositions: Mapping[str, str]) -> list[str]:
    """Part ids whose ``after`` closure carries only closed dispositions."""
    _check_fresh(lane_map, dispositions)
    parts = _parts_by_id(lane_map)
    closed = {
        r: _raw_disposition_kind(r, dispositions[r]) != _DEP_KIND_LIVE
        for r in dispositions
    }
    ready: List[str] = []
    for lane in lane_map["lanes"]:
        for part in lane["parts"]:
            if all(closed[r] for p in _after_closure(parts, part["id"]) for r in parts[p]["rows"]):
                ready.append(part["id"])
    return ready


def _escape_cell(cell: str) -> str:
    return cell.replace("|", "\\|")


def render_part_inventory(master_text: str, lane_map: dict, part_id: str) -> str:
    """Sub-inventory text for one part; raises ``PartNotReadyError`` on a live cross-part dependency."""
    master = parse_chunk_table(master_text)
    by_id = {_row_id(r): r for r in master}
    _check_fresh(lane_map, by_id)
    parts = _parts_by_id(lane_map)
    if part_id not in parts:
        raise KeyError(f"lane map has no part {part_id!r}; parts: {sorted(parts)}")
    in_part = list(parts[part_id]["rows"])
    members = set(in_part)
    part_of = {r: p["id"] for p in parts.values() for r in p["rows"]}
    header = list(master[0].keys())

    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for rid in in_part:
        row = dict(by_id[rid])
        kind = _raw_disposition_kind(rid, row["disposition"])
        kept: List[str] = []
        routed_by: Optional[str] = None
        for dep in _split_id_list(row["deps"]):
            if dep in members or dep not in by_id:
                kept.append(dep)
                continue
            if kind != _DEP_KIND_LIVE:
                continue
            dep_kind = _raw_disposition_kind(dep, by_id[dep]["disposition"])
            if dep_kind == _DEP_KIND_LIVE:
                raise PartNotReadyError(
                    f"part {part_id!r} is not ready: row {rid!r} depends on live row "
                    f"{dep!r} in part {part_of[dep]!r}"
                )
            if dep_kind == _DEP_KIND_ROUTED_OUT and routed_by is None:
                routed_by = dep
        row["deps"] = ", ".join(kept) if kept else "-"
        if routed_by is not None:
            row["disposition"] = (
                f"routed-out (dep {routed_by} routed out in part {part_of[routed_by]})"
            )
        lines.append("| " + " | ".join(_escape_cell(row[h]) for h in header) + " |")

    front = [f"run_id: {lane_map['run_id']}-{part_id}"]
    if lane_map.get("start_sha"):
        front.append(f"start_sha: {lane_map['start_sha']}")
    return (
        "---\n"
        + "\n".join(front)
        + "\n---\n# Mise inventory\n\n"
        + f"Derived from {lane_map_path(lane_map['run_id'])}; do not hand-edit.\n\n"
        + "## Chunk table\n\n"
        + "\n".join(lines)
        + "\n"
    )
