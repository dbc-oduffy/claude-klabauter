"""Spawn-tax-aware chunk shaping: a pure pre-pass that merges sub-floor sibling rows.

Every dispatched chunk pays an orientation cost before it does work (about 9% of a 200k context,
18_000 tokens). ``shape_rows`` merges a row below that floor into a peer it shares a write path
with, or is ordered against, when the merge costs no parallelism: the shaped spine's uncapped
``build_waves`` has wave_count <= and peak_width >= the unshaped one. Pure function; no I/O.

Constants were derived from the r52 executed plans (experiments ed5e3fa, 24 rows across three
plans): row body is 266..2497 tokens (len//4), median about 600; a row writes 1..8 files, median 2.
A row's work is body plus PER_WRITE_TOKENS per written file, giving a median row near 3_600 and a
largest near 14_500. WORK_CEILING is 9_000: two median rows merge (7_200), two of the largest do
not. Nearly every authored row sits below the 18_000 floor, so the ceiling is the binding bound.
"""

from __future__ import annotations

from typing import NamedTuple

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow
from coordinator_core.ops.dispatch_emit.wave_map import (
    WaveCycleError,
    _normalize_path,
    _paths_overlap,
    build_waves,
)

ORIENTATION_FRACTION = 0.09
CONTEXT_TOKENS = 200_000
ORIENTATION_TOKENS_PER_SPAWN = int(ORIENTATION_FRACTION * CONTEXT_TOKENS)

# The question this answers: how many tokens of reading, editing and re-testing does one written
# file cost an executor beyond the words of its row body.
PER_WRITE_TOKENS = 1_500

# DoE plan-author floor (60s or 30k tokens): same-wave disjoint rows below it merge unless a row
# declares disjoint_parallel. WORK_CEILING bounds the pair sum, so it binds before this floor
# for any pair; the floor is the doctrine's stated number, not the effective limit.
DISJOINT_FLOOR_TOKENS = 30_000

# Proxy for the doctrine's 15-minute executor ceiling (DoE dispatching-parallel-agents): a merged
# row above this is a mega-dispatch. Measured from r52 rows; see the module docstring.
WORK_CEILING = 9_000


class ShapeResult(NamedTuple):
    """``rows`` is the shaped spine; ``merges`` is (merged_id, constituent_ids) per merged row."""

    rows: list
    merges: list


def work_estimate(row: EmitterRow) -> int:
    """Tokens of work a row asks of its executor: body//4 plus PER_WRITE_TOKENS per write."""
    writes = row.writes if isinstance(row.writes, list) else []
    n_writes = len(writes) + len(row.writes_under or ())
    return len(row.body or "") // 4 + PER_WRITE_TOKENS * n_writes


def parallelism(rows: list) -> tuple:
    """(wave_count, peak_width) of ``build_waves(rows)``, uncapped."""
    waves = build_waves(rows)
    return len(waves), max((len(w) for w in waves), default=0)


def _dep_id(edge) -> object:
    return edge.get("chunk") if isinstance(edge, dict) else edge


def _mergeable(row: EmitterRow) -> bool:
    if row.writes is UNDECLARED or not isinstance(row.writes, list):
        return False
    if row.memo_send is not None or getattr(row, "adopts_stranded", False):
        return False
    return not any(isinstance(e, dict) and e.get("gate_kind") for e in row.depends_on)


def _writes_share(a: EmitterRow, b: EmitterRow) -> bool:
    a_paths = [*a.writes, *a.writes_under]
    b_paths = [*b.writes, *b.writes_under]
    return any(_paths_overlap(x, y) for x in a_paths for y in b_paths)


def _ordered(a: EmitterRow, b: EmitterRow) -> bool:
    return a.id in map(_dep_id, b.depends_on) or b.id in map(_dep_id, a.depends_on)


def _union(*seqs) -> list:
    out: list = []
    for seq in seqs:
        for item in seq:
            if item not in out:
                out.append(item)
    return out


