"""Workday-start briefing renderer: ledger, decisions and envelope in, markdown out.

Pure; no I/O. The briefing is a record of what ran and what was decided. Its section set is the
closed tuple `SECTION_ORDER`; renderer-authored text is checked against `NARRATION_DENY_LIST`.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from coordinator_core.workday_start.contract import (
    DECISIONS_FILE,
    LEDGER_ROW_KEYS,
    NARRATION_DENY_LIST,
    Decision,
    StepResult,
    StepSpec,
)

FAILED = "Failed"
DECIDED = "Decided"
OBSERVATIONS = "Observations"
NOT_RUN = "Not run"
RUN_COST = "Run cost"

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

FAILURE_BACKLOG_STAGE = "failure-backlog"
ENGINE_BAR_MS = 500.0
MAX_RULE_POINTS = 3


def _check_authored(text: str) -> str:
    low = text.lower()
    for phrase in NARRATION_DENY_LIST:
        if phrase.lower() in low:
            raise ValueError(f"briefing: renderer-authored text contains deny-listed phrase {phrase!r}")
    return text


def _bare_heading(heading: str) -> str:
    return heading.strip().lstrip("#").strip()


def _row(r: StepResult | Mapping[str, Any]) -> StepResult:
    if isinstance(r, StepResult):
        return r
    return StepResult(**{k: r[k] for k in LEDGER_ROW_KEYS if k in r})


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _backlog_id(step_id: str, rows: dict[str, StepResult], steps: dict[str, StepSpec]) -> str:
    """The failure-backlog stage row whose args name `step_id`; its first stdout line is the id."""
    for sid, spec in steps.items():
        if spec.stage == FAILURE_BACKLOG_STAGE and step_id in spec.args:
            row = rows.get(sid)
            if row is not None and row.status == "ran":
                return _first_line(row.stdout)
    return ""


def _failed(rows: list[StepResult], by_id: dict[str, StepResult], steps: dict[str, StepSpec]) -> list[str]:
    failed = [r for r in rows if r.status == "failed"]
    out: list[str] = []
    for r in failed:
        backlog = _backlog_id(r.id, by_id, steps)
        out.append(f"- `{r.id}`: {r.reason or 'failed'}; backlog row: {backlog or 'none filed'}")
    if any(steps[r.id].gates_next for r in failed if r.id in steps):
        halted = sum(1 for r in rows if r.status == "halted-by-gate")
        out.append(f"- halted-by-gate: {halted}")
    return out


def _decided(decisions: Sequence[Decision]) -> list[str]:
    step0: list[str] = []
    agent: list[str] = []
    grouped: dict[tuple[str, str], list[Decision]] = {}
    for d in decisions:
        if d.decided_by == "rule" and d.rule.startswith("step0-"):
            step0.append(f"- `{d.rule}` `{d.point_id}`: {d.question} -> {d.decision}; {d.rationale}")
        elif d.decided_by == "rule":
            grouped.setdefault((d.rule, d.decision), []).append(d)
        else:
            acted = ", ".join(d.acted) if d.acted else "none"
            agent.append(
                f"- {d.question} -> {d.decision} ({d.decided_by}): {d.rationale}; acted: {acted}"
            )
    rules = []
    for (rule, decision), ds in grouped.items():
        ids = ", ".join(d.point_id for d in ds[:MAX_RULE_POINTS])
        more = ", ..." if len(ds) > MAX_RULE_POINTS else ""
        rules.append(f"- rule `{rule}` x{len(ds)}: {decision}; {ids}{more}")
    lines = step0 + agent + rules
    if lines:
        lines.append(f"- full record: `{DECISIONS_FILE}`")
    return lines


def _not_run(rows: list[StepResult]) -> list[str]:
    return [
        f"- `{r.id}` ({r.status}): {r.reason or 'no reason recorded'}"
        for r in rows
        if r.status in ("not-composed", "halted-by-gate")
    ]


def _run_cost(rows: list[StepResult], steps: dict[str, StepSpec]) -> list[str]:
    ran = [r for r in rows if r.status == "ran"]
    if not ran:
        return []
    spawns = sum(r.spawns or 0 for r in ran)
    timed = [r for r in ran if r.process_ms is not None]
    parts = [f"{len(ran)} steps run", f"{spawns} spawns"]
    if timed:
        slow = max(timed, key=lambda r: r.process_ms or 0.0)
        parts.append(f"slowest `{slow.id}` {slow.process_ms:.0f}ms")
    over = [
        f"`{r.id}` {r.process_ms:.0f}ms"
        for r in timed
        if r.id in steps and steps[r.id].cost_class == "engine" and (r.process_ms or 0.0) > ENGINE_BAR_MS
    ]
    if over:
        parts.append("engine over 500ms: " + ", ".join(over))
    return ["- " + "; ".join(parts)]


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
    rows = [_row(r) for r in ledger_rows]
    by_id = {r.id: r for r in rows}
    spec_by_id = {s.id: s for s in steps}
    missing = [s.id for s in steps if s.id not in by_id]
    if missing:
        raise ValueError(f"briefing: manifest steps with no ledger row: {', '.join(missing)}")

    sections: dict[str, list[str]] = {name: [] for name in SECTION_ORDER}
    sections[FAILED] = _failed(rows, by_id, spec_by_id)
    sections[DECIDED] = _decided(decisions)
    for s in steps:
        r = by_id[s.id]
        if r.status != "ran" or not r.stdout.strip():
            continue
        name = _bare_heading(s.heading)
        _check_authored(name)
        if name not in sections:
            raise ValueError(f"briefing: heading {name!r} is not in the section set")
        sections[name].extend(r.stdout.rstrip().splitlines())

    obs = [f"- {o}" for o in observations]
    next_move = str((envelope or {}).get("next_move") or "").strip()
    if next_move and not any(p.lower() in next_move.lower() for p in NARRATION_DENY_LIST):
        obs.append(f'- envelope output: "{next_move}"')
    sections[OBSERVATIONS] = sections[OBSERVATIONS] + obs
    sections[NOT_RUN] = _not_run(rows)
    sections[RUN_COST] = _run_cost(rows, spec_by_id)

    out = [_check_authored(f"# Workday-start briefing: {date} ({branch})")]
    for name in SECTION_ORDER:
        body = sections[name]
        if not body:
            continue
        out.append("")
        out.append(_check_authored(f"### {name}"))
        out.extend(body)
    return "\n".join(out) + "\n"
