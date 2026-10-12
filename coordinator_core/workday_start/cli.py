"""workday-start-run: one deterministic launch for the whole workday-start ceremony.

`launch` runs every mechanical stage in-process under the stage runner, resolves each judgment
point by rule or routes it to an agent, and either finishes the run itself (no agent work) or
binds the digest onto the workflow template and prints the one `Workflow(...)` line. `finish`
runs the tail: decisions, failure backlog, briefing, marker and caches. Nothing a person
supplies continues a run.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from coordinator_core.contract import apply_base
from coordinator_core.workday_start import backfill, briefing, decide, emit, ledger, manifest, rules, single_flight
from coordinator_core.workday_start.contract import (
    EXIT_GATE_FAILED,
    EXIT_IN_FLIGHT,
    EXIT_OK,
    EXIT_TEMPLATE_MISSING,
    EXIT_USAGE,
    FINISH_STAGES,
    LAUNCH_STAGES,
    Decision,
    StepResult,
    StepSpec,
    build_digest,
    step_from_directive,
    validate_agent_results,
)
from coordinator_core.workday_start.runner import ChildOutcome, default_dispatch, run_stages

CEREMONY = "workday-start"
DISPATCH_KEY = "workday_start"
PROG = "workday-start-run"

FAILURE_ROWS_FILED_MAX = 10

TEMPLATE_RELPATH = "workflows/workday-start.mjs"
SCRIPT_FILE = "workflow.mjs"
STATE_FILE = "launch-state.json"
ENVELOPE_FILE = "envelope.json"
DIGEST_FILE = "digest.json"
AGENT_RESULTS_FILE = "agent-results.json"
BRIEFING_FILE = "briefing.md"
DONE_FILE = "finished.json"

STEP0_STEP = "step0-branch-setup"
ORIENT_STEP = "orient-assemble-brief-day"
INBOX_STEP = "memo-inbox-blitz-assemble"
DECIDE_STEP = "decide-resolve"
FRESHNESS_STEP = "fresh-commit-delta"
BACKFILL_SCAN_STEP = "weekly-backfill-scan"
ALIGNMENT_STEPS = ("priority-whats-next", "triage-query-completions")

PHASE_ONE_LAST_STAGE = "freshness"
POST_BRIEFING_STAGES = FINISH_STAGES[FINISH_STAGES.index("briefing") + 1 :]
BACKLOG_PREFIX = "backlog:"
BACKLOG_CLI = "coordinator-queue-append"
BACKLOG_SURFACE = "coordinator_core/workday_start"
BACKLOG_DIR = Path("state") / "bug-backlog"
TEMPLATE_MISSING = "template-missing"
RUN_ID_RE = re.compile(r"\d{8}T\d{6}Z-\d{6}[0-9a-f]{4}")  # the shape _mint_run_id produces

USAGE = (
    f"usage: {PROG} launch\n"
    f"       {PROG} finish --run-id <id> [--agent-results <path>]"
)

Dispatch = Mapping[str, Callable[..., ChildOutcome]]


@dataclass
class _Run:
    """Everything a run carries between its stages; the persisted subset is `launch-state.json`."""

    repo_root: Path
    run_id: str
    run_dir: Path
    lock_path: str
    lock_run_id: str
    day: str
    date: _dt.date
    branch: str
    steps: list[StepSpec] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    captured: dict[str, ChildOutcome] = field(default_factory=dict)
    envelope: "dict[str, Any] | None" = None
    inbox: "dict[str, Any] | None" = None
    resolution: "decide.Resolution | None" = None
    halted_by: "str | None" = None


def _spec_to_json(spec: StepSpec) -> dict[str, Any]:
    out = dataclasses.asdict(spec)
    out["args"] = list(spec.args)
    out["on_exit"] = {str(k): v for k, v in spec.on_exit.items()}
    return out


def _spec_from_json(obj: Mapping[str, Any]) -> StepSpec:
    return StepSpec(**{**obj, "args": tuple(obj["args"]), "on_exit": {int(k): v for k, v in obj["on_exit"].items()}})


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(run: _Run) -> None:
    _write_json(
        run.run_dir / STATE_FILE,
        {
            "run_id": run.run_id,
            "repo_root": run.repo_root.as_posix(),
            "lock_path": run.lock_path,
            "lock_run_id": run.lock_run_id,
            "day": run.day,
            "branch": run.branch,
            "steps": [_spec_to_json(s) for s in run.steps],
            "observations": run.observations,
        },
    )


def _load_state(repo_root: Path, run_dir: Path) -> _Run:
    obj = _read_json(run_dir / STATE_FILE)
    envelope_path = run_dir / ENVELOPE_FILE
    return _Run(
        repo_root=repo_root,
        run_id=obj["run_id"],
        run_dir=run_dir,
        lock_path=obj["lock_path"],
        lock_run_id=obj["lock_run_id"],
        day=obj["day"],
        date=_dt.date.fromisoformat(obj["day"]),
        branch=obj["branch"],
        steps=[_spec_from_json(s) for s in obj["steps"]],
        observations=list(obj["observations"]),
        envelope=_read_json(envelope_path) if envelope_path.is_file() else None,
    )


def _mint_run_id() -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{now.microsecond:06d}{uuid.uuid4().hex[:4]}"


def _marker_path(repo_root: Path) -> Path:
    from coordinator_core.ops.check_weekly_staleness import _resolve_state_root

    root = _resolve_state_root()
    return Path(root if root else repo_root / "state") / ".workday-start-marker"


def _current_branch(repo_root: Path) -> str:
    from coordinator_core.git.git_dir import resolve_git_dir

    try:
        head = (resolve_git_dir(repo_root) / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else head[:12]


def _json_or_none(text: str) -> "dict[str, Any] | None":
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _row(step_id: str, status: str, reason: str = "", stdout: str = "") -> StepResult:
    return StepResult(id=step_id, status=status, reason=reason, stdout=stdout)


def _rules_table() -> dict[str, Callable[..., Any]]:
    """The runner's rule table with the conflicted paths appended to an unresolved reconcile reason."""
    table = dict(rules.RULES)
    inner = table["step0-reconcile-outcome"]

    def reconcile(step: Any, result: Any, *, repo_root: Path, run_dir: Path) -> "bool | str":
        out = inner(step, result, repo_root=repo_root, run_dir=run_dir)
        if isinstance(out, str):
            paths = rules._paths_after_marker(result.stderr or "")
            if paths:
                out = f"{out} paths:{','.join(paths)}"
        return out

    table["step0-reconcile-outcome"] = reconcile
    return table


