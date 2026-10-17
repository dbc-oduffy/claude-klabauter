"""Workday-start briefing renderer: ledger, decisions and envelope in, markdown out.

Pure; no I/O. The briefing is a record of what ran and what was decided. Its section set is the
closed tuple `SECTION_ORDER`; renderer-authored text is checked against `NARRATION_DENY_LIST`.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from coordinator_core.ceremony_common.run import receipt
from coordinator_core.workday_start.contract import (
    NARRATION_DENY_LIST,
    Decision,
    StepResult,
    StepSpec,
)

FAILED = receipt.FAILED
DECIDED = receipt.DECIDED
OBSERVATIONS = receipt.OBSERVATIONS
NOT_RUN = receipt.NOT_RUN
RUN_COST = receipt.RUN_COST

# Closed set, in emission order. A heading outside it cannot be emitted.
SECTION_ORDER: tuple[str, ...] = (
    FAILED,
    DECIDED,
    "Branch Span Mismatch",
    "Context Freshness",
    "Handoffs",
    "Inbox",
    "Daily-Wrap Coverage",
    "Alignment Check",
    "Orphan Sweep",
    "Auto-Push Health",
    "Addon Health",
    "Test-Red Delta",
    "Blast Radius",
    "Stale Branches",
    "Lessons Outbox",
    "Weekly",
    "Wrap Backfill",
    "Priority Suggestions",
    OBSERVATIONS,
    NOT_RUN,
    RUN_COST,
)


def render(
    ledger_rows: Sequence[StepResult | Mapping[str, Any]],
    decisions: Sequence[Decision],
    observations: Sequence[str],
    envelope: Mapping[str, Any] | None,
    *,
    date: str,
    branch: str,
    steps: Sequence[StepSpec],
) -> str:
    """Render the briefing markdown.

    `steps` is the manifest's step list for the run: it supplies each step's heading and stage
    and is the set every ledger row must cover. Raises ValueError when a
    manifest step has no ledger row, a step heading is outside `SECTION_ORDER`, or
    renderer-authored text hits `NARRATION_DENY_LIST`.
    """
    rows = [receipt.as_row(r) for r in ledger_rows]
    by_id = {r.id: r for r in rows}
    spec_by_id = {s.id: s for s in steps}
    missing = [s.id for s in steps if s.id not in by_id]
    if missing:
        raise ValueError(f"briefing: manifest steps with no ledger row: {', '.join(missing)}")

    sections: dict[str, list[str]] = {name: [] for name in SECTION_ORDER}
    record = receipt.render_record(rows, decisions, observations, expected_step_ids=[], steps=spec_by_id)
    sections[FAILED] = record[FAILED].splitlines()
    sections[DECIDED] = record[DECIDED].splitlines()
    for s in steps:
        r = by_id[s.id]
        if r.status != "ran" or not r.stdout.strip():
            continue
        name = receipt.bare_heading(s.heading)
        receipt.check_narration(name)
        if name not in sections:
            raise ValueError(f"briefing: heading {name!r} is not in the section set")
        sections[name].extend(r.stdout.rstrip().splitlines())

    obs = record[OBSERVATIONS].splitlines()
    next_move = str((envelope or {}).get("next_move") or "").strip()
    if next_move and not any(p.lower() in next_move.lower() for p in NARRATION_DENY_LIST):
        obs.append(f'- envelope output: "{next_move}"')
    sections[OBSERVATIONS] = sections[OBSERVATIONS] + obs
    sections[NOT_RUN] = record[NOT_RUN].splitlines()
    sections[RUN_COST] = record[RUN_COST].splitlines()

    out = [receipt.check_narration(f"# Workday-start briefing: {date} ({branch})")]
    for name in SECTION_ORDER:
        body = sections[name]
        if not body:
            continue
        out.append("")
        out.append(receipt.check_narration(f"### {name}"))
        out.extend(receipt.clip(body))
    return "\n".join(out) + "\n"
