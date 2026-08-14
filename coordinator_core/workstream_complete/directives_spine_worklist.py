"""
coordinator_core.workstream_complete.directives_spine_worklist — the
open-plan-spine-row advisory for the `workstream-complete-assemble`
computed-skill engine.

Purpose: a plan-tasks spine row that never reaches a terminal
`disposition` has five honest ends, reachable via `python3 coordinator/bin/
plan-tasks-resolve` — do it now (`disposition: coded`, no PM word), spin
it off or move it to an existing plan (`disposition: spun_off`, no PM
word — relaxed at DoE `bd0475fd5`, schema 1.4.0), backlog it to the
improvement queue (`disposition: backlogged`, a PM word required), or
rule it won't-do (`disposition: wont_do`, a PM word required) — plus the
legitimate non-terminal choice of leaving it genuinely `open` and
carried on the successor baton. There is a sixth, dishonest end where
the row is none of those, nobody notices at close time, and the work
evaporates with no record it ever existed. This module computes the
read-only advisory (`gates.open_spine_row_worklist`) that names any
still-open rows on the session's governing plan at close time, closing
that silent dishonest exit — it does NOT write anything, and it never
blocks (see Negative-spec).

Spec backlink: pln-workstream-complete-names-its-dc94b7,
chunk C1. Design provenance: C13 of DoE-claude's
docs/plans/2026-07-29-pm-approved-provenance-write-time-closure-gate.md,
PM-ratified, handed over via docs/plans/2026-08-05-leg-a-closing-aid-
terminal-statuses.md and delivered as ask 5 of source memo
2026-08-05-doe-claude-em-leg-a-ac-checkbox-divestment.md.

Mirrors `directives_session_hygiene.py`'s Step 2.96 completeness-
checklist gate shape (`FREE_VALUE_KEYS`, an item NamedTuple, a gate
NamedTuple, a `_WARN_TEMPLATE`, one pure compute function) — that
module is the reference implementation for this one; see its own
docstring for the fuller rationale behind the shape being mirrored here.

Consumes (orchestrates, reimplements none):
    coordinator_core.ops.plan_tasks_render.load_rows / spine_projection
        -> locates and parses the governing plan's `` ```yaml plan-tasks ``
        fenced block and projects it to `{"open": [...], "closed_count": n}`.
        This module writes NO second spine parser and NO second
        disposition accessor — `_disposition`'s schema-default handling
        (a row missing `disposition` reads as `open`) is inherited
        entirely from that module.

Negative-spec:
    - Does NOT write `carried_items[]` on any successor baton — PM-cut
      scope (see the plan's "Scope cut" and "Out of scope" sections).
      Automating exit three is a deferred, unclaimed row (D1), not this
      module's concern.
    - Does NOT block. `compute_open_spine_row_gate` returns no exit
      code, no blocking judgment point, and its own `applies`/`warn_text`
      feed only the advisory `gates.open_spine_row_worklist` fact — never
      a `judgment_points[]` entry, and never a `directives[]` dependency
      edge. An EM finishing a workstream with rows open and carried is
      exit three and is legitimate; this module exists only to make that
      exit legible, never to prevent it.
    - Does NOT hard-code a kill-condition count, or any other
      auto-retirement threshold — see the plan's Anti-scope.
    - Does NOT import a "canonical" disposition-terminal set from
      `lifecycle_constants.py` — that module's docstring warns against
      asserting its status axes equal a spine row's `disposition` axis.
    - Does NOT verify a `coded` row's `disposition_ref` against real git
      history — `schema_validate.py` validates shape only, and this
      module inherits that same shallow trust, unclaimed here as
      elsewhere (see the plan's "Out of scope").
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ops.plan_tasks_render import load_rows, spine_projection

_WAIVED_ROWS_KEY = "waived_open_spine_row_ids"

#: The `decisions` keys `compute_open_spine_row_gate` reads — declared once
#: so a caller (`__init__.py`'s `preflight.decisions_template` composition)
#: can import and union this tuple rather than hand-copying the key list.
#: Mirrors `directives_session_hygiene.FREE_VALUE_KEYS`'s own role and the
#: AC3 one-oracle rule from docs/plans/2026-07-29-workstream-complete-the-
#: envelope-names-t.md.
FREE_VALUE_KEYS: tuple[str, ...] = (_WAIVED_ROWS_KEY,)


class SpineRowItem(NamedTuple):
    id: str
    title: str
    #: True when the caller's `decisions[_WAIVED_ROWS_KEY]` names this
    #: row's `id` — a row the EM has already reviewed and knowingly left
    #: open. Waiving removes a row from `warn_text`'s enumeration, never
    #: from `rows`/`open_count`: those two stay the spine's own ground
    #: truth (every `open`-disposition row, unfiltered), matching
    #: `CompletenessChecklistGate.items`'s own "full evidence, filtered
    #: warn_text" shape.
    waived: bool


class OpenSpineRowGate(NamedTuple):
    applies: bool
    rows: tuple[SpineRowItem, ...]
    open_count: int
    warn_text: Optional[str]
    summary_line: str
    #: Which of the three answers `applies=False` was standing in for,
    #: drawing the same `not-applicable` vs `indeterminate` distinction
    #: `consumed_handoff_completeness` already draws. `applies` alone
    #: collapses "resolved the governing plan, nothing open" with "could
    #: not resolve a governing plan at all", and a caller cannot tell a
    #: genuinely clean close from a gate whose input never arrived — the
    #: false-clean that let a plan-authoring session close with five open
    #: spine rows (source memo 2026-08-12-example-market-data-repo-em-wsc-
    #: capped-a-session-with-an-unexecuted-plan.md).
    #:
    #:   "applicable"     — spine resolved, at least one row open
    #:   "not-applicable" — spine resolved, nothing open (or no spine)
    #:   "indeterminate"  — no input: no governing plan resolved, the plan
    #:                      file unreadable, or its spine fence malformed
    #:
    #: Advisory only, exactly like `applies`: `indeterminate` adds no
    #: judgment point, no dependency edge, and no exit code (see the
    #: module Negative-spec, and the source memo's own "not asking you to
    #: make the ceremony refuse on ambiguity").
    verdict: str = "not-applicable"


_NOT_APPLICABLE_SUMMARY = "Open spine rows: not applicable — no open rows on the governing plan"

_INDETERMINATE_SUMMARY = (
    "Open spine rows: INDETERMINATE — {reason}; this is not a clean-close signal, "
    "check by hand whether a plan of this session's own authorship has open rows"
)

_WARN_TEMPLATE = """WARN [open-spine-row-worklist]: {count} plan-spine row(s) still open on {plan_ref}.
Five honest ends, via `python3 coordinator/bin/plan-tasks-resolve --id <row-id> ... --disposition-detail "<why>"`:
  - do it now  : --coded <sha>          (disposition: coded      -- no PM word needed)
  - spin off   : --spun-off <ref>       (disposition: spun_off   -- no PM word needed)
  - move it    : --moved-to <plan-path> (disposition: spun_off   -- no PM word needed)
  - backlog it : --backlogged           (disposition: backlogged -- PM word required)
  - won't do   : --wont-do              (disposition: wont_do    -- PM word required)