def _orient_hook(run: _Run, outcome: ChildOutcome) -> ChildOutcome:
    envelope = _json_or_none(outcome.stdout)
    if outcome.exit_code != 0:
        return outcome
    if envelope is None:
        return ChildOutcome(1, outcome.process_ms, outcome.spawns, "", outcome.stderr, "envelope-not-json")
    run.envelope = envelope
    _write_json(run.run_dir / ENVELOPE_FILE, envelope)
    points = len(envelope.get("judgment_points") or [])
    directives = len(envelope.get("directives") or [])
    return dataclasses.replace(outcome, stdout=f"orient: {directives} directive(s), {points} judgment point(s)")


def _inbox_hook(run: _Run, outcome: ChildOutcome) -> ChildOutcome:
    if outcome.exit_code != 0:
        return outcome
    inbox = _json_or_none(outcome.stdout)
    if inbox is None:
        return ChildOutcome(1, outcome.process_ms, outcome.spawns, "", outcome.stderr, "assembler-output-not-json")
    run.inbox = inbox
    points = (run.envelope or {}).get("judgment_points") or []
    if inbox.get("state") == "skipped" and any(str(p.get("id", "")).startswith("j-memo-") for p in points):
        return ChildOutcome(1, outcome.process_ms, outcome.spawns, "", outcome.stderr, "skipped-with-open-memos")
    dispatches = inbox.get("dispatches") or []
    memos = sum(len(d.get("memos") or []) for d in dispatches)
    return dataclasses.replace(
        outcome, stdout=f"inbox {inbox.get('state', 'unknown')}: {len(dispatches)} dispatch(es), {memos} memo(s)"
    )


