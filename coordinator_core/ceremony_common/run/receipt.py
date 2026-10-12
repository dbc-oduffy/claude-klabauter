"""Shared receipt rendering: the record sections every ceremony's receipt carries.

Pure; no I/O. Renderer-authored text is checked against `NARRATION_DENY_LIST`; step output is
quoted as data and is not. Nothing here can emit `### Parked for you`, a question, or a command.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from coordinator_core.ceremony_common.run.contract import (
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

HEAD_RECORD: tuple[str, ...] = (FAILED, DECIDED)
TAIL_RECORD: tuple[str, ...] = (OBSERVATIONS, NOT_RUN, RUN_COST)
RECORD_SECTIONS: tuple[str, ...] = HEAD_RECORD + TAIL_RECORD

FAILURE_BACKLOG_STAGE = "failure-backlog"
ENGINE_BAR_MS = 500.0
MAX_RULE_POINTS = 3
SEPARATE_COST_CLASSES: tuple[str, ...] = ("suite", "remote")


def check_narration(text: str) -> str:
    """Return `text`; raise ValueError when it contains a deny-listed phrase."""
    low = text.lower()
    for phrase in NARRATION_DENY_LIST:
        if phrase.lower() in low:
            raise ValueError(f"receipt: renderer-authored text contains deny-listed phrase {phrase!r}")
    return text


def bare_heading(heading: str) -> str:
    return heading.strip().lstrip("#").strip()


def as_row(r: StepResult | Mapping[str, Any]) -> StepResult:
    if isinstance(r, StepResult):
        return r
    return StepResult(**{k: r[k] for k in LEDGER_ROW_KEYS if k in r})


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _backlog_id(step_id: str, rows: Mapping[str, StepResult], steps: Mapping[str, StepSpec]) -> str:
    for sid, spec in steps.items():
        if spec.stage == FAILURE_BACKLOG_STAGE and step_id in spec.args:
            row = rows.get(sid)
            if row is not None and row.status == "ran":
                return _first_line(row.stdout)
    return ""


def _failed(rows: list[StepResult], by_id: Mapping[str, StepResult], steps: Mapping[str, StepSpec]) -> list[str]:
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


def _run_cost(rows: list[StepResult], steps: Mapping[str, StepSpec]) -> list[str]:
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
    lines = ["- " + "; ".join(parts)]
    for cls in SEPARATE_COST_CLASSES:
        of_class = [r for r in timed if r.id in steps and steps[r.id].cost_class == cls]
        if of_class:
            total = sum(r.process_ms or 0.0 for r in of_class)
            lines.append(f"- {cls}: {len(of_class)} steps, {total:.0f}ms")
    return lines


def render_record(
    ledger_rows: Sequence[StepResult | Mapping[str, Any]],
    decisions: Sequence[Decision],
    observations: Sequence[str],
    *,
    expected_step_ids: Sequence[str],
    steps: Mapping[str, StepSpec] | None = None,
) -> dict[str, str]:
    """The five record sections keyed by bare heading, each "" when it has nothing to say.

    Raises ValueError when an expected step id has no ledger row. `steps` maps step id to its
    spec and supplies backlog lookup, gating and cost class.
    """
    rows = [as_row(r) for r in ledger_rows]
    by_id = {r.id: r for r in rows}
    missing = [i for i in expected_step_ids if i not in by_id]
    if missing:
        raise ValueError(f"receipt: expected steps with no ledger row: {', '.join(missing)}")
    spec_by_id = steps or {}
    lines = {
        FAILED: _failed(rows, by_id, spec_by_id),
        DECIDED: _decided(decisions),
        OBSERVATIONS: [f"- {o}" for o in observations],
        NOT_RUN: _not_run(rows),
        RUN_COST: _run_cost(rows, spec_by_id),
    }
    return {name: "\n".join(body) for name, body in lines.items()}


def emit_sections(title: str, middle: Sequence[tuple[str, Sequence[str]]], record: Mapping[str, str]) -> str:
    """Assemble: title, Failed, Decided, `middle` sections, Observations, Not run, Run cost.

    Empty sections are omitted; every authored heading and the title are narration-checked.
    """
    out = [check_narration(title)]

    def add(name: str, body: Sequence[str]) -> None:
        if not body:
            return
        out.append("")
        out.append(check_narration(f"### {name}"))
        out.extend(body)

    for name in HEAD_RECORD:
        add(name, record.get(name, "").splitlines())
    for name, body in middle:
        add(name, body)
    for name in TAIL_RECORD:
        add(name, record.get(name, "").splitlines())
    return "\n".join(out) + "\n"


def _condition_holds(condition: str, facts: Mapping[str, Any]) -> bool:
    if condition == "always":
        return True
    return bool(facts.get(condition))


def _source_lines(
    source: str,
    by_id: Mapping[str, StepResult],
    agent_results: Mapping[str, Any] | None,
    facts: Mapping[str, Any],
) -> list[str]:
    kind, _, key = source.partition(":")
    if kind == "step":
        r = by_id.get(key)
        if r is None:
            raise ValueError(f"receipt: section source names step {key!r} with no ledger row")
        return r.stdout.rstrip().splitlines() if r.status == "ran" and r.stdout.strip() else []
    if kind == "agent":
        value = (agent_results or {}).get(key)
        return str(value).rstrip().splitlines() if value else []
    if kind == "fact":
        value = facts.get(key)
        return str(value).rstrip().splitlines() if value else []
    raise ValueError(f"receipt: unknown section source {source!r}")


def render(
    spec: Any,
    ledger_rows: Sequence[StepResult | Mapping[str, Any]],
    decisions: Sequence[Decision],
    observations: Sequence[str],
    envelope: Mapping[str, Any] | None,
    agent_results: Mapping[str, Any] | None,
    *,
    facts: Mapping[str, Any],
) -> str:
    """Render a receipt for a spec with no renderer of its own.

    `spec.receipt_sections` is a literal tuple of `(heading, condition, source)`: `condition`
    is "always" or a key of `facts` that must be truthy; `source` is `step:<id>`,
    `agent:<group>` or `fact:<key>`. A section whose condition is false is omitted. Optional
    `facts` keys: `title`, `steps` (id -> StepSpec), `expected_step_ids`.
    """
    rows = [as_row(r) for r in ledger_rows]
    by_id = {r.id: r for r in rows}
    steps = facts.get("steps") or {}
    record = render_record(
        rows, decisions, observations,
        expected_step_ids=list(facts.get("expected_step_ids") or ()),
        steps=steps,
    )
    next_move = str((envelope or {}).get("next_move") or "").strip()
    if next_move and not any(p.lower() in next_move.lower() for p in NARRATION_DENY_LIST):
        extra = f'- envelope output: "{next_move}"'
        record[OBSERVATIONS] = (record[OBSERVATIONS] + "\n" + extra).strip("\n")
    middle: list[tuple[str, list[str]]] = []
    for heading, condition, source in spec.receipt_sections:
        name = bare_heading(heading)
        if name in RECORD_SECTIONS:
            raise ValueError(f"receipt: heading {name!r} is a fixed record section")
        check_narration(name)
        if not _condition_holds(condition, facts):
            continue
        middle.append((name, _source_lines(source, by_id, agent_results, facts)))
    title = facts.get("title") or f"# {spec.name} receipt"
    return emit_sections(title, middle, record)
