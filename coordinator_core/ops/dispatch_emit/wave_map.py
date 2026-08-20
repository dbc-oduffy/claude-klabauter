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

## Declared-beats-derived (break-class fix, 2026-08-20)

The two edge sources are not co-equal in authority. A ``depends_on`` edge is
an explicit authoring statement of order; a read-after-write edge is
INFERRED from path sets. When they disagree — the pair carries a declared
edge one way and a derived edge the other — the derived edge is DROPPED and
a ``logging.warning`` names the drop. Only cycles that survive that
resolution raise: a genuinely circular ``depends_on`` set is still an
authoring bug worth refusing on.

The reproducing case (example-retrieval-repo-em cross-repo memo, 2026-08-20) is a spike
row that reads a file AT HEAD to reach a verdict, and a later row that
implements the verdict into that same file and gates ``epistemic-premise``
on the spike. Both legs are correct authoring; unioning them refused a
correctly-filled spine. The derived rule exists to stop two WRITERS that
import each other co-scheduling (point 3 above) — it has no standing to
overturn an author who already said which way round the order goes.

Note what this does NOT do: it does not exempt a row from derived edges
because its ``writes`` are disjoint from every other row's. The engine
cannot tell a reference-read at HEAD from a read that wants the writer's
output, and a reader with disjoint writes can legitimately need either. The
author distinguishes them by declaring the edge — which this resolution now
honours.

A cycle in the combined depends_on + read-after-write predecessor graph
(including a self-edge) is detected and raises ``WaveCycleError`` fail-loud,
naming the cycle's members and EACH LEG'S PROVENANCE (declared vs derived,
and for a derived leg the colliding path) — it is never silently ordered or hung on. This
check runs against the FULL predecessor graph, before the epistemic-premise
holdout below removes any row, so a cycle made up entirely of held-out rows
is still caught rather than reported as two indistinguishable ordinary
holds (see § Epistemic-premise holdout).

## Epistemic-premise holdout (break-class fix, 2026-08-19)

A row gated ``epistemic-premise`` on a predecessor (plan-tasks.schema.json's
``gate_kind`` enum: the predecessor "decides whether this row should exist
at all... there is no interface to pin and nothing to author against until
its verdict lands") whose ``writes`` is UNDECLARED cannot be placed in ANY
wave without either sinking the whole emit (``pathspec.commit_pathspec``'s
``NoWritesDeclaredError`` when it lands alone) or fabricating a surface it
does not have. ``build_waves`` HOLDS such a row OUT of this pass's wave
graph entirely, transitively (a row that itself depends on a held row
cannot run against a predecessor that did not run), and reports every held
row by id via ``logging.warning`` before deriving waves from what remains.

This is a scheduling decision, not a disposition mutation — a held row
carries no changed ``disposition``/``deferred``/``writes``/anything; it
simply does not appear in this call's wave output, and re-appears once a
later ``read_spine`` call sees its epistemic predecessor's verdict resolve
the gate (either the row's ``writes`` becomes declared, or the
``depends_on`` edge/row is edited away in the plan body).

The predicate is exactly two-part and BOTH parts are required:
  (a) the row carries a ``depends_on`` edge with
      ``gate_kind == "epistemic-premise"``, and
  (b) the row's own ``writes`` is the UNDECLARED sentinel (identity check,
      never truthiness — see AC2 above). ``writes: []`` is a POSITIVE
      empty declaration, not UNDECLARED, and does not hold the row out.
A row satisfying only one half keeps its current (unheld) behaviour
exactly — including a row that itself gates ``epistemic-premise`` on a
predecessor but declares real writes, which stays live and orders no
differently than an ``output-consumption-runtime`` gate would.

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
  - Does NOT special-case ``gate_kind`` beyond carrying it through and the
    one named epistemic-premise holdout above — every other gate kind
    orders identically (see module docstring above).
  - Does NOT stamp any disposition or write to the plan file for a held
    row — see § Epistemic-premise holdout above.