def _decide_outcome(run: _Run) -> ChildOutcome:
    envelope = run.envelope or {}
    run.resolution = decide.resolve(
        envelope.get("judgment_points") or [],
        envelope.get("directives") or [],
        rules=rules.POINT_RULES,
        context={"inbox": run.inbox or {}},
    )
    decide.record(run.run_dir, run.resolution.decisions)
    r = run.resolution
    text = (
        f"{len(r.decisions)} judgment point(s) decided by rule, "
        f"{len(r.judgment_entries)} routed to agents, {len(r.observations)} reported"
    )
    return ChildOutcome(0, spawns=0, stdout=text)


def _wrap_dispatch(base: Dispatch, run: _Run) -> dict[str, Callable[..., ChildOutcome]]:
    """Capture each step's full outcome and apply the per-step hooks; the runner clips what it ledgers."""
    hooks: dict[str, Callable[[_Run, ChildOutcome], ChildOutcome]] = {
        ORIENT_STEP: _orient_hook,
        INBOX_STEP: _inbox_hook,
    }
    table: dict[str, Callable[..., ChildOutcome]] = dict(base)

    def wrap(inner: Callable[..., ChildOutcome]) -> Callable[..., ChildOutcome]:
        def call(step: StepSpec, *, repo_root: Path, run_dir: Path, deadline_s: float) -> ChildOutcome:
            if step.id == DECIDE_STEP:
                outcome = _decide_outcome(run)
            else:
                outcome = inner(step, repo_root=repo_root, run_dir=run_dir, deadline_s=deadline_s)
            run.captured[step.id] = outcome
            hook = hooks.get(step.id)
            return hook(run, outcome) if hook else outcome

        return call

    for kind in ("cli", "op", "inproc"):
        table[kind] = wrap(base[kind])
    return table


def _stages_upto(last: str) -> tuple[str, ...]:
    return LAUNCH_STAGES[: LAUNCH_STAGES.index(last) + 1]


def _stages_after(last: str) -> tuple[str, ...]:
    return LAUNCH_STAGES[LAUNCH_STAGES.index(last) + 1 :]


def _run_phase(run: _Run, stages: Sequence[str], table: Dispatch) -> None:
    freshness = run.captured.get(FRESHNESS_STEP)
    owed = manifest.steps_for(
        stages, repo_root=run.repo_root, today=run.date,
        freshness={"stdout": freshness.stdout} if freshness else None,
    )
    skipped = manifest.cadence_skipped(
        stages, repo_root=run.repo_root, today=run.date,
        freshness={"stdout": freshness.stdout} if freshness else None,
    )
    if run.halted_by is not None:
        for spec in owed:
            ledger.append(run.run_dir, _row(spec.id, "halted-by-gate", run.halted_by))
    else:
        results = run_stages(
            owed, ceremony=DISPATCH_KEY, repo_root=run.repo_root, run_dir=run.run_dir,
            dispatch=table, rules=_rules_table(),
        )
        gating = {s.id for s in owed if s.gates_next}
        run.halted_by = next(
            (r.id for r in results if r.id in gating and r.status in ("failed", "not-composed")), None
        )
    for spec, reason in skipped:
        ledger.append(run.run_dir, _row(spec.id, "cadence-skipped", reason))
    run.steps.extend(owed)
    run.steps.extend(spec for spec, _ in skipped)


def _row_status(run_dir: Path, step_id: str) -> str:
    rows = [r for r in ledger.read(run_dir) if r["id"] == step_id]
    return rows[-1]["status"] if rows else ""


