"""
coordinator_core.ops.dispatch_emit.wave_map — normalized rows -> ordered waves.

Purpose: the single wave-derivation entry point for the dispatch-emit pipeline
(docs/plans/2026-08-12-emitter-turns-a-spine-into-one-workflow.md § C2). Takes
the ``EmitterRow`` list ``spine_read.read_spine`` produces and derives an
ordered list of waves — groups of rows that may dispatch in parallel — from
three constraints that are COMPUTED here, never read off the spine:

  1. Write overlap. Two rows whose declared ``writes`` sets intersect cannot
     share a wave. A row whose ``writes`` is the ``UNDECLARED`` sentinel
     (spine_read.py's AC2 distinction) cannot share a wave with ANY other
     row, including another UNDECLARED row — undeclared means unknown, not
     empty, so no overlap claim can be proven false. Overlap is
     path-containment-aware in both directions (a directory write and a
     write to a file inside it are the same surface), not exact string
     equality.
  2. Dependency. A ``depends_on`` edge forces its target row into a strictly
     later wave than its predecessor. Both declarable gate kinds
     (``output-consumption-runtime``, ``epistemic-premise``) order
     identically here — the kind is carried through onto the emitted row for
     the downstream brief-writer (C4), but never used to relax ordering.
  3. Read-after-write. A row's declared ``reads:`` intersecting another
     row's declared ``writes:`` (a real list, not UNDECLARED) forces the
     writer into a strictly earlier wave than the reader — this is what
     stops modules that import each other from landing in one parallel
     wave when write-overlap alone sees disjoint files (the plan's own
     spine is the reproducing case: spine_read.py, wave_map.py, pathspec.py
     and emit.py write disjoint files but each imports the previous one).

A cycle in the combined depends_on + read-after-write predecessor graph
(including a self-edge) is detected and raises ``WaveCycleError`` fail-loud,
naming the cycle's members — it is never silently ordered or hung on.

This module is a pure function: same rows in, same wave order out, no I/O,
no mutation of its inputs.

Negative-spec:
  - Does NOT re-derive the UNDECLARED-vs-empty distinction; it trusts
    ``spine_read``'s sentinel via identity check.
  - Does NOT reuse ``fan_out_integrator._check_overlap`` as-is — that helper
    validates a caller-supplied partition (raises on overlap); this module
    DERIVES a partition from scratch, which is a different problem shape.
    Its exact-string-equality comparison is also insufficient here (see
    point 1 above) and is not carried over.
  - Does NOT special-case ``gate_kind`` beyond carrying it through — both
    declarable kinds order identically (see module docstring above).
"""

from __future__ import annotations

import posixpath
from pathlib import PurePosixPath
from typing import NamedTuple

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow


class WaveRow(NamedTuple):
    """One emitted row within a wave, carrying its predecessor edges for C4."""

    id: str
    title: str
    surface: str
    writes: object
    reads: list
    depends_on: list


class WaveCycleError(ValueError):
    """Raised when depends_on/read-after-write edges form a cycle (finding 3).

    Covers both a self-edge (a row depending on itself, directly or via its
    declared reads/writes) and a longer cycle among two or more rows.
    """


def _normalize_path(path: str) -> PurePosixPath:
    """Normalize a declared path string for containment comparison.

    Two pure-string transforms, neither touching disk (that would violate
    the package's no-tree-survey negative spec):

      1. ``posixpath.normpath`` collapses a leading ``./``, redundant
         separators, and ``..`` segments (``dir/../other.py`` and
         ``other.py`` become the same normalized path) — string-only, no
         filesystem access.
      2. Case-fold to lowercase — the fleet's dominant dev box (macOS,
         case-insensitive HFS+/APFS by default) treats ``docs/Wiki/x.md``
         and ``docs/wiki/x.md`` as the same file on disk; without this the
         containment check would call them non-overlapping.

    Case-folding is deliberately WRONG on a case-sensitive filesystem, and
    safe anyway because its error runs one way only. On Linux ``Foo.py`` and
    ``foo.py`` are two files, so folding can declare an overlap that does not
    exist — which costs an unnecessary wave boundary and serializes two rows
    that could have run together. It can never do the reverse: fold two
    genuinely-colliding paths apart. Over-serializing is a slower plan;
    under-serializing is a corrupted tree at execution time. Do not "fix"
    this by dropping the fold without replacing it with a per-platform
    case-sensitivity probe — the fold is the conservative branch, not an
    oversight.

    Review: coordinator:code-reviewer (wsc-A, ecb99d36) — both gaps flagged
    as path-comparison cases the containment logic missed.
    """
    normalized = posixpath.normpath(path)
    return PurePosixPath(normalized.lower())


def _paths_overlap(a: str, b: str) -> bool:
    """True if declared paths ``a`` and ``b`` name the same surface.

    Path-containment-aware in both directions: exact equality, OR one path
    is a directory ancestor of the other (``docs/wiki/`` overlaps
    ``docs/wiki/dispatch-emit.md``).
    """
    path_a, path_b = _normalize_path(a), _normalize_path(b)
    if path_a == path_b:
        return True
    return path_a in path_b.parents or path_b in path_a.parents


