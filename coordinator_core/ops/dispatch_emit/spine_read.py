"""
coordinator_core.ops.dispatch_emit.spine_read — plan file -> normalized emitter rows.

Purpose: the single spine-reading entry point for the dispatch-emit pipeline
(docs/plans/2026-08-12-emitter-turns-a-spine-into-one-workflow.md § C1).
Every downstream chunk (wave derivation, pathspec derivation, script
emission, op registration) consumes ``read_spine``'s ``EmitterRow`` list —
none of them re-parses a plan file.

Reuse, do not re-parse: locating and YAML-loading the `` ```yaml plan-tasks ``
fenced block is entirely delegated to
``coordinator_core.ops.plan_tasks_render.load_rows``, which itself wraps
``coordinator_core.frontmatter.body_blocks.locate_fenced_block``. This
module adds exactly two things load_rows does not do: the UNDECLARED-vs-
empty-list distinction on ``writes`` (AC2), and depends_on referent
resolution against the row-id set (AC6). It has no fenced-block or YAML
parsing code of its own.

Four fail-loud behaviours, three AC-bearing and one closing a gap the ACs
didn't name:

  1. AC2 — a row with no ``writes:`` key, or a present-but-empty value
     (``writes:`` with nothing after it, or ``writes: null``), gets the
     ``UNDECLARED`` sentinel — never ``None`` and never ``[]``.
     ``writes: []`` (declared empty) stays an empty list, a structurally
     different value from UNDECLARED so a wave-builder cannot conflate
     "writes nothing" with "unknown, treat as colliding with everything"
     (see the plan's Anti-scope: absent writes: must never be read as an
     empty set).
  2. AC6 — every ``depends_on[].chunk`` referent is resolved against the
     spine's row ``id`` set at read time. A dangling referent raises
     ``DanglingDependencyError`` naming both the row holding the edge and
     the unresolvable value — this referent is asserted in schema prose
     (plan-tasks.schema.json's ``depends_on`` description) but was
     enforced by no code anywhere in the fleet before this module.
  3. A scalar ``writes:``/``reads:``/``depends_on:`` value (e.g.
     ``writes: some/path.py`` instead of a one-item list) raises
     ``InvalidFieldTypeError`` naming the row and the value, rather than
     being silently coerced to a one-item list — left uncoerced, a bare
     string iterates into single characters downstream and silently
     corrupts any overlap comparison.
  4. A missing/non-string ``id``, or an ``id`` that duplicates another
     row's, raises ``InvalidRowIdError`` naming the offending row/id. Not
     an AC in the source plan, but the same failure class as points 2 and
     3 above: downstream, ``wave_map._predecessors`` keys its predecessor
     dict by ``id``, so a duplicate silently collapses two rows' edges
     into one dict entry and corrupts the wave order without raising.
     Enforced once here rather than in every id-keyed consumer.

A fifth behaviour, not one of the four fail-loud ones above but load-bearing:
``read_spine`` excludes non-dispatchable rows (closed ``disposition`` values
and ``deferred: true``) from its returned list entirely, per
DoE-claude's ``skills/execute-plan/SKILL.md`` § Chunk-SET derivation. A row
already shipped (``disposition: coded``) or explicitly deferred must never
reach the dispatch-emit pipeline and re-run as a live
``coordinator:executor`` call. ``depends_on`` referent resolution (AC6)
still runs against the FULL row-id set, before this filter — a live row
legitimately depends on a shipped row, and that edge is satisfied, not
dangling. Once filtering happens, any surviving row's ``depends_on`` edge
pointing at a filtered-out row is stripped (the edge is satisfied; leaving
it would either crash ``wave_map._predecessors`` or wrongly delay the
wave). An edge that resolves to nothing at all is still a hard
``DanglingDependencyError`` — filtering never softens that check.

Negative-spec:
  - Does NOT validate rows against the full plan-tasks schema (required
    fields, disposition cross-field rules, etc.) — that is
    ``schema_validate.py``'s surface. This module reads tolerantly for
    every field except the two AC-bearing ones it fails loudly on, plus
    the closed-disposition/deferred exclusion described above — a value
    filter, not a cross-field validation rule.
  - Does NOT derive waves, pathspecs, or script text — those are C2/C3/C4.
  - Does NOT special-case ``gate_kind`` or any depends_on field beyond
    ``chunk`` — resolving the referent is this module's whole job here.
"""