def _build_groups(run: _Run) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    dispatches = (run.inbox or {}).get("dispatches") or []
    if dispatches:
        entries, seen = [], set()
        for i, d in enumerate(dispatches, start=1):
            key = str(d.get("id") or d.get("bucket") or f"inbox-{i}")
            key = key if key not in seen else f"{key}-{i}"
            seen.add(key)
            entries.append(
                {"key": key, "brief": d.get("brief"), "argv": None,
                 "inputs": {k: v for k, v in d.items() if k != "brief"}}
            )
        groups.append({
            "id": "inbox", "phase": 1, "agent": "coordinator:executor", "ruling_agent": "coordinator:apm",
            "inputs": {"supersession_candidates": (run.inbox or {}).get("supersession_candidates") or []},
            "entries": entries,
        })
    scan = run.captured.get(BACKFILL_SCAN_STEP)
    if scan is not None and _row_status(run.run_dir, BACKFILL_SCAN_STEP) == "ran":
        planned, reason = backfill.plan_with_reason(scan.stdout, "")
        if reason:
            ledger.append(run.run_dir, _row("backfill-plan", "failed", reason))
        elif planned:
            groups.append({
                "id": "backfill", "phase": 2, "agent": None, "ruling_agent": None, "inputs": {},
                "entries": planned,
            })
    ran = [
        s for s in ALIGNMENT_STEPS
        if s in run.captured and run.captured[s].stdout.strip() and _row_status(run.run_dir, s) == "ran"
    ]
    if ran:
        groups.append({
            "id": "alignment", "phase": 3, "agent": None, "ruling_agent": None, "inputs": {},
            "entries": [{
                "key": "alignment", "brief": None, "argv": None,
                "inputs": {"ledger": str(run.run_dir / ledger.LEDGER_FILE), "steps": ran},
            }],
        })
    entries_j = (run.resolution.judgment_entries if run.resolution else [])
    if entries_j:
        groups.append({
            "id": "judgment", "phase": 4, "agent": "coordinator:apm", "ruling_agent": None, "inputs": {},
            "entries": [
                {"key": str(e["id"]), "brief": str(e.get("question", "")), "argv": None, "inputs": e}
                for e in entries_j
            ],
        })
    return groups


def _read_decisions(run_dir: Path) -> list[Decision]:
    path = run_dir / "decisions.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            row["acted"] = tuple(row.get("acted") or ())
            out.append(Decision(**row))
    return out


def _cost_class(bareword: str) -> str:
    return manifest._cost_rows().get(bareword, {}).get("cost_class", "unmeasured")


def _directive_steps(run: _Run, envelope: Mapping[str, Any], decisions: Sequence[Decision], table: Dispatch) -> list[StepSpec]:
    """Run each envelope directive a decision opens as a ledgered step; return the specs for the briefing."""
    from coordinator_core.ceremony_common.apply_halt import _directive_gate_open

    points = {str(p.get("id")): p for p in envelope.get("judgment_points") or []}
    directives = list(envelope.get("directives") or [])
    directive_ids = {d["id"] for d in directives}
    decided = decide.decisions_map(decisions)
    opened: dict[str, list[str]] = {}
    specs: list[StepSpec] = []
    composable: list[StepSpec] = []
    for d in directives:
        deps = [x for x in apply_base.normalize_depends_on(d.get("depends_on")) if x in decided]
        if not deps or not _directive_gate_open(d, points, decided, directive_ids):
            continue
        spec = step_from_directive(
            dict(d, id=f"directive:{d['id']}"), stage="decisions", heading=briefing.OBSERVATIONS,
            cost_class=_cost_class(d["cli"]),
        )
        specs.append(spec)
        for dep in deps:
            opened.setdefault(dep, []).append(spec.id)
        try:
            apply_base.assert_dispatchable(DISPATCH_KEY, spec.target)
        except apply_base.UnrecognizedDirective:
            ledger.append(run.run_dir, _row(spec.id, "not-composed", "not-in-dispatch-row"))
        else:
            composable.append(spec)
    if composable:
        run_stages(
            composable, ceremony=DISPATCH_KEY, repo_root=run.repo_root, run_dir=run.run_dir,
            dispatch=table, rules=_rules_table(),
        )
    for dec in decisions:
        if dec.decision not in rules.ACTING_DISPOSITIONS:
            continue
        carried = opened.get(dec.point_id, [])
        if not carried:
            ledger.append(run.run_dir, _row(f"decision:{dec.point_id}", "failed", f"no-directive-carries:{dec.decision}"))
            continue
        bad = next((s for s in (_row_status(run.run_dir, i) for i in carried) if s != "ran"), "")
        if bad:
            ledger.append(run.run_dir, _row(f"decision:{dec.point_id}", "failed", bad))
    return specs


