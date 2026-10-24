"""The /workweek-start run as data: one `SPEC` for the shared ceremony driver.

Launch steps read and measure; the week's priorities and the HEADER marker are judgment points
the run decides itself (rules first, then one agent pick from a closed candidate list); the
finish steps those decisions open scaffold goals, write the fragment, move HEADER, commit, and
chain into workday-start. Nothing waits on a person.

Contracts a reader would trip on:
- Step args may carry `{repo_root}`; the driver expands it, as workday-start's manifest does.
- An `inproc` target is called as `fn(repo_root)`. A step that needs parameters names a
  parameter-bound target (`_bound`), which `__getattr__` resolves to a closure.
- A priorities decision that cannot be acted on becomes one failing gate step, so the ledger
  carries a `failed` row and every later step ends `halted-by-gate`.
"""
from __future__ import annotations

import base64
import dataclasses
import json
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from coordinator_core.daily_day import local_day
from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point
from coordinator_core.ceremony_common.run import decide, receipt
from coordinator_core.ceremony_common.run.contract import Decision, StepSpec
from coordinator_core.ceremony_common.run.spec import CeremonySpec
from coordinator_core.session import record_homes
from coordinator_core.workweek_start import header

_GOALS_REL = Path(record_homes.home_dir(".", "goals")).as_posix()

COSTS_PATH = Path(__file__).resolve().parent / "step_costs.json"
_MODULE = "coordinator_core.workweek_start.run_spec"

PRIORITIES_POINT = "priorities"
MARKER_POINT = "j-week-marker-freshness"
PRIORITIES_QUESTION = "Given last week's results and current state, what are 1–3 priorities for this week?"
MAX_PRIORITIES = 3

STEP_BRIEF = "w_step1_brief_week"
STEP_HEADER_BOOTSTRAP = "w_step0_header_bootstrap"
STEP_UNFINISHED = "w_step0_unfinished_priorities"
STEP_SCRATCH_DRY = "w_step1_scratch_hygiene"
STEP_SCRATCH_APPLY = "w_step1_scratch_hygiene_apply"
STEP_COVERAGE = "w_step7_goal_coverage_scan"
STEP_EXPIRED_GATES = "w_step1_expired_plan_gates"
STEP_NUDGE = "w_step6_competitor_nudge"
STEP_HOOK = "w_step9_ceremony_hook"
STEP_CHAIN = "w_step10_chain_workday_start"
GATE_STEP = "priorities-gate"

LAUNCH_STAGES: tuple[str, ...] = ("header", "orient", "hygiene", "summary", "scan")
FINISH_STAGES: tuple[str, ...] = (
    "scratch-apply", "priorities", "header-move", "goal-event", "commit", "hook", "maintenance", "chain",
)

CHAIN: tuple[str, ...] = ("workday-start-run", "launch")

# One template for every ceremony; it names no step, stage or group. Equal to workday-start's.
TEMPLATE_RELPATH = "workflows/workday-start.mjs"

_BRIEF_DIRECTIVE_IDS: frozenset[str] = frozenset({
    "d-branch-span-mismatch", "d-claude-klabauter-bin-sentinel", "d-working-repo-registration", "d-hook-currency",
    "d-git-perf-currency", "d-ceremony-hook-output", "d-workday-marker-write", "d-plugin-drift",
    "d-goal-coverage", "d-workweek-trail-scope", "d-handoff-triage-ready", "d-handoff-triage-awaiting-gate",
    "d-plan-orphan-tiers", "d-rag-staleness-regen",
})
_BRIEF_DIRECTIVE_PREFIXES: tuple[str, ...] = ("d-git-maintenance-", "d-addon-health-", "d-worktree-reap-")
_ORPHAN_DIRECTIVE = "d-plan-orphan-tiers"

ENGINE_GAP_STEPS: Mapping[str, str] = {
    "w_step3_prior_week_digest": "No engine-computed prior-week digest yet — see `state/week-changelog/*.md` for the raw record.",
    "w_step4_stalled_workstreams": "No engine-computed staleness signal yet.",
}
_ENGINE_GAP_TARGET = "engine-gap"


def _load_costs() -> dict[str, str]:
    try:
        rows = json.loads(COSTS_PATH.read_text(encoding="utf-8"))["steps"]
    except (OSError, ValueError, KeyError):
        return {}
    return {sid: row["cost_class"] for sid, row in rows.items()}


