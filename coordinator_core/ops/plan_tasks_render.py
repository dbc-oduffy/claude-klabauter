"""
coordinator_core.ops.plan_tasks_render — read/projection module over the
plan `## Tasks` task-spine's disposition field set.

Purpose: three generated (never hand-maintained, D6) read/compute
projections over the spine rows a plan already carries:

  1. ``render_closed_items(rows)`` — the human-legible "Closed items"
     markdown section, grouped by disposition.
  2. ``spine_projection(rows)`` — the unresolved-head-in-full /
     closed-tail-as-count shape a subagent sidecar can consume without
     reading every closed row's prose.
  3. ``dispositions_for_delivered(rows, delivered_ids, ...)`` — the missing
     computation step behind klabauter#44's "no op sets a spine row's
     disposition, so a delivered row re-emits as live forever": nothing in
     this pipeline ever computed WHAT a closing write for a delivered row
     should even contain, so there was nothing for a mutate op to be
     handed. This function derives that payload; it does not write it —
     see negative-spec below.

Nothing physically moves out of the spine (D6) — both outputs are computed
fresh from the current rows on every call; there is no stored/cached
rendering for a caller to accidentally treat as the source of truth. The
row is, and remains, the single source of truth.

Homing (see docs/plans/2026-07-27-plan-line-item-resolution-model.md § C9,
DoE-claude): this is a NEW module, not an addition to plan_tasks_mutate.py.
It is a READ concern depending on C1's schema fields, not a MUTATE concern
depending on C4's ``resolve`` verb — and plan_tasks_mutate.py's own
docstring already carries a negative-spec ("Does NOT support verbs beyond
add-task / stamp") that C4 amends once; widening that module further here
would give it a second, unrelated reason to change.

Locate rule: reuses ``coordinator_core.frontmatter.body_blocks.locate_fenced_block``
exclusively — no fresh parser. See that module's docstring for the two
documented fenced-block traps this reuse avoids (a template-comment
collision causing a literal-fence miscount, and a prose-intro skip), both
of which caused real silent row-drops on 2026-07-11.

Not a JSON-RPC op: no ``@register_op`` call here — pure read + compute,
the same "plain module, direct import" shape as
``coordinator_core.ops.coordinator_render_rollup`` and
``coordinator_core.hooks.auto_push``. Callers import and call directly.

Closed-set scope decision (C9, executor judgment call — recorded here
since the plan's row body left it open): D5's ORDERING rule ("closed rows
sort to the bottom") treats every non-``open`` disposition as closed —
``coded`` included — and ``spine_projection``'s ``closed_count`` follows
that same partition. ``render_closed_items`` is narrower on purpose: it
covers only ``spun_off`` / ``backlogged`` / ``wont_do``, excluding
``coded``. Rationale: the Problem section this plan opens with is "scope
leaves a plan without leaving a trace" — that gap is specifically about
work that left the plan's scope (spun off, backlogged, declined), not
about work that shipped. A ``coded`` row already has a home: the plan's
own Acceptance-Criteria table and the commit it points to via
``disposition_ref``. Rendering it a second time in "Closed items" would
duplicate a signal that already has a legible home and dilute the
section's actual purpose.

Negative-spec:
  - Does NOT write any file, does NOT call git, does NOT lock — pure read
    + compute. A caller wanting to splice the rendered section back into a
    plan body does that itself (e.g. via a future op); this module never
    touches plan source bytes. ``dispositions_for_delivered`` is no
    exception: it returns a payload shaped for
    ``coordinator_core.ops.plan_tasks_mutate``'s ``resolve`` verb
    (``id``/``disposition``/``disposition_ref``) — it never calls that op,
    never opens ``locked_rmw``, and never mutates ``rows`` in place.
  - Does NOT re-implement the fenced-block locate rule inline — see above.
  - Does NOT validate rows against the vendored schema. A row missing or
    misshaping ``disposition`` is read tolerantly (falls back to the
    schema's own ``open`` default, D1) rather than raising — schema
    enforcement is ``schema_validate.py``'s surface (C2), not this one's.
  - ``dispositions_for_delivered`` does NOT decide WHICH ids are delivered
    — that evidence (a landed commit sha, a DONE dispatch report) lives
    outside this module and outside this plan's own spine, so the caller
    supplies ``delivered_ids`` rather than this module inferring it from
    disk or git. Never widen this into a delivery-detection heuristic.
"""

