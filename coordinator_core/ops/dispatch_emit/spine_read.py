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

Two fail-loud behaviours, both AC-bearing:

  1. AC2 — a row with no ``writes:`` key gets the ``UNDECLARED`` sentinel,
     never ``None`` and never ``[]``. ``writes: []`` (declared empty) stays
     an empty list, a structurally different value from UNDECLARED so a
     wave-builder cannot conflate "writes nothing" with "unknown, treat as
     colliding with everything" (see the plan's Anti-scope: absent writes:
     must never be read as an empty set).
  2. AC6 — every ``depends_on[].chunk`` referent is resolved against the
     spine's row ``id`` set at read time. A dangling referent raises
     ``DanglingDependencyError`` naming both the row holding the edge and
     the unresolvable value — this referent is asserted in schema prose
     (plan-tasks.schema.json's ``depends_on`` description) but was
     enforced by no code anywhere in the fleet before this module.

Negative-spec:
  - Does NOT validate rows against the full plan-tasks schema (required
    fields, disposition cross-field rules, etc.) — that is
    ``schema_validate.py``'s surface. This module reads tolerantly for
    every field except the two it exists to fail loudly on.
  - Does NOT derive waves, pathspecs, or script text — those are C2/C3/C4.
  - Does NOT special-case ``gate_kind`` or any depends_on field beyond
    ``chunk`` — resolving the referent is this module's whole job here.
"""

from __future__ import annotations

from typing import NamedTuple

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ops.plan_tasks_render import load_rows


class _Undeclared:
    """Sentinel type for an absent ``writes:`` field (AC2).

    A distinct type, not ``None``, so downstream code cannot accidentally
    treat "undeclared" as falsy-equivalent to an empty list via a loose
    ``if not row.writes`` check — such a check is true for BOTH
    ``UNDECLARED`` and ``[]`` today, which is exactly the confusion AC2
    exists to make structurally hard: callers that need to tell them apart
    must use ``is UNDECLARED`` (identity), and a caller that doesn't care
    still gets correct falsy behaviour either way.
    """

    def __repr__(self) -> str:  # pragma: no cover - repr aid only
        return "UNDECLARED"


UNDECLARED = _Undeclared()


class SpineReadError(ValueError):
    """Raised when the plan's `` ```yaml plan-tasks `` block cannot be read.

    Covers every non-LOCATED outcome from ``load_rows`` (ABSENT, MALFORMED)
    — this module does not attempt to emit against a spine it could not
    locate or parse.
    """


class DanglingDependencyError(ValueError):
    """Raised when a ``depends_on[].chunk`` referent has no matching row id (AC6)."""


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

    Raises ``SpineReadError`` if the spine block is absent or malformed, and
    ``DanglingDependencyError`` if any row's ``depends_on[].chunk`` does not
    resolve against the spine's row-id set (AC6). ``writes`` is UNDECLARED
    (AC2) on any row that omits the key; ``reads`` and ``depends_on`` both
    default to ``[]`` when omitted (neither carries an undeclared-vs-empty
    distinction per the schema — only ``writes`` does, per plan-tasks.schema.json).
    """
    with open(plan_path, encoding="utf-8") as handle:
        source = handle.read()

    result = load_rows(source)
    if result.status is not LocateStatus.LOCATED:
        raise SpineReadError(
            f"plan {plan_path!r} task-spine block is {result.status.name}, not LOCATED"
        )

    raw_rows = result.rows
    row_ids = {raw.get("id") for raw in raw_rows if isinstance(raw.get("id"), str)}

    rows: list[EmitterRow] = []
    for raw in raw_rows:
        row_id = raw.get("id")
        writes = raw["writes"] if "writes" in raw else UNDECLARED
        reads = raw.get("reads") or []
        depends_on = raw.get("depends_on") or []

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

    return rows