def _merge_pair(a: EmitterRow, b: EmitterRow) -> EmitterRow:
    merged_id = f"{a.id}+{b.id}"
    inner = {a.id, b.id}
    writes = _union(a.writes, b.writes)
    written = {_normalize_path(p) for p in writes}
    reads = [p for p in _union(a.reads, b.reads) if _normalize_path(p) not in written]
    depends_on = [e for e in _union(a.depends_on, b.depends_on) if _dep_id(e) not in inner]
    kinds = {a.change_kind, b.change_kind}
    runs = [v for v in (a.verification_runs, b.verification_runs) if v is not None]
    return a._replace(
        id=merged_id,
        title=f"{a.title} + {b.title}",
        writes=writes,
        writes_under=tuple(_union(a.writes_under, b.writes_under)),
        reads=reads,
        depends_on=depends_on,
        reads_at_head=tuple(_union(a.reads_at_head, b.reads_at_head)),
        appends=tuple(_union(a.appends, b.appends)),
        effort=a.effort or b.effort,
        verification_runs=any(runs) if runs else None,
        change_kind=kinds.pop() if len(kinds) == 1 else None,
        body=f"## {a.id}: {a.title}\n{a.body}\n\n## {b.id}: {b.title}\n{b.body}",
    )


def _rewire(rows: list, inner: set, merged_id: str) -> list:
    """Point every surviving row's depends_on edge naming a constituent at the merged id."""
    out = []
    for row in rows:
        if not any(_dep_id(e) in inner for e in row.depends_on):
            out.append(row)
            continue
        deps: list = []
        for e in row.depends_on:
            if _dep_id(e) in inner:
                e = dict(e, chunk=merged_id) if isinstance(e, dict) else merged_id
            if e not in deps:
                deps.append(e)
        out.append(row._replace(depends_on=deps))
    return out


def _candidate(a: EmitterRow, b: EmitterRow) -> bool:
    if not (_mergeable(a) and _mergeable(b)):
        return False
    if (a.agent_type, a.agent_model) != (b.agent_type, b.agent_model):
        return False
    if min(work_estimate(a), work_estimate(b)) >= ORIENTATION_TOKENS_PER_SPAWN:
        return False
    if work_estimate(a) + work_estimate(b) > WORK_CEILING:
        return False
    return _writes_share(a, b) or _ordered(a, b)


def _disjoint_candidate(a: EmitterRow, b: EmitterRow, wave_of: dict) -> bool:
    """Same-wave, write-disjoint, unordered rows each under the DoE floor, neither opted out."""
    if not (_mergeable(a) and _mergeable(b)):
        return False
    if getattr(a, "disjoint_parallel", False) or getattr(b, "disjoint_parallel", False):
        return False
    if (a.agent_type, a.agent_model) != (b.agent_type, b.agent_model):
        return False
    if _writes_share(a, b) or _ordered(a, b):
        return False
    if a.id not in wave_of or wave_of.get(a.id) != wave_of.get(b.id):
        return False
    if max(work_estimate(a), work_estimate(b)) >= DISJOINT_FLOOR_TOKENS:
        return False
    return work_estimate(a) + work_estimate(b) <= WORK_CEILING


def _wave_index(rows: list) -> dict:
    return {r.id: n for n, wave in enumerate(build_waves(rows)) for r in wave}


def shape_rows(rows: list) -> ShapeResult:
    """Greedily merge sub-floor rows, keeping the wave count.

    Rows that share writes or are ordered merge when peak_width does not drop. Same-wave rows with
    disjoint writes, each under DISJOINT_FLOOR_TOKENS, merge too (peak_width may drop) unless one
    declares ``disjoint_parallel``. An unschedulable input is returned unshaped.
    """
    current = list(rows)
    members = {r.id: (r.id,) for r in current}
    try:
        base = parallelism(current)
    except WaveCycleError:
        return ShapeResult(current, [])
    merged_any = True
    while merged_any:
        merged_any = False
        wave_of = _wave_index(current)
        for i in range(len(current)):
            for j in range(i + 1, len(current)):
                a, b = current[i], current[j]
                disjoint = _disjoint_candidate(a, b, wave_of)
                if not (disjoint or _candidate(a, b)):
                    continue
                merged = _merge_pair(a, b)
                trial = [r for k, r in enumerate(current) if k not in (i, j)]
                trial.insert(i, merged)
                trial = _rewire(trial, {a.id, b.id}, merged.id)
                try:
                    after = parallelism(trial)
                except WaveCycleError:
                    continue
                if after[0] > base[0] or (after[1] < base[1] and not disjoint):
                    continue
                members[merged.id] = members[a.id] + members[b.id]
                current, base, merged_any = trial, after, True
                break
            if merged_any:
                break
    merges = [(r.id, members[r.id]) for r in current if len(members[r.id]) > 1]
    return ShapeResult(current, merges)