from __future__ import annotations

from typing import NamedTuple

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPEN = "open"

# Fixed render order — not sorted, not schema-enum order (schema order is
# open|coded|spun_off|backlogged|wont_do; this list omits coded/open and
# fixes the remaining three so output is stable across runs).
_CLOSED_SECTION_DISPOSITIONS = ("spun_off", "backlogged", "wont_do")

_DISPOSITION_SECTION_TITLES = {
    "spun_off": "Spun off",
    "backlogged": "Backlogged",
    "wont_do": "Won't do",
}


# ---------------------------------------------------------------------------
# Loading rows out of a plan's raw source
# ---------------------------------------------------------------------------


class RowsResult(NamedTuple):
    """Outcome of loading task-spine rows out of a plan's raw markdown source.

    Mirrors ``body_blocks.LocateResult``'s LOCATED/ABSENT/MALFORMED
    discriminant rather than collapsing to a bare list — a caller
    distinguishing "no spine yet" from "spine is malformed" needs the
    status, not just an empty ``rows`` list (both cases end up with
    ``rows == []``).
    """

    status: LocateStatus
    rows: list


def load_rows(source: str) -> RowsResult:
    """Locate and parse the `` ```yaml plan-tasks `` fenced block out of `source`.

    Delegates locating to ``body_blocks.locate_fenced_block`` exclusively
    (see module docstring) — never a fresh parser. A LOCATED block whose
    body is not a YAML list, or whose parse fails, or whose parse contains
    a non-dict entry, degrades the result to MALFORMED rather than raising
    — this module reads tolerantly; it is not the schema-enforcement
    surface (that is ``schema_validate.py``, C2).
    """
    result = locate_fenced_block(source)
    if result.status is not LocateStatus.LOCATED:
        return RowsResult(status=result.status, rows=[])

    try:
        rows = yaml.safe_load(result.body) or []
    except yaml.YAMLError:
        return RowsResult(status=LocateStatus.MALFORMED, rows=[])

    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return RowsResult(status=LocateStatus.MALFORMED, rows=[])

    return RowsResult(status=LocateStatus.LOCATED, rows=rows)


def _disposition(row: dict) -> str:
    """Row's disposition, defaulting to 'open' per the schema default (D1)."""
    value = row.get("disposition")
    return value if isinstance(value, str) and value else _OPEN


# ---------------------------------------------------------------------------
# (b) unresolved-head / closed-tail-count projection
# ---------------------------------------------------------------------------


def spine_projection(rows: list) -> dict:
    """Unresolved-head/closed-tail-count projection (C9 output (b)).

    Returns ``{"open": [<row>, ...], "closed_count": <int>}`` — every
    ``open``-disposition row in full (a subagent sidecar reading this
    projection sees exactly the live work, unabridged), plus a bare count
    of everything else. D5 sorts closed rows to the spine's tail; this
    projection is the machine-consumable form of that same head/tail
    split — the sidecar never needs the closed rows' content, only how
    many there are. ``closed_count`` follows D5's full non-open partition
    (``coded`` included), which is intentionally wider than
    ``render_closed_items``'s narrower closed-SECTION scope — see the
    module docstring's "Closed-set scope decision" for why the two differ.
    """
    open_rows = [row for row in rows if _disposition(row) == _OPEN]
    return {"open": open_rows, "closed_count": len(rows) - len(open_rows)}


# ---------------------------------------------------------------------------
# (c) the disposition-write payload behind a delivered row (klabauter#44)
# ---------------------------------------------------------------------------