def _writes_overlap(a: EmitterRow, b: EmitterRow) -> bool:
    """True if ``a`` and ``b`` cannot share a wave on write-overlap grounds.

    Either side UNDECLARED forces separation (AC2): undeclared is unknown,
    not empty, so it cannot be proven disjoint from anything, including
    another UNDECLARED row.
    """
    if a.writes is UNDECLARED or b.writes is UNDECLARED:
        return True
    return any(_paths_overlap(x, y) for x in a.writes for y in b.writes)


def _predecessors(rows: list[EmitterRow]) -> dict[str, set[str]]:
    """Row id -> set of row ids that must land in a strictly earlier wave.

    Combines two computed edge sources: declared ``depends_on`` edges, and
    read-after-write edges (a row's ``reads`` intersecting another row's
    declared ``writes``). Both are COMPUTED here, never hand-declared.
    """
    row_ids = {row.id for row in rows}
    preds: dict[str, set[str]] = {row.id: set() for row in rows}

    for row in rows:
        for edge in row.depends_on:
            chunk = edge.get("chunk") if isinstance(edge, dict) else None
            if chunk in row_ids:
                preds[row.id].add(chunk)

    for writer in rows:
        if writer.writes is UNDECLARED or not isinstance(writer.writes, list):
            continue
        write_set = {_normalize_path(p) for p in writer.writes}
        for reader in rows:
            if reader.id == writer.id:
                continue
            read_set = {_normalize_path(p) for p in reader.reads}
            if write_set & read_set:
                preds[reader.id].add(writer.id)

    return preds


def _detect_cycle(preds: dict[str, set[str]]) -> None:
    """Raise ``WaveCycleError`` if ``preds`` contains a cycle or self-edge."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in preds}
    path: list[str] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        path.append(node)
        # sorted(): preds[node] is a set, and Python's set iteration order
        # for str elements depends on per-process hash randomization
        # (PYTHONHASHSEED) — without a deterministic visit order, WHICH
        # predecessor is visited first (and so which cycle path/member
        # ordering ends up in the raised message) could vary run to run,
        # even though whether a cycle exists is itself deterministic.
        # Review: coordinator:code-reviewer (wsc-A, ecb99d36).
        for pred in sorted(preds[node]):
            if pred == node:
                raise WaveCycleError(f"cycle detected: row {node!r} depends on itself")
            if color.get(pred) == GRAY:
                start = path.index(pred)
                members = path[start:] + [pred]
                raise WaveCycleError(
                    "cycle detected among rows: " + " -> ".join(members)
                )
            if color.get(pred) == WHITE:
                visit(pred)
        path.pop()
        color[node] = BLACK

    for node in list(preds):
        if color[node] == WHITE:
            visit(node)


def _topological_order(rows: list[EmitterRow], preds: dict[str, set[str]]) -> list[EmitterRow]:
    """Order ``rows`` so every predecessor precedes its dependents.

    Ties (rows with no ordering relationship) preserve input order —
    Kahn's algorithm, scanning the still-pending list in its original order
    each round.

    O(n^2) (linear scan + list.remove per placement) — a deliberate small-n
    tradeoff, fine at plan-sized row counts (single/low-double digits). A
    future caller batching much larger spines would hit this silently;
    revisit with an ordered structure if that ever becomes real.
    """
    placed_ids: set[str] = set()
    ordered: list[EmitterRow] = []
    pending = list(rows)

    while pending:
        for row in pending:
            if preds[row.id] <= placed_ids:
                ordered.append(row)
                placed_ids.add(row.id)
                pending.remove(row)
                break
        else:  # pragma: no cover - _detect_cycle already rules this out
            raise WaveCycleError(
                "cycle detected among rows: " + ", ".join(row.id for row in pending)
            )

    return ordered


def build_waves(rows: list[EmitterRow]) -> list[list[WaveRow]]:
    """Derive an ordered list of waves from ``rows``.

    Each wave is a list of ``WaveRow`` objects that may dispatch in parallel.
    Raises ``WaveCycleError`` if the combined depends_on + read-after-write
    predecessor graph contains a cycle (including a self-edge).

    Wave order is deterministic for a given input: rows are placed in
    predecessor-respecting (topological) order, each into the earliest wave
    that is simultaneously (a) free of any write-overlap conflict with every
    row already placed in that wave and (b) at or past every one of the
    row's predecessor waves + 1.
    """
    preds = _predecessors(rows)
    _detect_cycle(preds)
    order = _topological_order(rows, preds)

    wave_of: dict[str, int] = {}
    waves: list[list[EmitterRow]] = []

    for row in order:
        min_wave = 0
        for pred_id in preds[row.id]:
            predecessor_wave = wave_of.get(pred_id)
            if predecessor_wave is not None:
                min_wave = max(min_wave, predecessor_wave + 1)

        candidate = min_wave
        while True:
            if candidate >= len(waves):
                waves.append([])
            if all(
                not _writes_overlap(row, placed) for placed in waves[candidate]
            ):
                waves[candidate].append(row)
                wave_of[row.id] = candidate
                break
            candidate += 1

    return [
        [
            WaveRow(
                id=row.id,
                title=row.title,
                surface=row.surface,
                writes=row.writes,
                reads=row.reads,
                depends_on=row.depends_on,
            )
            for row in wave
        ]
        for wave in waves
        if wave
    ]