def _memo_name(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name


def _check_inbox(run: _Run, digest: Mapping[str, Any], results: Mapping[str, Any]) -> None:
    named: list[str] = []
    for group in digest["agent_work"]["groups"]:
        if group["id"] == "inbox":
            for entry in group["entries"]:
                named.extend((entry["inputs"].get("memos") or []))
    returned: set[str] = set()
    for res in (results.get("groups") or {}).get("inbox", []):
        returned.update(res.get("memos") or [])
        if res.get("outcome") == "needs-ruling" and not res.get("decision"):
            ledger.append(run.run_dir, _row(f"inbox:{res['key']}", "failed", "needs-ruling-no-decision"))
    seen: set[str] = set()
    for memo in named:
        name = _memo_name(memo)
        if memo not in returned and name not in seen:
            seen.add(name)
            ledger.append(run.run_dir, _row(f"inbox:{name}", "failed", "no-result"))


def _open_bug_titles(repo_root: Path) -> dict[str, str]:
    """Open bug-backlog rows by lowercase title -> repo-relative path."""
    out: dict[str, str] = {}
    directory = repo_root / BACKLOG_DIR
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.yaml")):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        title = re.search(r"^title:\s*(.+?)\s*$", head, re.MULTILINE)
        status = re.search(r"^status:\s*(\S+)", head, re.MULTILINE)
        if title and status and status.group(1).strip("'\"") == "open":
            out.setdefault(title.group(1).strip("'\"").lower(), (BACKLOG_DIR / path.name).as_posix())
    return out


def _family(point_id: str) -> str:
    return re.sub(r"[-_]?\d+$", "", point_id) or point_id


def _backlog_groups(failed: Sequence[Mapping[str, Any]], run_id: str) -> list[tuple[str, str, list[str]]]:
    """`(title, reason, covered ids)`: one group per title, or one for the whole run past the cap."""
    ids = [r["id"] for r in failed]
    if len(failed) > FAILURE_ROWS_FILED_MAX:
        return [(f"{CEREMONY} run {run_id} failed {len(failed)} steps", ", ".join(ids), ids)]
    grouped: dict[str, tuple[str, list[str]]] = {}
    for r in failed:
        reason = str(r.get("reason") or "failed")
        if reason.startswith(TEMPLATE_MISSING):
            ident, reason = "workflow-template", TEMPLATE_MISSING
        elif r["id"].startswith("decision:"):
            ident = _family(r["id"].split(":", 1)[1])
        else:
            ident = r["id"]
        title = f"{CEREMONY} step {ident} failed: {reason}"
        grouped.setdefault(title, (reason, []))[1].append(r["id"])
    return [(title, reason, ids_) for title, (reason, ids_) in grouped.items()]


def _file_failures(run: _Run, table: Dispatch, filed: set[str]) -> list[StepSpec]:
    """File one bug-backlog row per failed ledger row not yet filed; an open row of the same title is cited."""
    failed = [
        r for r in ledger.read(run.run_dir)
        if r["status"] == "failed" and not r["id"].startswith(BACKLOG_PREFIX) and r["id"] not in filed
    ]
    if not failed:
        return []
    filed.update(r["id"] for r in failed)
    open_rows = _open_bug_titles(run.repo_root)
    to_run: list[StepSpec] = []
    display: list[StepSpec] = []
    for title, reason, ids in _backlog_groups(failed, run.run_id):
        step_id = f"{BACKLOG_PREFIX}{ids[0]}"
        args = (
            "--schema", "bug-backlog", "--title", title,
            "--body", f"{reason[:300]} | run {run.run_id} | ledger {run.run_dir / ledger.LEDGER_FILE}".replace("\n", " "),
            "--surface", BACKLOG_SURFACE, "--severity", "P2", "--status", "open", "--evidence", ids[0],
        )
        spec = StepSpec(
            id=step_id, stage="failure-backlog", kind="cli", target=BACKLOG_CLI, args=args,
            depends_on=None, already_satisfied=False, detail="", heading=briefing.OBSERVATIONS,
            cost_class=_cost_class(BACKLOG_CLI),
        )
        display.append(dataclasses.replace(spec, args=args + tuple(ids[1:])))
        cited = open_rows.get(title.lower())
        if cited:
            ledger.append(run.run_dir, StepResult(id=step_id, status="ran", reason="open-row-cited", stdout=cited))
        else:
            to_run.append(spec)
    if to_run:
        run_stages(
            to_run, ceremony=DISPATCH_KEY, repo_root=run.repo_root, run_dir=run.run_dir,
            dispatch=table, rules=None,
        )
    return display


def _tail_specs(run: _Run) -> tuple[list[StepSpec], list[tuple[StepSpec, str]]]:
    kwargs = dict(repo_root=run.repo_root, today=run.date)
    return (
        manifest.steps_for(POST_BRIEFING_STAGES, **kwargs),
        manifest.cadence_skipped(POST_BRIEFING_STAGES, **kwargs),
    )


def _render(run: _Run, steps: Sequence[StepSpec]) -> str:
    text = briefing.render(
        ledger.read(run.run_dir), _read_decisions(run.run_dir), run.observations, run.envelope,
        date=run.day, branch=run.branch, steps=steps,
    )
    (run.run_dir / BRIEFING_FILE).write_text(text, encoding="utf-8", newline="\n")
    return text


def _finish_run(
    run: _Run,
    table: Dispatch,
    digest: Mapping[str, Any],
    results: "Mapping[str, Any] | None",
    *,
    step0_failed: bool = False,
) -> str:
    """Run the tail for `run` and return the briefing; releases the run's lock.

    `results` is None when no workflow ran (template missing): each digest group with entries
    is then a `failed` row. A step 0 failure runs the failure backlog and the briefing only.
    """
    render_steps: list[StepSpec] = list(run.steps)
    filed: set[str] = set()
    try:
        if results is None:
            for group in digest["agent_work"]["groups"]:
                if not group["entries"]:
                    continue
                reason = TEMPLATE_MISSING
                if group["id"] == "judgment":
                    reason += ": undecided " + ", ".join(e["key"] for e in group["entries"])
                ledger.append(run.run_dir, _row(f"agent:{group['id']}", "failed", reason))
        elif not step0_failed:
            entries = [e["inputs"] for g in digest["agent_work"]["groups"] if g["id"] == "judgment" for e in g["entries"]]
            _, undecided = decide.record_agent_decisions(run.run_dir, entries, results)
            for u in undecided:
                ledger.append(run.run_dir, _row(f"judgment:{u['id']}", "failed", u["reason"]))
            render_steps += _directive_steps(run, run.envelope or {}, _read_decisions(run.run_dir), table)
            _check_inbox(run, digest, results)
        render_steps += _file_failures(run, table, filed)
        text = _render(run, render_steps)
        if not step0_failed:
            owed, skipped = _tail_specs(run)
            run_stages(
                owed, ceremony=DISPATCH_KEY, repo_root=run.repo_root, run_dir=run.run_dir,
                dispatch=table, rules=None,
            )
            for spec, reason in skipped:
                ledger.append(run.run_dir, _row(spec.id, "cadence-skipped", reason))
            render_steps += owed + [s for s, _ in skipped]
            render_steps += _file_failures(run, table, filed)
            text = _render(run, render_steps)
        _write_json(run.run_dir / DONE_FILE, {"briefing": str(run.run_dir / BRIEFING_FILE)})
        return text
    finally:
        single_flight.release_run(run.lock_path, run.lock_run_id)


def _emit_digest(obj: Mapping[str, Any]) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _stub_digest(repo_root: Path, state: str, note: str) -> dict[str, Any]:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return build_digest(
        ceremony=CEREMONY, run_id=f"{stamp}-{note}", repo_root=repo_root.as_posix(), state=state,
        finish_argv=[], groups=[],
    )


def _find_run_dir(repo_root: Path, lock_run_id: str) -> "Path | None":
    base = ledger.run_dir(repo_root, CEREMONY, "x").parent
    if not base.is_dir():
        return None
    for path in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True)[:20]:
        state_file = path / STATE_FILE
        if state_file.is_file():
            try:
                if _read_json(state_file).get("lock_run_id") == lock_run_id:
                    return path
            except ValueError:
                continue
    return None