COSTS: dict[str, str] = {**_load_costs(), **{sid: "over-bar" for sid in ENGINE_GAP_STEPS}}


def _cost(step_id: str) -> str:
    return COSTS.get(step_id) or COSTS.get(re.sub(r"_\d+$", "", step_id), "unmeasured")


def stage_of(directive_id: str) -> str:
    """Stage of a week-brief directive; raises on an id the brief is not known to emit."""
    if directive_id in _BRIEF_DIRECTIVE_IDS or directive_id.startswith(_BRIEF_DIRECTIVE_PREFIXES):
        return "orient"
    raise ValueError(f"workweek-start: no stage for brief directive {directive_id!r}")


# --- in-process adapters -------------------------------------------------------------------

def _date_in(line_re: str, text: str) -> str | None:
    m = re.search(rf"^\*\*{line_re}:\*\*[ \t]*(\d{{4}}-\d{{2}}-\d{{2}})", text, re.MULTILINE)
    return m.group(1) if m else None


def header_state(repo_root: Path) -> dict[str, Any] | None:
    """`{week_starting, last_start, unset}` from HEADER.md, or None when it cannot be read."""
    try:
        text = (Path(repo_root) / header.WEEK_DIR / "HEADER.md").read_text(encoding="utf-8")
    except OSError:
        return None
    week = _date_in("Week starting", text)
    released = re.search(r"^\*\*Prior week released:\*\*[ \t]*(.*)$", text, re.MULTILINE)
    day = local_day(str(repo_root))
    return {
        "week_starting": week,
        "last_start": _date_in("Last /workweek-start", text),
        "unset": week is None and header.UNSET_SENTINEL in text,
        "missed_weeks": list(header.missed_weeks(Path(repo_root), day)),
        "missed_as_of": str(day),
        "missed_as_of_week": header.current_week_start(Path(repo_root), day),
        "release_recorded": bool(released and "(none recorded)" not in released.group(1)
                                 and released.group(1).strip()),
    }