from __future__ import annotations

from typing import NamedTuple

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ops.plan_tasks_render import load_rows


class _Undeclared:
    """Sentinel type for an absent (or present-but-empty) ``writes:`` field (AC2).

    A distinct type, not ``None``. This class defines no ``__bool__`` or
    ``__len__``, so ``UNDECLARED`` is TRUTHY — a loose ``if not row.writes``
    check is False for ``UNDECLARED`` and True for ``[]``. The two are NOT
    interchangeable under truthiness: a caller that needs to tell "unknown"
    apart from "declared empty" MUST use ``is UNDECLARED`` (identity). A
    caller that blindly truthiness-tests does NOT get correct behaviour
    either way — it silently reads UNDECLARED as "has writes."
    """

    def __repr__(self) -> str:  # pragma: no cover - repr aid only
        return "UNDECLARED"


UNDECLARED = _Undeclared()

# Every closed `disposition` value per coordinator_core/frontmatter/schemas/
# plan-tasks.schema.json — `open` (and an absent disposition, which the
# schema defaults to `open`) is the only dispatchable state. Named once here
# so the filter site never re-spells the literals.
NON_DISPATCHABLE_DISPOSITIONS = frozenset({"coded", "spun_off", "backlogged", "wont_do"})


class SpineReadError(ValueError):
    """Raised when the plan's `` ```yaml plan-tasks `` block cannot be read.

    Covers every non-LOCATED outcome from ``load_rows`` (ABSENT, MALFORMED)
    — this module does not attempt to emit against a spine it could not
    locate or parse.
    """


class DanglingDependencyError(ValueError):
    """Raised when a ``depends_on[].chunk`` referent has no matching row id (AC6)."""


class InvalidFieldTypeError(SpineReadError):
    """Raised when ``writes:``/``reads:``/``depends_on:`` is declared as a
    non-list value (scalar or other container), instead of a list.

    ``writes: some/path.py`` (a bare string) is not coerced to
    ``["some/path.py"]`` — coercion is the tempting fix and the wrong one: a
    spine that declares a scalar is a spine whose author does not know the
    field is a list, and guessing for them hides that. Left uncaught, a
    scalar string iterates into single characters downstream (a str is
    itself an iterable of its own characters), silently corrupting any
    overlap comparison built on it.
    """


class InvalidRowIdError(SpineReadError):
    """Raised when a spine row's ``id`` is missing, non-string, or a
    duplicate of another row's ``id`` in the same spine.

    Review: coordinator:code-reviewer (wsc-A, ecb99d36) — ``wave_map.
    _predecessors`` keys its predecessor dict by ``id``; a duplicate or
    missing id silently collapses two rows into one dict entry rather than
    raising, corrupting the predecessor graph and producing a wrong wave
    order with no error at all. ``spine_read`` is where every other
    row-shape guarantee (AC2 UNDECLARED, AC6 dangling depends_on, scalar
    writes:/reads:) is already established and enforced once for every
    downstream consumer, so id presence/uniqueness belongs here rather than
    as an ad hoc check duplicated in ``wave_map`` (and any future consumer
    of ``read_spine``'s output).
    """


class EmitterRow(NamedTuple):
    """One normalized task-spine row for the dispatch-emit pipeline."""

    id: str
    title: str
    surface: str
    writes: object  # list[str], or the UNDECLARED sentinel
    reads: list
    depends_on: list