"""

from __future__ import annotations

import logging
import posixpath
from pathlib import PurePosixPath
from typing import NamedTuple

from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED, EmitterRow

_logger = logging.getLogger(__name__)

_EPISTEMIC_PREMISE = "epistemic-premise"


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


def _predecessors(
    rows: list[EmitterRow],
    provenance: dict[tuple[str, str], str] | None = None,
) -> dict[str, set[str]]:
    """Row id -> set of row ids that must land in a strictly earlier wave.

    Combines two computed edge sources: declared ``depends_on`` edges, and
    read-after-write edges (a row's ``reads`` intersecting another row's
    declared ``writes``). Both are COMPUTED here, never hand-declared.

    Declared outranks derived (§ Declared-beats-derived in the module
    docstring): a derived read-after-write edge is DROPPED, with a
    ``logging.warning``, when the pair already carries a declared
    ``depends_on`` edge pointing the other way.

    ``provenance``, when supplied, is filled in place with
    ``(row_id, predecessor_id) -> label`` for every edge kept, so a caller
    can name each leg's origin (``_detect_cycle``'s message). A declared
    label wins over a derived one for a pair edged both ways round the same
    direction — the author's statement is the one worth reporting.
    """
    row_ids = {row.id for row in rows}
    preds: dict[str, set[str]] = {row.id: set() for row in rows}
    declared: dict[str, set[str]] = {row.id: set() for row in rows}

    for row in rows:
        for edge in row.depends_on:
            chunk = edge.get("chunk") if isinstance(edge, dict) else None
            if chunk in row_ids:
                preds[row.id].add(chunk)
                declared[row.id].add(chunk)
                if provenance is not None:
                    gate_kind = (
                        edge.get("gate_kind") if isinstance(edge, dict) else None
                    ) or "unspecified"
                    provenance[(row.id, chunk)] = (
                        f"declared: depends_on, gate_kind={gate_kind}"
                    )

    for writer in rows:
        if writer.writes is UNDECLARED or not isinstance(writer.writes, list):
            continue
        write_paths = {_normalize_path(path): path for path in writer.writes}
        for reader in rows:
            if reader.id == writer.id:
                continue
            collisions = [
                (read_path, write_paths[normalized])
                for read_path, normalized in (
                    (path, _normalize_path(path)) for path in reader.reads
                )
                if normalized in write_paths
            ]
            if not collisions:
                continue
            if reader.id in declared[writer.id]:
                read_path, write_path = collisions[0]
                _logger.warning(
                    "wave_map: dropped derived edge %s -> %s (%s reads %s, "
                    "written by %s); %s declares depends_on %s and a declared "
                    "edge outranks a derived one",
                    reader.id,
                    writer.id,
                    reader.id,
                    read_path,
                    writer.id,
                    writer.id,
                    reader.id,
                )
                continue
            preds[reader.id].add(writer.id)
            if provenance is not None and (reader.id, writer.id) not in provenance:
                read_path, write_path = collisions[0]
                provenance[(reader.id, writer.id)] = (
                    f"derived: {reader.id} reads {read_path}, "
                    f"written by {writer.id}"
                )

    return preds


def _epistemic_premise_predecessors(row: EmitterRow) -> list[str]:
    """Row ids named by one of ``row``'s ``depends_on`` edges whose
    ``gate_kind`` is ``epistemic-premise``. Empty if none."""
    return [
        edge.get("chunk")
        for edge in row.depends_on
        if isinstance(edge, dict) and edge.get("gate_kind") == _EPISTEMIC_PREMISE
    ]


def _compute_held_out(
    rows: list[EmitterRow], preds: dict[str, set[str]]
) -> dict[str, str]:
    """Row id -> human-readable hold reason, for every row this pass excludes
    from the wave graph (§ Epistemic-premise holdout in the module
    docstring).

    Two membership routes, both computed here rather than trusted from a
    caller:

      1. Direct — the row carries an ``epistemic-premise`` depends_on edge
         AND its own ``writes`` is UNDECLARED (both halves required; see
         module docstring predicate).
      2. Transitive — the row (transitively, via ``preds``, which already
         combines depends_on and read-after-write edges) depends on a row
         already held for either reason. A row cannot run against a
         predecessor that did not run.

    ``preds`` must be derived from the FULL ``rows`` list (before any
    holdout filtering) — the transitive walk needs every edge, including
    ones pointing at rows route 1 is about to hold.
    """
    reasons: dict[str, str] = {}
    for row in rows:
        gates = _epistemic_premise_predecessors(row)
        if gates and row.writes is UNDECLARED:
            reasons[row.id] = (
                "epistemic-premise gate on "
                + ", ".join(gates)
                + ", surface not yet declared"
            )

    # Fixed-point closure: a row held this round can make another row
    # eligible next round (a chain of depends_on edges through held rows).
    # Bounded by len(rows) — each round holds at least one more row or the
    # loop stops.
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row.id in reasons:
                continue
            blockers = sorted(preds[row.id] & reasons.keys())
            if blockers:
                reasons[row.id] = "depends on held row(s) " + ", ".join(blockers)
                changed = True

    return reasons


def _report_held_out(held: dict[str, str]) -> None:
    """Log every held-out row by id and reason (never silent — see module
    docstring § Epistemic-premise holdout). ``logging.warning`` reaches the
    caller's stderr via Python's last-resort handler with no configuration
    required, so an operator sees this without reading code."""
    for row_id in sorted(held):
        _logger.warning(
            "wave_map: held %s out of this pass's wave graph (%s)",
            row_id,
            held[row_id],
        )


def _cycle_message(
    members: list[str], provenance: dict[tuple[str, str], str] | None
) -> str:
    """The ``WaveCycleError`` body for cycle path ``members``.

    Each hop is annotated with the provenance of the edge that produced it,
    so an EM can see which leg was authored and which was inferred without
    re-deriving the graph by hand (example-retrieval-repo-em cross-repo memo,
    2026-08-20: "the error names neither leg's provenance").
    """
    head = "cycle detected among rows: " + " -> ".join(members)
    if not provenance:
        return head
    lines = [head, "  each hop reads 'must land in a later wave than':"]
    for node, pred in zip(members, members[1:]):
        origin = provenance.get((node, pred), "provenance unavailable")
        lines.append(f"    {node} -> {pred}  ({origin})")
    return "\n".join(lines)


def _detect_cycle(
    preds: dict[str, set[str]],
    provenance: dict[tuple[str, str], str] | None = None,
) -> None:
    """Raise ``WaveCycleError`` if ``preds`` contains a cycle or self-edge.

    ``provenance`` (``_predecessors``' out-param) annotates each leg of the
    reported cycle with the edge source that minted it.
    """
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
                raise WaveCycleError(
                    _cycle_message([node, node], provenance)
                    if provenance
                    else f"cycle detected: row {node!r} depends on itself"
                )
            if color.get(pred) == GRAY:
                start = path.index(pred)
                members = path[start:] + [pred]
                raise WaveCycleError(_cycle_message(members, provenance))
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

    A row held out under § Epistemic-premise holdout (module docstring)
    never reaches wave placement at all — it, and every row that
    transitively depends on it, is removed from ``rows`` before the
    predecessor graph used for placement is (re)computed, and reported by
    id via ``logging.warning``. This does not weaken the wave-level
    ``NoWritesDeclaredError``-style refusal for any OTHER reason a wave
    might end up with no declared writes — it carves out exactly this one
    named, justified case.

    Cycle detection runs against the FULL, unfiltered predecessor graph —
    before any holdout row is removed — so a cycle consisting entirely of
    held-out rows (each blocked on the other, so neither's epistemic-premise
    gate can ever resolve) still raises ``WaveCycleError`` instead of being
    silently swallowed as two ordinary holds. Removing a strict subset of
    nodes (and their edges) from an already-acyclic graph cannot introduce a
    cycle, so no second cycle check against the filtered graph is needed.
    """
    provenance: dict[tuple[str, str], str] = {}
    full_preds = _predecessors(rows, provenance)
    _detect_cycle(full_preds, provenance)
    held = _compute_held_out(rows, full_preds)
    if held:
        _report_held_out(held)
    rows = [row for row in rows if row.id not in held]

    preds = _predecessors(rows)
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