Or leave it open and carried on the successor baton -- that is still a legitimate close.

Open rows:
{row_lines}

Reference: docs/plans/2026-08-05-wsc-open-spine-row-worklist.md
To waive: add the row id(s) to decisions["{waived_key}"] once you've reviewed and knowingly
left it open, or resolve it via `python3 coordinator/bin/plan-tasks-resolve` above and re-run
/workstream-complete."""


def _indeterminate(reason: str) -> OpenSpineRowGate:
    """The gate could not resolve its input — distinct from resolving it
    and finding nothing open. Same non-blocking shape as every other
    `applies=False` branch; only `verdict`/`summary_line` differ."""
    return OpenSpineRowGate(
        applies=False,
        rows=(),
        open_count=0,
        warn_text=None,
        summary_line=_INDETERMINATE_SUMMARY.format(reason=reason),
        verdict="indeterminate",
    )


def compute_open_spine_row_gate(
    governing_plan_slug: Optional[str],
    governing_plan_path: Optional[Path],
    decisions: Optional[Mapping[str, Any]] = None,
) -> OpenSpineRowGate:
    """The read-only advisory over the existing spine primitives (AC1, AC5).

    Degrades to `applies=False, warn_text=None` (AC3/AC7), never raises,
    on every one of: no resolvable governing plan (`governing_plan_slug`
    or `governing_plan_path` absent), an unreadable plan file, a
    `load_rows` status that is not LOCATED (no fence, or a malformed
    one), or a LOCATED spine with zero `open`-disposition rows.
    `summary_line` is populated in the not-applicable case too, matching
    `CompletenessChecklistGate`'s own convention.

    Those five degradations are NOT one answer, and `verdict` splits them
    (see `OpenSpineRowGate.verdict`): the first three are `indeterminate`
    — the gate never got an input — while an absent fence or an empty
    open set is `not-applicable`, a real resolution that found nothing.
    Collapsing them is what let a plan-authoring session read as a clean
    close: governing-plan resolution has no input on a session that
    *writes* a plan rather than inheriting one, so `applies=False` fired
    for want of an input and was indistinguishable at the call site from
    a spine with every row terminal.

    When at least one open row exists, `rows`/`open_count` reflect the
    FULL open set from `spine_projection` unfiltered by waiver — a
    caller inspecting the gate's evidence sees exactly what the spine
    says is open, regardless of what any `decisions` waiver names.
    `warn_text` is the subset excluding rows the caller has explicitly
    waived (see `SpineRowItem.waived`); `warn_text` is `None` whenever
    every open row is waived, even though `applies` stays `True` and
    `open_count` stays the real, unwaived count — mirroring
    `compute_completeness_checklist_gate`'s own "applies True, warn_text
    None once every item is accounted for" shape.
    """
    decisions = decisions or {}
    waived_ids = frozenset(str(x) for x in decisions.get(_WAIVED_ROWS_KEY, ()))

    if not governing_plan_slug or governing_plan_path is None:
        return _indeterminate("no governing plan resolved for this session")

    try:
        source = governing_plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _indeterminate(f"governing plan {governing_plan_slug} could not be read")

    result = load_rows(source)
    if result.status is LocateStatus.MALFORMED:
        return _indeterminate(f"governing plan {governing_plan_slug} has a malformed plan-tasks spine")
    if result.status is not LocateStatus.LOCATED:
        return OpenSpineRowGate(
            applies=False, rows=(), open_count=0, warn_text=None,
            summary_line=_NOT_APPLICABLE_SUMMARY, verdict="not-applicable",
        )

    projection = spine_projection(result.rows)
    open_rows = projection["open"]
    if not open_rows:
        return OpenSpineRowGate(
            applies=False, rows=(), open_count=0, warn_text=None,
            summary_line=_NOT_APPLICABLE_SUMMARY, verdict="not-applicable",
        )

    items = tuple(
        SpineRowItem(
            id=str(row.get("id", "?")),
            title=str(row.get("title", "")),
            waived=str(row.get("id", "?")) in waived_ids,
        )
        for row in open_rows
    )

    unwaived = [it for it in items if not it.waived]
    if not unwaived:
        summary_line = f"Open spine rows: {len(items)} open on {governing_plan_slug} — all waived"
        return OpenSpineRowGate(
            applies=True, rows=items, open_count=len(items), warn_text=None,
            summary_line=summary_line, verdict="applicable",
        )

    row_lines = "\n".join(f"  - {it.id} — {it.title}" for it in unwaived)
    warn_text = _WARN_TEMPLATE.format(
        count=len(unwaived),
        plan_ref=governing_plan_slug,
        row_lines=row_lines,
        waived_key=_WAIVED_ROWS_KEY,
    )
    summary_line = f"Open spine rows: {len(unwaived)} still open on {governing_plan_slug} — WARN emitted"
    return OpenSpineRowGate(
        applies=True, rows=items, open_count=len(items), warn_text=warn_text,
        summary_line=summary_line, verdict="applicable",
    )