def dispositions_for_delivered(
    rows: list,
    delivered_ids,
    *,
    disposition: str = "coded",
    disposition_ref: str | None = None,
) -> list[dict]:
    """Derive the ``plan.tasks.mutate resolve`` batch payload that would
    close every row in ``delivered_ids`` — the computation klabauter#44
    found missing: nothing produced this payload, so nothing was ever
    handed to ``resolve``, and a delivered row's ``disposition`` stayed at
    its schema default (``open``) forever, re-emitting as live on every
    subsequent read of the spine.

    Returns one ``{"id": ..., "disposition": ...}`` dict per row in
    ``rows`` whose ``id`` is in ``delivered_ids`` AND whose CURRENT
    disposition is still ``open`` (via ``_disposition``, D1-tolerant) — an
    id already resolved to some other disposition is left out, so a
    caller can pass this straight to ``resolve``'s batch param without
    re-deriving idempotency itself (re-resolving an already-closed row is
    the caller's decision, not this function's). ``disposition_ref`` is
    included only when supplied, and then on every returned entry
    uniformly — this function has no way to derive a per-row ref (a commit
    sha, a queue path) from ``rows`` alone; the caller who knows what
    landed supplies it or leaves it to ``resolve``'s own required-only-
    conditionally validation.

    ``delivered_ids`` may be any iterable — a ``set`` is not required, and
    passing a ``list`` (e.g. straight off a dispatch report) works
    unchanged. An id in ``delivered_ids`` naming no row in ``rows`` is
    silently ignored: this function derives payload for rows that exist,
    it does not validate the caller's delivery evidence.

    Row ORDER in the return mirrors ``rows``' own order, not
    ``delivered_ids``' — matching ``resolve``'s own batch semantics, which
    treat order as insignificant but this keeps output deterministic for a
    given ``rows`` input regardless of how ``delivered_ids`` was built
    (e.g. from an unordered set).
    """
    wanted = set(delivered_ids)
    updates: list[dict] = []
    for row in rows:
        row_id = row.get("id")
        if row_id not in wanted:
            continue
        if _disposition(row) != _OPEN:
            continue
        entry = {"id": row_id, "disposition": disposition}
        if disposition_ref is not None:
            entry["disposition_ref"] = disposition_ref
        updates.append(entry)
    return updates


# ---------------------------------------------------------------------------
# (a) human-legible "Closed items" section
# ---------------------------------------------------------------------------


def _render_row_bullet(row: dict) -> str:
    """One bullet line for a single closed row: id, title, ref and/or detail."""
    row_id = row.get("id", "?")
    title = row.get("title", "")
    ref = row.get("disposition_ref")
    detail = row.get("disposition_detail")

    parts = [f"**{row_id}** — {title}"]
    if ref:
        parts.append(f"(`{ref}`)")
    if detail:
        parts.append(f"— {detail}")
    return "- " + " ".join(parts)


def render_closed_items(rows: list) -> str:
    """Render the human-legible "Closed items" markdown section from spine rows.

    Generated, never hand-maintained (D6) — every call recomputes fresh
    from the current rows; there is no stored rendering to go stale.
    Groups by disposition — Spun off / Backlogged / Won't do, in that
    fixed order — with one bullet per row naming its ``id``, ``title``,
    and whichever of ``disposition_ref``/``disposition_detail`` it
    carries. ``coded`` and ``open`` rows are excluded from this section —
    see the module docstring's "Closed-set scope decision" for why.

    Returns ``""`` (no section at all, not an empty placeholder) when no
    row carries a disposition this section covers — a plan with nothing
    yet spun off, backlogged, or declined renders nothing, matching D6's
    generated-not-maintained intent: there is no stale empty section to
    leave behind.
    """
    body_lines: list = []
    for disposition in _CLOSED_SECTION_DISPOSITIONS:
        matching = [row for row in rows if _disposition(row) == disposition]
        if not matching:
            continue
        body_lines.append(f"### {_DISPOSITION_SECTION_TITLES[disposition]}")
        body_lines.append("")
        for row in matching:
            body_lines.append(_render_row_bullet(row))
        body_lines.append("")

    if not body_lines:
        return ""

    return "## Closed items\n\n" + "\n".join(body_lines).rstrip() + "\n"