def _goal_open(root: Path, goal_path: str) -> bool:
    try:
        doc = yaml.safe_load((root / goal_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False
    return isinstance(doc, dict) and doc.get("status") == "active"


def bootstrap_step(repo_root: Path) -> tuple[int, str]:
    wrote = header.bootstrap(Path(repo_root))
    return 0, "created HEADER.md" if wrote else ""


def unfinished_step(repo_root: Path) -> tuple[int, str]:
    """Read-only: the unchecked fragment lines with their goal's open state, plus the HEADER state, as JSON."""
    root = Path(repo_root)
    rows = []
    for title, goal_path, fragment, number in header.unfinished_priorities(root):
        try:
            rel = Path(fragment).relative_to(root).as_posix()
        except ValueError:
            rel = Path(fragment).as_posix()
        rows.append({
            "title": title, "goal_path": goal_path, "evidence": f"{rel}:{number}",
            "goal_open": _goal_open(root, goal_path),
        })
    return 0, json.dumps({"rows": rows, "header": header_state(root)})


def recheck_step(repo_root: Path) -> tuple[int, str]:
    from coordinator_core.workday_start import probes

    return probes.recheck_due(Path(repo_root), 7)


def _write_fragment(repo_root: Path, *, sid: str, priorities: list[list[str]]) -> tuple[int, str]:
    path = header.write_priorities_fragment(Path(repo_root), sid, [(t, g) for t, g in priorities])
    return 0, path.name


def _reset(repo_root: Path, *, today: str) -> tuple[int, str]:
    archived = header.reset_week(Path(repo_root), today)
    return 0, f"archived week {', '.join(archived)}" if archived else "archived no week"


def _start_week(repo_root: Path, *, today: str) -> tuple[int, str]:
    """HEADER carries the unset sentinel (a complete already archived): set the week, archive nothing."""
    start = header.current_week_start(Path(repo_root), today)
    path = Path(repo_root) / header.WEEK_DIR / "HEADER.md"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for i, line in enumerate(lines):
        if header._WEEK_STARTING_RE.match(line):
            lines[i] = f"**Week starting:** {start}\n"
            break
    header._write(path, "".join(lines))
    header.update_in_place(Path(repo_root), today)
    return 0, f"week starts {start}"


def _update(repo_root: Path, *, today: str) -> tuple[int, str]:
    header.update_in_place(Path(repo_root), today)
    return 0, f"updated Last /workweek-start {today}"


def _fail(repo_root: Path, *, reason: str) -> tuple[int, str]:
    return 1, reason


_BOUND: Mapping[str, Callable[..., tuple[int, str]]] = {
    "write_fragment": _write_fragment, "reset": _reset, "start_week": _start_week,
    "update": _update, "fail": _fail,
}


def _bound(fn: str, **params: Any) -> str:
    token = base64.urlsafe_b64encode(json.dumps(params, sort_keys=True).encode()).decode().rstrip("=")
    return f"{_MODULE}:{fn}~{token}"


def __getattr__(name: str) -> Callable[[Path], tuple[int, str]]:
    key, sep, token = name.partition("~")
    if not sep or key not in _BOUND:
        raise AttributeError(name)
    params = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    impl = _BOUND[key]
    return lambda repo_root: impl(repo_root, **params)


# --- steps ---------------------------------------------------------------------------------

def _step(
    step_id: str, kind: str, target: str, args: Sequence[str], *, stage: str, detail: str,
    heading: str = "Observations", **kw: Any,
) -> StepSpec:
    return StepSpec(
        id=step_id, stage=stage, kind=kind, target=target, args=tuple(args), depends_on=None,
        already_satisfied=False, detail=detail, heading=heading, cost_class=_cost(step_id), **kw,
    )


def _cli(step_id: str, target: str, args: Sequence[str], *, stage: str, detail: str, **kw: Any) -> StepSpec:
    return _step(step_id, "cli", target, args, stage=stage, detail=detail, **kw)


def _inproc(step_id: str, fn: str, *, stage: str, detail: str, **kw: Any) -> StepSpec:
    return _step(step_id, "inproc", fn if ":" in fn else f"{_MODULE}:{fn}", (), stage=stage, detail=detail, **kw)


def _hygiene_op(apply: bool) -> StepSpec:
    params = json.dumps({"repo_root": "{repo_root}", "cadence": "workweek_start", "apply": apply})
    sid = STEP_SCRATCH_APPLY if apply else STEP_SCRATCH_DRY
    return _step(sid, "op", "fleet.scratch_hygiene", (params,), stage="scratch-apply" if apply else "hygiene",
                 detail=f"Scratch purge, {'apply' if apply else 'dry run'}.")


def _gap_step(step_id: str) -> StepSpec:
    return _step(step_id, "inproc", _ENGINE_GAP_TARGET, (), stage="summary",
                 detail=f"not-composed: engine-gap. {ENGINE_GAP_STEPS[step_id]}")


def _recheck() -> StepSpec:
    from coordinator_core.workday_start import probes

    if hasattr(probes, "recheck_due"):
        return _inproc("w_step5_recheck_due", "recheck_step", stage="summary", detail="Step 5: rechecks due within 7 days.")
    return dataclasses.replace(
        _step("w_step5_recheck_due", "inproc", _ENGINE_GAP_TARGET, (), stage="summary",
              detail="not-composed: engine-gap. tasks.recheck_due_this_week has no producer."),
        cost_class="over-bar",
    )


def _front_door() -> tuple[StepSpec, ...]:
    return (
        _inproc(STEP_HEADER_BOOTSTRAP, "bootstrap_step", stage="header", detail="Step 0: HEADER.md when absent."),
        _inproc(STEP_UNFINISHED, "unfinished_step", stage="header", detail="Unfinished priorities and HEADER state."),
        _cli(STEP_BRIEF, "orient-assemble", ("brief", "--cadence", "week"), stage="orient",
             detail="Step 1: the week brief."),
    )


def _residue() -> tuple[StepSpec, ...]:
    return (
        _cli(STEP_EXPIRED_GATES, "expired-plan-gates", (), stage="hygiene", detail="Step 1: expired plan gates."),
        _cli("w_step1_anchor_freshness", "check-anchor-freshness", (), stage="hygiene", detail="Step 1: anchor freshness."),
        _hygiene_op(False),
        _cli("w_step2_exec_summary", "generate-exec-summary", (), stage="summary", detail="Step 2: executive summary."),
        _gap_step("w_step3_prior_week_digest"),
        _gap_step("w_step4_stalled_workstreams"),
        _recheck(),
        _cli(STEP_NUDGE, "check-competitor-positioning-nudge", (), stage="summary", detail="Step 6: positioning nudge."),
        _cli(STEP_COVERAGE, "goal-coverage-scan", ("--format", "text"), stage="scan", detail="Step 7: goal coverage."),
    )


# --- rows and parsing ----------------------------------------------------------------------

def _rows(ledger_rows: Sequence[Any]) -> dict[str, Any]:
    return {r.id: r for r in (receipt.as_row(x) for x in ledger_rows)}


def _ran_stdout(rows: Mapping[str, Any], step_id: str) -> str | None:
    row = rows.get(step_id)
    return row.stdout if row is not None and row.status == "ran" else None


def _unfinished(rows: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    out = _ran_stdout(rows, STEP_UNFINISHED)
    try:
        obj = json.loads(out) if out else {}
    except ValueError:
        obj = {}
    return list(obj.get("rows") or []), obj.get("header")


def _scan_goals(text: str) -> list[dict[str, Any]]:
    goals: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.startswith("GOAL: "):
            title = line[len("GOAL: "):]
            zero = title.endswith(" [ZERO COVERAGE]")
            goals.append({"title": title.removesuffix(" [ZERO COVERAGE]"), "zero": zero, "path": None, "covers": []})
        elif goals and line.startswith("  path: "):
            goals[-1]["path"] = line[len("  path: "):].strip()
        elif goals and line.startswith("  - "):
            goals[-1]["covers"].append(line[4:].strip())
    return goals


_P1_LINE = re.compile(r"^P1 authorized_orphan:\s*(?P<path>\S+)")


def _directive_detail(envelope: Mapping[str, Any], directive_id: str) -> str:
    for d in envelope.get("directives") or ():
        if d.get("id") == directive_id:
            return str(d.get("detail") or "")
    return ""


def _stem(path: str) -> str:
    return Path(path).stem


def _candidates(
    carried: Sequence[Mapping[str, Any]], envelope: Mapping[str, Any], rows: Mapping[str, Any],
    *, carried_claimed: bool,
) -> list[dict[str, Any]]:
    """Preference order: carried, active goals, P1 orphans. Deduplicated by title."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(title: str, source: str, evidence: str, goal_path: str | None) -> None:
        if title and title not in seen:
            seen.add(title)
            out.append({"title": title, "source": source, "evidence": evidence, "goal_path": goal_path})

    if not carried_claimed:
        for r in carried:
            add(r["title"], "carried", r["evidence"], r["goal_path"])
    scan = _ran_stdout(rows, STEP_COVERAGE)
    goals = _scan_goals(scan) if scan else []
    for g in goals:
        add(g["title"], "active-goal", f"goal-coverage-scan: {g['path']}", g["path"])
    for line in _directive_detail(envelope, _ORPHAN_DIRECTIVE).splitlines():
        m = _P1_LINE.match(line)
        if m:
            add(_stem(m["path"]), "p1-orphan", f"{_ORPHAN_DIRECTIVE}: {m['path']}", None)
    return out


def _supplied(flags: Any) -> list[str]:
    """Titles typed as `--priority`: from a mapping (`priority`/`--priority`) or an argv sequence."""
    if isinstance(flags, Mapping):
        value = flags.get("priority", flags.get("--priority"))
        if value is None:
            return []
        return [value] if isinstance(value, str) else [str(v) for v in value]
    titles: list[str] = []
    tokens = list(flags or ())
    for i, tok in enumerate(tokens):
        if tok == "--priority" and i + 1 < len(tokens):
            titles.append(str(tokens[i + 1]))
        elif isinstance(tok, str) and tok.startswith("--priority="):
            titles.append(tok.split("=", 1)[1])
    return titles


def _priorities_point(
    supplied: Sequence[str], carried: Sequence[Mapping[str, Any]], candidates: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    pairs = json.dumps([[c["title"], c["evidence"], c["goal_path"]] for c in candidates])
    point = build_judgment_point(
        None,
        id=PRIORITIES_POINT,
        question=PRIORITIES_QUESTION,
        dispositions=[build_disposition("set")],
        evidence=f"supplied={len(supplied)} carried={len(carried)} candidates={len(candidates)} | candidates={pairs}",
        reason="week-priorities",
    )
    point["supplied"] = list(supplied)
    point["carried"] = [dict(c) for c in carried]
    point["candidates"] = [dict(c) for c in candidates]
    point["payload"] = (
        "{\"priorities\": [{\"title\": <candidate title>, \"evidence\": <that candidate's evidence>}]}, "
        f"one to {MAX_PRIORITIES} entries copied verbatim from candidates"
    )
    return point


def _missed_week_note(state: Mapping[str, Any]) -> str:
    """Names the missed weeks; adds the catch-up pointer when no release is recorded for them."""
    missed = state.get("missed_weeks") or []
    if not missed:
        return ""
    note = f" | missed weeks: {', '.join(missed)}"
    if not state.get("release_recorded"):
        note += " | no release record: run /workweek-complete --catch-up"
    return note


def _marker_point(state: Mapping[str, Any]) -> dict[str, Any]:
    point = build_judgment_point(
        None,
        id=MARKER_POINT,
        question="Does HEADER.md start a new week or update in place?",
        dispositions=[build_disposition("reset_week"), build_disposition("update_in_place")],
        evidence=f"HEADER state: {json.dumps(state, sort_keys=True)}{_missed_week_note(state)}",
        reason="recommendation-forbidden",
    )
    point["header"] = dict(state)
    return point


def spec_points(envelope: Mapping[str, Any], flags: Any, ledger_rows: Sequence[Any]) -> list[dict[str, Any]]:
    """`priorities` always, and `j-week-marker-freshness` when the brief did not emit it and HEADER was read."""
    rows = _rows(ledger_rows)
    unfinished, state = _unfinished(rows)
    carried = [r for r in unfinished if r.get("goal_open")]
    supplied = _supplied(flags)
    claimed = bool(supplied) or 1 <= len(carried) <= MAX_PRIORITIES
    points = [_priorities_point(supplied, carried, _candidates(carried, envelope, rows, carried_claimed=claimed))]
    brief_ids = {p.get("id") for p in envelope.get("judgment_points") or ()}
    if state is not None and MARKER_POINT not in brief_ids:
        points.append(_marker_point(state))
    return points


# --- rules ---------------------------------------------------------------------------------

def _rule_decision(
    point: Mapping[str, Any], rule: str, value: str, rationale: str, evidence: str,
    payload: Mapping[str, Any] | None = None,
) -> Decision:
    return Decision(
        point_id=str(point.get("id", "")), question=str(point.get("question", "")), decision=value,
        decided_by="rule", rule=rule, rationale=rationale, evidence=evidence, payload=payload,
    )


def priorities_supplied(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    if point.get("id") != PRIORITIES_POINT or not point.get("supplied"):
        return None
    titles = [str(t) for t in point["supplied"]]
    return _rule_decision(
        point, "priorities-supplied", "set",
        "Rung 1: the priorities were typed on the launch.", "launch flag --priority",
        {"priorities": [{"title": t, "evidence": "launch flag --priority"} for t in titles]},
    )


def priorities_carried(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    carried = point.get("carried") or []
    if point.get("id") != PRIORITIES_POINT or not 1 <= len(carried) <= MAX_PRIORITIES:
        return None
    return _rule_decision(
        point, "priorities-carried", "set",
        f"Rung 2: {len(carried)} unfinished prior-week priorities with an open goal carry forward; the set was not typed.",
        "; ".join(c["evidence"] for c in carried),
        {"priorities": [
            {"title": c["title"], "goal_path": c["goal_path"], "evidence": c["evidence"]} for c in carried
        ]},
    )


def priorities_no_evidence(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    if point.get("id") != PRIORITIES_POINT or point.get("candidates"):
        return None
    return _rule_decision(
        point, "priorities-no-evidence", "unresolved",
        "No supplied, carried or candidate priorities exist.", "no-priority-evidence",
        {"undecided": "no-priority-evidence"},
    )


def week_marker(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    """Reset only when HEADER carries the unset sentinel (a complete ran since the last start)."""
    if point.get("id") != MARKER_POINT:
        return None
    state = point.get("header") or context.get("header")
    if not isinstance(state, Mapping):
        return None
    value = "reset_week" if state.get("unset") else "update_in_place"
    why = (
        "HEADER `Week starting:` is unset, so a /workweek-complete ran since the last /workweek-start."
        if state.get("unset") else "HEADER shows no /workweek-complete since the last /workweek-start."
    )
    return _rule_decision(point, "week-marker", value, why, json.dumps(dict(state), sort_keys=True))


def memo_left_for_day_run(point: Mapping[str, Any], *, context: Mapping[str, Any]) -> Decision | None:
    point_id = str(point.get("id", ""))
    if not (point_id.startswith("j-memo-") or point_id == "j-overflow-memo"):
        return None
    return _rule_decision(
        point, "memo-left-for-day-run", "left-for-day-run",
        "The chained workday-start run owns the inbox.", str(point.get("evidence", "")),
    )


RULES: dict[str, Callable[..., Any]] = {
    "priorities-supplied": priorities_supplied,
    "priorities-carried": priorities_carried,
    "priorities-no-evidence": priorities_no_evidence,
    "week-marker": week_marker,
    "memo-left-for-day-run": memo_left_for_day_run,
    decide.RECOMMENDATION: decide.recommendation,
}

POINT_RULES = decide.RuleTable(
    point_rules=tuple((rule_id, fn) for rule_id, fn in RULES.items()),
    exhausted_rule="no-live-disposition",
)


# --- agent work ----------------------------------------------------------------------------

def agent_groups(envelope: Mapping[str, Any], ledger_rows: Sequence[Any], resolution: Any) -> list[dict[str, Any]]:
    entries = resolution.judgment_entries if resolution else []
    if not entries:
        return []
    return [{
        "id": "judgment", "phase": 4, "agent": decide.DEFAULT_AGENT, "ruling_agent": None, "inputs": {},
        "entries": [
            {"key": str(e["id"]), "brief": str(e.get("question", "")), "argv": None, "inputs": e} for e in entries
        ],
    }]


# --- finish steps --------------------------------------------------------------------------

def _gate(reason: str) -> StepSpec:
    return _step(
        GATE_STEP, "inproc", _bound("fail", reason=reason), (), stage="priorities", detail=reason,
        gates_next=True,
    )


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]


def _resolve_priorities(decision: Decision | None) -> tuple[list[dict[str, Any]], str]:
    """The chosen `{title, evidence, goal_path}` rows, or `([], reason)` when the decision cannot be acted on."""
    if decision is None:
        return [], "priorities-undecided"
    payload = decision.payload
    if decision.decision != "set":
        reason = payload.get("undecided") if isinstance(payload, Mapping) else None
        return [], str(reason or f"priorities-{decision.decision}")
    items = payload.get("priorities") if isinstance(payload, Mapping) else None
    if not isinstance(items, list) or not all(isinstance(i, Mapping) and i.get("title") for i in items):
        return [], "priorities-payload-missing"
    if not 1 <= len(items) <= MAX_PRIORITIES:
        return [], "priority-count"
    chosen = [{"title": str(i["title"]), "evidence": str(i.get("evidence", "")), "goal_path": i.get("goal_path")}
              for i in items]
    if decision.decided_by.startswith("agent:"):
        candidates = _evidence_candidates(decision.evidence)
        for row in chosen:
            match = next((c for c in candidates if (c[0], c[1]) == (row["title"], row["evidence"])), None)
            if match is None:
                return [], f"priority-not-a-candidate:{row['title']}"
            row["goal_path"] = match[2]
    return chosen, ""


def _evidence_candidates(evidence: str) -> list[list[Any]]:
    _, sep, raw = evidence.rpartition("candidates=")
    try:
        return json.loads(raw) if sep else []
    except ValueError:
        return []


def _scratch_apply(rows: Mapping[str, Any]) -> list[StepSpec]:
    out = _ran_stdout(rows, STEP_SCRATCH_DRY)
    try:
        caps = (json.loads(out) if out else {}).get("capabilities") or []
    except (ValueError, AttributeError):
        caps = []
    return [_hygiene_op(True)] if "hold_pending_marker" in caps else []


def _weeks_rolled_since(as_of_week: str, today: str) -> list[str]:
    """Week starts from `as_of_week` on the 7-day chain that end on or before `today`; [] on unparseable input."""
    try:
        start, day = date.fromisoformat(as_of_week), date.fromisoformat(today)
    except ValueError:
        return []
    out: list[str] = []
    while start + timedelta(days=7) <= day:
        out.append(start.isoformat())
        start += timedelta(days=7)
    return out


def finish_steps(decisions: Sequence[Decision], ledger_rows: Sequence[Any], *, run: Mapping[str, Any]) -> list[StepSpec]:
    """The steps the priorities and marker decisions open, ending in the chained workday-start launch."""
    rows = _rows(ledger_rows)
    by_point = {d.point_id: d for d in decisions}
    steps = _scratch_apply(rows)
    chosen, reason = _resolve_priorities(by_point.get(PRIORITIES_POINT))
    sid = str(run.get("session_id") or "")[:8]
    if not reason and not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        reason = "no-session-id"
    if reason:
        return [*steps, _gate(reason), *_after_gate()]
    today = str(run["day"])
    marker = by_point.get(MARKER_POINT)
    reset = marker is not None and marker.decision == "reset_week"
    _, state = _unfinished(rows)
    prior = (state or {}).get("week_starting")
    as_of = (state or {}).get("missed_as_of")
    missed = list((state or {}).get("missed_weeks") or [])
    if as_of not in (None, today):
        missed += _weeks_rolled_since(str((state or {}).get("missed_as_of_week") or ""), today)
    archive_weeks = list(dict.fromkeys([*missed, *([prior] if prior else [])]))
    archive_args = tuple(a for w in sorted(archive_weeks) for a in ("--prior-week-start", w))

    goals: list[str] = []
    used: set[str] = set()
    for i, row in enumerate(chosen, start=1):
        if row["goal_path"]:
            goals.append(str(row["goal_path"]))
            continue
        slug = _slug(row["title"])
        if slug in used:
            slug = f"{slug[:36]}-{i}"
        used.add(slug)
        out = f"{_GOALS_REL}/{today}-{slug}-{sid}.yaml"
        row["goal_path"] = out
        goals.append(out)
        steps.append(_cli(
            "w_step7_scaffold_goal" + ("" if len(used) == 1 else f"_{len(used)}"), "workweek-start-goal-and-priorities",
            ("scaffold-goal", "--title", row["title"], "--sid-short", sid, "--out", out),
            stage="priorities", detail="Step 7: weekly goal artifact.",
        ))
    steps.append(_step(
        "w_step7_write_fragment", "inproc",
        _bound("write_fragment", sid=sid, priorities=[[r["title"], r["goal_path"]] for r in chosen]), (),
        stage="priorities", detail="Step 7: this session's priorities fragment.",
    ))
    if reset and prior:
        steps.append(_step("w_step8_reset_week", "inproc", _bound("reset", today=today), (),
                           stage="header-move", detail="Step 8: archive the closing week."))
    elif reset:
        steps.append(_step("w_step8_start_week", "inproc", _bound("start_week", today=today), (),
                           stage="header-move", detail="Step 8: start the week."))
    else:
        steps.append(_step("w_step8_update_in_place", "inproc", _bound("update", today=today), (),
                           stage="header-move", detail="Step 8: update the marker in place."))
    for i, goal in enumerate(goals, start=1):
        steps.append(_cli(
            "w_step8_emit_goal_event" + ("" if i == 1 else f"_{i}"), "workweek-start-goal-and-priorities",
            ("emit-goal-event", "--goal", goal), stage="goal-event", detail="Step 8: weekly goal event.",
        ))
    steps.append(_cli("w_step8_commit_priorities", "workweek-start-goal-and-priorities",
                      ("commit-priorities", "--sid-short", sid), stage="commit", detail="Step 8: commit and push."))
    if reset and prior:
        steps.append(_cli("w_step8_commit_archive_reset", "workweek-start-goal-and-priorities",
                          ("commit-archive-reset", *archive_args), stage="commit",
                          detail="Step 8: commit the archived week."))
    steps.append(_cli(STEP_HOOK, "workweek-start-goal-and-priorities", ("ceremony-hook", "--ceremony", "workweek-start"),
                      stage="hook", detail="Step 9: post-ceremony hook."))
    steps.append(_step(
        "w_step9_5_git_maintenance_weekly", "op", "git.maintenance",
        (json.dumps({"tier": "weekly", "repo": "{repo_root}"}),), stage="maintenance",
        detail="Step 9.5: maintenance checkpoint.", best_effort=True,
    ))
    steps.append(_cli(STEP_CHAIN, CHAIN[0], CHAIN[1:], stage="chain", detail="Step 10: chain into workday-start."))
    return steps


def _after_gate() -> list[StepSpec]:
    """The steps an undecided run owes, so each ends `halted-by-gate` behind the failed gate."""
    return [
        _cli("w_step8_commit_priorities", "workweek-start-goal-and-priorities", ("commit-priorities",),
             stage="commit", detail="Step 8: commit and push."),
        _cli(STEP_HOOK, "workweek-start-goal-and-priorities", ("ceremony-hook", "--ceremony", "workweek-start"),
             stage="hook", detail="Step 9: post-ceremony hook."),
        _step("w_step9_5_git_maintenance_weekly", "op", "git.maintenance",
              (json.dumps({"tier": "weekly", "repo": "{repo_root}"}),), stage="maintenance",
              detail="Step 9.5: maintenance checkpoint.", best_effort=True),
        _cli(STEP_CHAIN, CHAIN[0], CHAIN[1:], stage="chain", detail="Step 10: chain into workday-start."),
    ]


# --- receipt -------------------------------------------------------------------------------

RECEIPT_SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("Handoff/health advisories", "advisories", "fact:advisories"),
    ("Orphaned plans", "orphans", "fact:orphans"),
    ("This week's priorities", "priorities", "fact:priorities"),
    ("HEADER.md", "header_action", "fact:header_action"),
    ("Post-ceremony hook", "hook_out", "fact:hook_out"),
)


def _first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")


def _observations(rows: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    gates = _ran_stdout(rows, STEP_EXPIRED_GATES)
    if gates and gates.strip():
        out += [f"expired plan gates: {ln.strip()}" for ln in gates.splitlines() if ln.strip()]
    nudge = _ran_stdout(rows, STEP_NUDGE)
    if nudge and nudge.strip():
        out.append(f"positioning nudge: {_first_line(nudge)}")
    scan = _ran_stdout(rows, STEP_COVERAGE)
    out += [f"goal with no in-flight work: {g['title']}" for g in (_scan_goals(scan) if scan else []) if g["zero"]]
    return out


def render(
    spec: Any, ledger_rows: Sequence[Any], decisions: Sequence[Decision], observations: Sequence[str],
    envelope: Mapping[str, Any] | None, agent_results: Mapping[str, Any] | None, *, facts: Mapping[str, Any],
) -> str:
    rows = _rows(ledger_rows)
    env = envelope or {}
    by_point = {d.point_id: d for d in decisions}
    chosen, reason = _resolve_priorities(by_point.get(PRIORITIES_POINT))
    marker = by_point.get(MARKER_POINT)
    advisories = [
        f"- {_first_line(str(d.get('detail') or ''))}"
        for d in env.get("directives") or ()
        if d.get("id") != _ORPHAN_DIRECTIVE and _first_line(str(d.get("detail") or ""))
    ]
    merged = dict(facts)
    merged.update({
        "advisories": "\n".join(advisories),
        "orphans": _directive_detail(env, _ORPHAN_DIRECTIVE).strip(),
        "priorities": "\n".join(f"- [ ] {r['title']}" for r in chosen) if not reason else "",
        "header_action": (
            "" if reason or (marker is None and PRIORITIES_POINT not in by_point)
            else "reset (archived prior week)" if marker is not None and marker.decision == "reset_week"
            else "updated in place"
        ),
        "hook_out": _first_line(_ran_stdout(rows, STEP_HOOK) or ""),
    })
    return receipt.render(
        spec, ledger_rows, decisions, [*observations, *_observations(rows)], envelope, agent_results, facts=merged,
    )


# --- the spec ------------------------------------------------------------------------------

CONSUMES_MANIFEST: frozenset[str] = frozenset({
    "orient-assemble", "expired-plan-gates", "check-anchor-freshness", "generate-exec-summary",
    "check-competitor-positioning-nudge", "workweek-start-goal-and-priorities", "goal-coverage-scan",
    "coordinator-invoke", "workday-start-run",
})

SPEC = CeremonySpec(
    name="workweek-start",
    dispatch_key="workweek_start_run",
    run_dir_name="workweek-start",
    template=TEMPLATE_RELPATH,
    marker_path=None,
    launch_stages=LAUNCH_STAGES,
    finish_stages=FINISH_STAGES,
    front_door=_front_door(),
    envelope_step=STEP_BRIEF,
    stage_of=stage_of,
    residue=_residue(),
    rules=RULES,
    spec_points=spec_points,
    agent_groups=agent_groups,
    finish_steps=finish_steps,
    receipt_sections=RECEIPT_SECTIONS,
    render=render,
    costs=COSTS,
    chain=CHAIN,
    launch_flags=frozenset({"--priority"}),
)