def _finish_stored(repo_root: Path, run_dir: Path, table: Dispatch, results: Mapping[str, Any]) -> str:
    run = _load_state(repo_root, run_dir)
    return _finish_run(run, table, _read_json(run_dir / DIGEST_FILE), results)


def launch(
    repo_root: Path,
    *,
    dispatch: "Dispatch | None" = None,
    plugin_root: "Path | None" = None,
    today: "str | None" = None,
) -> int:
    base = dict(default_dispatch())
    base.update(dispatch or {})
    from coordinator_core.session.core import resolve_session_id

    adm = single_flight.acquire(
        repo_root, ceremony=CEREMONY, marker_path=_marker_path(repo_root), today=today,
        session_id=resolve_session_id(str(repo_root)) or "",
    )
    if adm.state == single_flight.ALREADY_RAN:
        _emit_digest(_stub_digest(repo_root, "already-ran", "already-ran"))
        return EXIT_OK
    if adm.state == single_flight.IN_FLIGHT:
        held = _find_run_dir(repo_root, adm.run_id) if adm.run_id else None
        if held is not None and (held / AGENT_RESULTS_FILE).is_file() and not (held / DONE_FILE).is_file():
            results = _read_json(held / AGENT_RESULTS_FILE)
            errors = validate_agent_results(results)
            if not errors:
                sys.stdout.write(_finish_stored(repo_root, held, base, results))
                return EXIT_OK
            print(f"{PROG}: {held / AGENT_RESULTS_FILE}: {'; '.join(errors)}", file=sys.stderr)
        _emit_digest(_stub_digest(repo_root, "in-flight", "in-flight"))
        return EXIT_IN_FLIGHT

    day = today or adm.run_dir.name
    run_id = _mint_run_id()
    run_dir = ledger.run_dir(repo_root, CEREMONY, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        adm.run_dir.rmdir()
    except OSError:
        pass
    run = _Run(
        repo_root=repo_root, run_id=run_id, run_dir=run_dir, lock_path=str(adm.lock_path),
        lock_run_id=adm.run_id, day=day, date=_dt.date.fromisoformat(day), branch=_current_branch(repo_root),
    )
    try:
        return _launch_admitted(run, adm, base, plugin_root)
    except BaseException:
        single_flight.release_run(run.lock_path, run.lock_run_id)
        raise


def _launch_admitted(run: _Run, adm: single_flight.Admission, base: Dispatch, plugin_root: "Path | None") -> int:
    if adm.abandoned_run_id:
        ledger.append(run.run_dir, _row(f"run:{adm.abandoned_run_id}", "failed", "never-finished"))
    table = _wrap_dispatch(base, run)
    _run_phase(run, _stages_upto(PHASE_ONE_LAST_STAGE), table)
    _run_phase(run, _stages_after(PHASE_ONE_LAST_STAGE), table)
    if run.resolution is not None:
        for point in run.resolution.observations:
            rationale = (point.get("recommendation") or {}).get("rationale", "")
            run.observations.append(f"{point.get('question', '')} ({point.get('id', '')}): {rationale}".strip())
    _save_state(run)

    finish_argv = [PROG, "finish", "--run-id", run.run_id]
    step0_failed = _row_status(run.run_dir, STEP0_STEP) == "failed"
    groups = [] if step0_failed else _build_groups(run)
    digest = build_digest(
        ceremony=CEREMONY, run_id=run.run_id, repo_root=run.repo_root.as_posix(),
        state="failed" if step0_failed else "ran", finish_argv=finish_argv, groups=groups,
    )
    _write_json(run.run_dir / DIGEST_FILE, digest)

    if step0_failed:
        sys.stdout.write(_finish_run(run, base, digest, {"groups": {}}, step0_failed=True))
        return EXIT_GATE_FAILED
    if not groups:
        sys.stdout.write(_finish_run(run, base, digest, {"groups": {}}))
        return EXIT_OK

    root = plugin_root if plugin_root is not None else emit.resolve_plugin_root()
    template = (Path(root) / TEMPLATE_RELPATH) if root else Path(TEMPLATE_RELPATH)
    if not template.is_file():
        sys.stdout.write(_finish_run(run, base, digest, None))
        print(f"{PROG}: workflow template not found: {template.as_posix()}", file=sys.stderr)
        return EXIT_TEMPLATE_MISSING
    script = emit.emit(digest, template=template, out_path=run.run_dir / SCRIPT_FILE, repo_root=run.repo_root)
    single_flight.mark_awaiting_finish(adm)
    print(f"digest: {(run.run_dir / DIGEST_FILE).as_posix()}")
    print(f'Workflow({{scriptPath: "{script.as_posix()}"}})')
    return EXIT_OK


def finish(
    repo_root: Path, run_id: str, agent_results: "str | None", *, dispatch: "Dispatch | None" = None
) -> int:
    base = dict(default_dispatch())
    base.update(dispatch or {})
    run_dir = ledger.run_dir(repo_root, CEREMONY, run_id)
    if not (run_dir / STATE_FILE).is_file():
        print(f"{PROG}: unknown run id: {run_id}", file=sys.stderr)
        return EXIT_USAGE
    if (run_dir / DONE_FILE).is_file():
        sys.stdout.write((run_dir / BRIEFING_FILE).read_text(encoding="utf-8"))
        return EXIT_OK
    path = Path(agent_results) if agent_results else run_dir / AGENT_RESULTS_FILE
    if path.is_file():
        try:
            results = _read_json(path)
        except ValueError as exc:
            print(f"{PROG}: {path}: not JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        errors = validate_agent_results(results)
        if errors:
            print(f"{PROG}: {path}: {'; '.join(errors)}", file=sys.stderr)
            return EXIT_USAGE
    elif agent_results:
        print(f"{PROG}: agent results not found: {path}", file=sys.stderr)
        return EXIT_USAGE
    else:
        results = {"groups": {}}
    sys.stdout.write(_finish_stored(repo_root, run_dir, base, results))
    return EXIT_OK


def _parse(argv: Sequence[str]) -> "tuple[str, dict[str, str]] | None":
    if not argv or argv[0] not in ("launch", "finish"):
        return None
    verb, opts, rest = argv[0], {}, list(argv[1:])
    allowed = {"--run-id", "--agent-results"} if verb == "finish" else set()
    while rest:
        flag = rest.pop(0)
        if flag not in allowed or not rest or flag in opts:
            return None
        opts[flag] = rest.pop(0)
    if verb == "finish" and not RUN_ID_RE.fullmatch(opts.get("--run-id", "")):
        return None
    return verb, opts


def main(
    argv: "Sequence[str] | None" = None,
    *,
    dispatch: "Dispatch | None" = None,
    plugin_root: "Path | None" = None,
    repo_root: "Path | None" = None,
    today: "str | None" = None,
) -> int:
    """Run one verb. `dispatch`, `plugin_root`, `repo_root` and `today` are test seams, not flags."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in ("-h", "--help") for a in args):
        print(USAGE)
        return EXIT_OK
    parsed = _parse(args)
    if parsed is None:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    verb, opts = parsed
    if repo_root is None:
        from coordinator_core.git.repo_root import show_toplevel

        top = show_toplevel(os.getcwd())
        if not top:
            print(f"{PROG}: not inside a git repository", file=sys.stderr)
            return EXIT_USAGE
        repo_root = Path(top)
    try:
        if verb == "launch":
            return launch(Path(repo_root), dispatch=dispatch, plugin_root=plugin_root, today=today)
        return finish(Path(repo_root), opts["--run-id"], opts.get("--agent-results"), dispatch=dispatch)
    except Exception as exc:  # noqa: BLE001 -- the one-line stderr report is the contract
        print(f"{PROG}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