def read_spine(plan_path) -> list[EmitterRow]:
    """Read `plan_path`'s task-spine and return normalized ``EmitterRow`` objects.

    Raises ``SpineReadError`` if the spine block is absent or malformed,
    ``InvalidRowIdError`` if any row's ``id`` is missing/non-string or
    duplicates another row's id, ``DanglingDependencyError`` if any row's
    ``depends_on[].chunk`` does not resolve against the spine's row-id set
    (AC6), and ``InvalidFieldTypeError`` if ``writes:``/``reads:``/
    ``depends_on:`` is declared as a non-list value rather than a list.
    ``writes`` is UNDECLARED (AC2) on any row that omits the key or
    declares it present-but-empty; ``reads`` and ``depends_on`` both
    default to ``[]`` when omitted (neither carries an undeclared-vs-empty
    distinction per the schema — only ``writes`` does, per
    plan-tasks.schema.json).

    Rows whose ``disposition`` is closed (see
    ``NON_DISPATCHABLE_DISPOSITIONS``) or whose ``deferred`` is ``true`` are
    excluded from the returned list — they are not dispatchable. depends_on
    referent resolution runs against the full row-id set before this
    exclusion, so an edge onto an excluded row is satisfied, not dangling;
    that edge is then stripped from the surviving row's ``depends_on``.
    """
    with open(plan_path, encoding="utf-8") as handle:
        source = handle.read()

    result = load_rows(source)
    if result.status is not LocateStatus.LOCATED:
        raise SpineReadError(
            f"plan {plan_path!r} task-spine block is {result.status.name}, not LOCATED"
        )

    raw_rows = result.rows

    # id presence/uniqueness (finding: coordinator:code-reviewer wsc-A,
    # ecb99d36, P1). Validated once here, up front, so every downstream
    # consumer (wave_map's predecessor-dict-keyed-by-id foremost among them)
    # inherits a guarantee rather than re-deriving it.
    seen_ids: set[str] = set()
    for raw in raw_rows:
        row_id = raw.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise InvalidRowIdError(f"row {raw!r} has a missing or non-string id")
        if row_id in seen_ids:
            raise InvalidRowIdError(f"duplicate row id {row_id!r} in spine")
        seen_ids.add(row_id)

    row_ids = seen_ids

    rows: list[EmitterRow] = []
    for raw in raw_rows:
        row_id = raw.get("id")
        writes = raw.get("writes")
        if writes is None:
            # Absent key AND present-but-empty value (`writes:` with no
            # scalar/list, or `writes: null`) both collapse to UNDECLARED —
            # AC2 admits exactly two states, never a third (see module
            # docstring point 1).
            writes = UNDECLARED
        elif not isinstance(writes, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares writes: as {writes!r}, not a list"
            )
        reads = raw.get("reads")
        if reads is None:
            reads = []
        elif not isinstance(reads, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares reads: as {reads!r}, not a list"
            )
        depends_on = raw.get("depends_on")
        if depends_on is None:
            depends_on = []
        elif not isinstance(depends_on, list):
            raise InvalidFieldTypeError(
                f"row {row_id!r} declares depends_on: as {depends_on!r}, not a list"
            )

        for edge in depends_on:
            chunk = edge.get("chunk") if isinstance(edge, dict) else None
            if chunk not in row_ids:
                raise DanglingDependencyError(
                    f"row {row_id!r} depends_on unresolvable chunk {chunk!r}"
                )

        rows.append(
            EmitterRow(
                id=row_id,
                title=raw.get("title", ""),
                surface=raw.get("surface", ""),
                writes=writes,
                reads=reads,
                depends_on=depends_on,
            )
        )

    # Non-dispatchable exclusion: closed disposition or deferred: true.
    # Computed after depends_on referent resolution against the full row-id
    # set above, so a live row's edge onto a shipped/deferred row is already
    # known-satisfied rather than dangling.
    excluded_ids: set[str] = set()
    for raw in raw_rows:
        disposition = raw.get("disposition")
        deferred = raw.get("deferred", False)
        if disposition in NON_DISPATCHABLE_DISPOSITIONS or deferred is True:
            # raw.get("id") cannot be None here: the id presence/uniqueness
            # loop above already required every raw["id"] to be a non-empty
            # string before this loop runs. Keep the two loops in that
            # order -- reordering them reintroduces a possible None into
            # excluded_ids with no signal.
            excluded_ids.add(raw.get("id"))

    dispatchable_rows = [row for row in rows if row.id not in excluded_ids]
    for i, row in enumerate(dispatchable_rows):
        if not row.depends_on:
            continue
        stripped = [
            edge
            for edge in row.depends_on
            if not (isinstance(edge, dict) and edge.get("chunk") in excluded_ids)
        ]
        if len(stripped) != len(row.depends_on):
            dispatchable_rows[i] = row._replace(depends_on=stripped)

    return dispatchable_rows
