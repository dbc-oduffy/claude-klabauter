"""Ceremony driver: `launch` and `finish` for any CeremonySpec.

`launch` takes the single-flight lock, runs the spec's segments under the stage runner (front
door, the envelope step, the envelope's launch-stage directives, then each residue segment),
resolves every judgment point by rule or routes it to an agent, and either finishes the run
itself (no agent work) or binds the digest onto the workflow template and prints the one
`Workflow(...)` line. `finish` records the agents' decisions, runs the steps they open, files
the failures, renders the receipt, runs the tail and the chained launch, and releases the lock.
Nothing a person supplies continues a run; the ledger, `decisions.jsonl`, the digest and the
lock are the only state between the two verbs.

Segment protocol: `spec.front_door` and each member of `spec.residue` is a sequence of items or
a callable `(*, repo_root, today, ledger_rows) -> items`, evaluated just before its segment so a
gate can read the rows of the segments before it. An item is a `StepSpec` (owed) or a
`(StepSpec, reason)` pair (ledgered `cadence-skipped`). `ledger_rows` are dicts that carry the
ledger's path as `ledger_path` and, during `launch`, each step's unclipped pre-hook stdout as
`raw_stdout`. `spec.finish_steps` returns
the same item kinds; those whose stage follows `RENDER_STAGE` in `spec.finish_stages` run after
the first render, the rest before it.

A spec may carry a `hooks` attribute (`RunHooks`) for the four ceremony-specific seams the
fields of `CeremonySpec` do not cover.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import inspect
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from coordinator_core.contract import apply_base
from . import decide, emit, envelope as envelope_mod, ledger, receipt, single_flight
from .contract import (
    EXIT_GATE_FAILED,
    EXIT_IN_FLIGHT,
    EXIT_OK,
    EXIT_TEMPLATE_MISSING,
    EXIT_USAGE,
    RUN_ID_RE,
    Decision,
    StepResult,
    StepSpec,
    build_digest,
    validate_agent_results,
    validate_digest,
)
from .runner import HELD_PREFIX, ChildOutcome, default_dispatch, run_stages

FAILURE_ROWS_FILED_MAX = 10
BACKLOG_PREFIX = "backlog:"
BACKLOG_CLI = "coordinator-queue-append"
BACKLOG_DIR = Path("state") / "bug-backlog"
TEMPLATE_MISSING = "template-missing"
DECIDE_STEP = "decide-resolve"
DECIDE_TARGET = "coordinator_core.ceremony_common.run.decide:resolve"
EXHAUSTED_RULE = "no-live-disposition"
RENDER_STAGE = "briefing"
PRE_DIRECTIVE_STAGE = "pre-directives"

SCRIPT_FILE = "workflow.mjs"
STATE_FILE = "launch-state.json"
ENVELOPE_FILE = "envelope.json"
DIGEST_FILE = "digest.json"
AGENT_RESULTS_FILE = "agent-results.json"
BRIEFING_FILE = "briefing.md"
DONE_FILE = "finished.json"
LOCK_RUN_FILE = "lock-run-id"
CARRIED_PREFIX = "carried:"

Dispatch = Mapping[str, Callable[..., ChildOutcome]]


@dataclass(frozen=True)
class RunHooks:
    """The seams a spec fills beyond `CeremonySpec`'s fields; every member is optional.

    `after_step(step, outcome, *, envelope)` may replace a step's outcome before it is ledgered.
    `check_results(digest, results)` returns `(row id, reason)` failures found in the agents'
    results. `directive_wanted(directive)` is False for an envelope directive the spec's own
    steps already cover. `acting_dispositions` are the dispositions that must open at least one
    envelope directive, else the decision has a `failed` row. `held_stages` are the stages a
    rule's `Hold` halts (None: every later stage). A finish step whose stage is
    `PRE_DIRECTIVE_STAGE` runs once the agents' results are in, before the finish directives.
    """

    after_step: Callable[..., ChildOutcome] | None = None
    check_results: Callable[[Mapping[str, Any], Mapping[str, Any]], Sequence[tuple[str, str]]] | None = None
    directive_wanted: Callable[[Mapping[str, Any]], bool] | None = None
    acting_dispositions: frozenset[str] = frozenset()
    held_stages: "frozenset[str] | None" = None


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
    prog: str
    flags: Mapping[str, Any] = field(default_factory=dict)
    steps: list[StepSpec] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    captured: dict[str, ChildOutcome] = field(default_factory=dict)
    envelope: "dict[str, Any] | None" = None
    points: list[dict[str, Any]] = field(default_factory=list)
    resolution: "decide.Resolution | None" = None
    halted_by: "str | None" = None
    held_by: "str | None" = None
    carry: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Finished:
    text: str
    undecided: bool = False
    chained_workflow: str = ""


def _hooks(spec: Any) -> RunHooks:
    return getattr(spec, "hooks", None) or RunHooks()


class _Costs:
    """Mapping-shaped view over `spec.costs`, which is a callable `key -> cost class`."""

    def __init__(self, source: Any) -> None:
        self._source = source

    def get(self, key: str, default: str = "unmeasured") -> str:
        if self._source is None:
            return default
        if callable(self._source):
            return self._source(key) or default
        return self._source.get(key, default)


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
            "points": run.points,
        },
    )


def _load_state(repo_root: Path, run_dir: Path, prog: str) -> _Run:
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
        prog=prog,
        steps=[_spec_from_json(s) for s in obj["steps"]],
        observations=list(obj["observations"]),
        points=list(obj.get("points") or []),
        envelope=_read_json(envelope_path) if envelope_path.is_file() else None,
    )


def _mint_run_id() -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{now.microsecond:06d}{uuid.uuid4().hex[:4]}"


def _current_branch(repo_root: Path) -> str:
    from coordinator_core.git.git_dir import resolve_git_dir

    try:
        head = (resolve_git_dir(repo_root) / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else head[:12]


def _row(step_id: str, status: str, reason: str = "", stdout: str = "") -> StepResult:
    return StepResult(id=step_id, status=status, reason=reason, stdout=stdout)


def _rows(run: _Run) -> list[dict[str, Any]]:
    """Ledger rows, each with `ledger_path`; a step this process ran also carries its unclipped pre-hook stdout as `raw_stdout`."""
    path = str(run.run_dir / ledger.LEDGER_FILE)
    return [
        dict(r, ledger_path=path, raw_stdout=run.captured[r["id"]].stdout)
        if r["id"] in run.captured
        else dict(r, ledger_path=path)
        for r in ledger.read(run.run_dir)
    ]


def _row_status(run_dir: Path, step_id: str) -> str:
    rows = [r for r in ledger.read(run_dir) if r["id"] == step_id]
    return rows[-1]["status"] if rows else ""


def _rule_table(rules: Mapping[str, Callable[..., Any]]) -> decide.RuleTable:
    """Point rules (those taking `context`) in mapping order, the shared `recommendation` last."""
    ordered = [
        (rid, fn)
        for rid, fn in rules.items()
        if rid != decide.RECOMMENDATION and "context" in inspect.signature(fn).parameters
    ]
    if decide.RECOMMENDATION in rules:
        ordered.append((decide.RECOMMENDATION, rules[decide.RECOMMENDATION]))
    return decide.RuleTable(point_rules=tuple(ordered), exhausted_rule=EXHAUSTED_RULE)


def _envelope_outcome(run: _Run, outcome: ChildOutcome) -> ChildOutcome:
    if outcome.exit_code != 0:
        return outcome
    try:
        envelope = envelope_mod.parse(outcome.stdout)
    except envelope_mod.EnvelopeError:
        return ChildOutcome(1, outcome.process_ms, outcome.spawns, "", outcome.stderr, "envelope-not-json")
    run.envelope = envelope
    _write_json(run.run_dir / ENVELOPE_FILE, envelope)
    points = len(envelope.get("judgment_points") or [])
    directives = len(envelope.get("directives") or [])
    return dataclasses.replace(outcome, stdout=f"orient: {directives} directive(s), {points} judgment point(s)")


def _decide_outcome(run: _Run, spec: Any) -> ChildOutcome:
    envelope = run.envelope or {}
    rows = _rows(run)
    run.points = [*(envelope.get("judgment_points") or []), *spec.spec_points(envelope, run.flags, rows)]
    run.resolution = decide.resolve(
        run.points,
        envelope.get("directives") or [],
        rules=_rule_table(spec.rules),
        context={"flags": run.flags, "envelope": envelope, "ledger_rows": rows},
    )
    decide.record(run.run_dir, run.resolution.decisions)
    r = run.resolution
    text = (
        f"{len(r.decisions)} judgment point(s) decided by rule, "
        f"{len(r.judgment_entries)} routed to agents, {len(r.observations)} reported"
    )
    return ChildOutcome(0, spawns=0, stdout=text)


def _launch_text(stdout: str) -> str:
    return "\n".join(ln for ln in stdout.splitlines() if "Workflow(" not in ln)


def _wrap_dispatch(base: Dispatch, run: _Run, spec: Any) -> dict[str, Callable[..., ChildOutcome]]:
    """Capture each step's full outcome and apply the envelope and `after_step` hooks; the runner clips what it ledgers."""
    hooks = _hooks(spec)
    table: dict[str, Callable[..., ChildOutcome]] = dict(base)

    def wrap(inner: Callable[..., ChildOutcome]) -> Callable[..., ChildOutcome]:
        def call(step: StepSpec, **kwargs: Any) -> ChildOutcome:
            if step.target == DECIDE_TARGET:
                outcome = _decide_outcome(run, spec)
            else:
                outcome = inner(step, **kwargs)
            run.captured[step.id] = outcome
            if step.id == spec.envelope_step:
                outcome = _envelope_outcome(run, outcome)
            if hooks.after_step is not None:
                outcome = hooks.after_step(step, outcome, envelope=run.envelope)
            if step.cost_class == "launch":
                outcome = dataclasses.replace(outcome, stdout=_launch_text(outcome.stdout))
            return outcome

        return call

    for kind in ("cli", "op", "inproc"):
        table[kind] = wrap(base[kind])
    return table


def _split(items: Any) -> tuple[list[StepSpec], list[tuple[StepSpec, str]]]:
    if isinstance(items, StepSpec):
        items = [items]
    owed: list[StepSpec] = []
    skipped: list[tuple[StepSpec, str]] = []
    for item in items or ():
        if isinstance(item, StepSpec):
            owed.append(item)
        else:
            skipped.append((item[0], item[1]))
    return owed, skipped


def _segment(source: Any, run: _Run) -> tuple[list[StepSpec], list[tuple[StepSpec, str]]]:
    if callable(source):
        source = source(repo_root=run.repo_root, today=run.date, ledger_rows=_rows(run))
    return _split(source)


def _suite_grant(owed: Sequence[StepSpec]) -> "str | None":
    return next((s.depends_on for s in owed if s.cost_class == "suite" and s.depends_on), None)


def _not_yet_ran(run: _Run, spec: Any, owed: Sequence[StepSpec]) -> list[StepSpec]:
    """`owed` minus the steps that already ran: a `ran` row in this ledger, or a dead run's envelope directive.

    A dead run's directive is carried into this ledger as a `carried:<run id>` row. Envelope,
    front-door, gating and in-process steps are never carried: later steps read their output from
    memory, which a dead run did not leave.
    """
    ran = {r["id"] for r in ledger.read(run.run_dir) if r["status"] == "ran"}
    directive_ids = {str(d.get("id", "")) for d in (run.envelope or {}).get("directives") or ()}
    runnable: list[StepSpec] = []
    for step in owed:
        if step.gates_next:
            runnable.append(step)
        elif step.id in ran:
            continue
        elif (
            step.id in run.carry and step.id in directive_ids and step.id != spec.envelope_step and step.kind != "inproc"
        ):
            ledger.append(run.run_dir, _row(step.id, "ran", f"{CARRIED_PREFIX}{run.carry[step.id]}"))
        else:
            runnable.append(step)
    return runnable


def _dead_run_ran_ids(spec: Any, repo_root: Path, lock_run_id: str) -> set[str]:
    """Ids of the `ran` rows in the ledger of the run that held lock `lock_run_id`; empty when it left none."""
    dead = _find_run_dir(spec, repo_root, lock_run_id)
    if dead is None:
        return set()
    try:
        return {r["id"] for r in ledger.read(dead) if r["status"] == "ran"}
    except (OSError, ValueError):
        return set()


def _run_batch(
    run: _Run,
    spec: Any,
    owed: Sequence[StepSpec],
    skipped: Sequence[tuple[StepSpec, str]],
    table: Dispatch,
    *,
    allow_suite: bool,
    rules: "Mapping[str, Callable[..., Any]] | None",
    gating: bool = True,
) -> None:
    """Run `owed` as one runner call, ledger `skipped`, and record both in `run.steps`.

    With `gating`, a gating step that ends unresolved halts every later batch of the run.
    """
    held_stages = _hooks(spec).held_stages
    if gating and run.halted_by is not None:
        for step in owed:
            ledger.append(run.run_dir, _row(step.id, "halted-by-gate", run.halted_by))
    elif owed:
        runnable = _not_yet_ran(run, spec, owed)
        if gating and run.held_by is not None:
            runnable = []
            for step in owed:
                if held_stages is None or step.stage in held_stages:
                    ledger.append(run.run_dir, _row(step.id, "halted-by-gate", run.held_by))
                else:
                    runnable.append(step)
        results = run_stages(
            runnable, ceremony=spec.dispatch_key, repo_root=run.repo_root, run_dir=run.run_dir,
            dispatch=table, rules=rules, allow_suite=allow_suite, suite_grant=_suite_grant(owed),
            held_stages=held_stages,
        ) if runnable else []
        if gating:
            blocking = {s.id for s in owed if s.gates_next and not s.best_effort}
            run.halted_by = next(
                (r.id for r in results if r.id in blocking and r.status in ("failed", "not-composed")), None
            )
            run.held_by = run.held_by or next(
                (r.reason for r in results if r.id in blocking and r.status == "ran" and r.reason.startswith(HELD_PREFIX)),
                None,
            )
    for step, reason in skipped:
        ledger.append(run.run_dir, _row(step.id, "cadence-skipped", reason))
    run.steps.extend(owed)
    run.steps.extend(step for step, _ in skipped)


def _stage_rank(spec: Any) -> Callable[[StepSpec], int]:
    order = [*spec.launch_stages, *spec.finish_stages]
    return lambda step: order.index(step.stage) if step.stage in order else len(order)


def _directive_steps(run: _Run, spec: Any) -> list[StepSpec]:
    """One step per wanted envelope directive that has no ledger row yet, in stage order.

    A directive whose stage the spec rejects, or whose stage is in neither stage list, gets
    its terminal `failed` row here and is not returned.
    """
    hooks = _hooks(spec)
    costs = _Costs(spec.costs)
    known = {r["id"] for r in ledger.read(run.run_dir)}
    stages = {*spec.launch_stages, *spec.finish_stages}
    steps: list[StepSpec] = []
    for directive in (run.envelope or {}).get("directives") or ():
        did = str(directive.get("id", ""))
        if did in known or (hooks.directive_wanted and not hooks.directive_wanted(directive)):
            continue
        try:
            step = envelope_mod.steps_from_envelope(
                {"directives": [directive]}, stage_of=spec.stage_of, costs=costs,
                heading_of=lambda _id: receipt.OBSERVATIONS,
            )[0]
        except Exception as exc:  # noqa: BLE001 -- the row is the report
            ledger.append(run.run_dir, _row(did, "failed", f"unknown-directive: {exc}"))
            continue
        if step.stage not in stages:
            ledger.append(run.run_dir, _row(did, "failed", f"unknown-stage:{step.stage}"))
            continue
        steps.append(step)
    return sorted(steps, key=_stage_rank(spec))


def _admit(run: _Run, spec: Any, steps: Sequence[StepSpec]) -> list[StepSpec]:
    """The steps the dispatch row admits; the rest get a `not-composed` row."""
    admitted: list[StepSpec] = []
    for step in steps:
        try:
            if step.kind == "cli":
                apply_base.assert_dispatchable(spec.dispatch_key, step.target)
        except apply_base.UnrecognizedDirective:
            ledger.append(run.run_dir, _row(step.id, "not-composed", "not-in-dispatch-row"))
            run.steps.append(step)
        else:
            admitted.append(step)
    return admitted


def _launch_directives(run: _Run, spec: Any, table: Dispatch) -> None:
    """Run the envelope directives of a launch stage whose gate is open before any decision."""
    candidates = [s for s in _directive_steps(run, spec) if s.stage in spec.launch_stages]
    jps = {str(p.get("id")): p for p in (run.envelope or {}).get("judgment_points") or []}
    ids = frozenset(str(d.get("id")) for d in (run.envelope or {}).get("directives") or [])
    ready, _held, _closed = envelope_mod.ready(candidates, jps, {}, directive_ids=ids)
    _run_batch(
        run, spec, _admit(run, spec, ready), [], table, allow_suite=True, rules=spec.rules,
    )


def _run_segments(run: _Run, spec: Any, table: Dispatch) -> None:
    sources = [spec.front_door, *spec.residue] if spec.front_door is not None else list(spec.residue)
    decide_planned = False
    directives_done = False
    for source in sources:
        owed, skipped = _segment(source, run)
        decide_planned = decide_planned or any(s.target == DECIDE_TARGET for s in [*owed, *(s for s, _ in skipped)])
        _run_batch(run, spec, owed, skipped, table, allow_suite=True, rules=spec.rules)
        if run.envelope is not None and not directives_done:
            directives_done = True
            _launch_directives(run, spec, table)
    if not decide_planned:
        step = StepSpec(
            id=DECIDE_STEP, stage=(spec.launch_stages or ("decide",))[-1], kind="inproc", target=DECIDE_TARGET,
            args=(), depends_on=None, already_satisfied=False, detail="Resolve the judgment points.",
            heading=receipt.OBSERVATIONS, cost_class="engine",
        )
        _run_batch(run, spec, [step], [], table, allow_suite=True, rules=spec.rules)


def _build_groups(run: _Run, spec: Any) -> list[dict[str, Any]]:
    groups = list(spec.agent_groups(run.envelope or {}, _rows(run), run.resolution))
    entries = run.resolution.judgment_entries if run.resolution else []
    if entries:
        groups.append({
            "id": "judgment", "phase": 4, "agent": decide.DEFAULT_AGENT, "ruling_agent": None, "inputs": {},
            "entries": [
                {"key": str(e["id"]), "brief": str(e.get("question", "")), "argv": None, "inputs": e}
                for e in entries
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


def _lock_record(run: _Run) -> dict[str, Any]:
    """The lock holder's record (`session_id`, `run_id`, `day`), plus `repo_root` and `branch`."""
    record: dict[str, Any] = {"session_id": "", "run_id": run.lock_run_id, "day": run.day}
    try:
        holder = json.loads(Path(run.lock_path).read_text(encoding="utf-8"))
        if isinstance(holder, dict) and holder.get("run_id") == run.lock_run_id:
            record.update(holder)
    except (OSError, ValueError):
        pass
    record["repo_root"] = run.repo_root.as_posix()
    record["branch"] = run.branch
    return record


def _finish_directives(
    run: _Run, spec: Any, decisions: Sequence[Decision], table: Dispatch
) -> dict[str, list[str]]:
    """Give every envelope directive not yet ledgered one terminal row; return `{point id: opened step ids}`."""
    steps = _directive_steps(run, spec)
    points = {str(p.get("id")): p for p in run.points or (run.envelope or {}).get("judgment_points") or []}
    ids = frozenset(str(d.get("id")) for d in (run.envelope or {}).get("directives") or [])
    decided = decide.decisions_map(decisions)
    ready, held, closed = envelope_mod.ready(steps, points, decided, directive_ids=ids)
    for step in closed:
        dep = next(d for d in apply_base.normalize_depends_on(step.depends_on) if d in points)
        ledger.append(run.run_dir, _row(step.id, "not-composed", f"closed-by:{dep}={decided[dep]['disposition']}"))
    for step in held:
        deps = apply_base.normalize_depends_on(step.depends_on)
        unknown = next((d for d in deps if d not in points and d not in ids), None)
        if unknown is not None:
            ledger.append(run.run_dir, _row(step.id, "failed", f"unknown-dependency:{unknown}"))
            continue
        waiting = next((d for d in deps if d in points and d not in decided), deps[0] if deps else step.id)
        ledger.append(run.run_dir, _row(step.id, "halted-by-gate", f"judgment:{waiting}"))
    run.steps.extend([*closed, *held])
    opened: dict[str, list[str]] = {}
    for step in ready:
        for dep in apply_base.normalize_depends_on(step.depends_on):
            if dep in points:
                opened.setdefault(dep, []).append(step.id)
    _run_batch(run, spec, _admit(run, spec, ready), [], table, allow_suite=False, rules=spec.rules, gating=False)
    return opened


def _check_acting(run: _Run, spec: Any, decisions: Sequence[Decision], opened: Mapping[str, list[str]]) -> None:
    acting = _hooks(spec).acting_dispositions
    for dec in decisions:
        if dec.decision not in acting:
            continue
        carried = opened.get(dec.point_id, [])
        if not carried:
            ledger.append(run.run_dir, _row(f"decision:{dec.point_id}", "failed", f"no-directive-carries:{dec.decision}"))
            continue
        bad = next((s for s in (_row_status(run.run_dir, i) for i in carried) if s != "ran"), "")
        if bad:
            ledger.append(run.run_dir, _row(f"decision:{dec.point_id}", "failed", bad))


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


def _backlog_groups(
    failed: Sequence[Mapping[str, Any]], run_id: str, name: str
) -> list[tuple[str, str, list[str]]]:
    """`(title, reason, covered ids)`: one group per title, or one for the whole run past the cap."""
    ids = [r["id"] for r in failed]
    if len(failed) > FAILURE_ROWS_FILED_MAX:
        return [(f"{name} run {run_id} failed {len(failed)} steps", ", ".join(ids), ids)]
    grouped: dict[str, tuple[str, list[str]]] = {}
    for r in failed:
        reason = str(r.get("reason") or "failed")
        if reason.startswith(TEMPLATE_MISSING):
            ident, reason = "workflow-template", TEMPLATE_MISSING
        elif r["id"].startswith("decision:"):
            ident = _family(r["id"].split(":", 1)[1])
        else:
            ident = r["id"]
        title = f"{name} step {ident} failed: {reason}"
        grouped.setdefault(title, (reason, []))[1].append(r["id"])
    return [(title, reason, ids_) for title, (reason, ids_) in grouped.items()]


def _file_failures(run: _Run, spec: Any, table: Dispatch, filed: set[str]) -> list[StepSpec]:
    """File one bug-backlog row per failed ledger row not yet filed; an open row of the same title is cited."""
    failed = [
        r for r in ledger.read(run.run_dir)
        if r["status"] == "failed" and not r["id"].startswith(BACKLOG_PREFIX) and r["id"] not in filed
    ]
    if not failed:
        return []
    filed.update(r["id"] for r in failed)
    open_rows = _open_bug_titles(run.repo_root)
    costs = _Costs(spec.costs)
    to_run: list[StepSpec] = []
    display: list[StepSpec] = []
    for title, reason, ids in _backlog_groups(failed, run.run_id, spec.name):
        step_id = f"{BACKLOG_PREFIX}{ids[0]}"
        args = (
            "--schema", "bug-backlog", "--title", title,
            "--body", f"{reason[:300]} | run {run.run_id} | ledger {run.run_dir / ledger.LEDGER_FILE}".replace("\n", " "),
            "--surface", f"coordinator_core/{spec.dispatch_key}", "--severity", "P2", "--status", "open",
            "--evidence", ids[0],
        )
        step = StepSpec(
            id=step_id, stage=receipt.FAILURE_BACKLOG_STAGE, kind="cli", target=BACKLOG_CLI, args=args,
            depends_on=None, already_satisfied=False, detail="", heading=receipt.OBSERVATIONS,
            cost_class=costs.get(BACKLOG_CLI),
        )
        display.append(dataclasses.replace(step, args=args + tuple(ids[1:])))
        cited = open_rows.get(title.lower())
        if cited:
            ledger.append(run.run_dir, StepResult(id=step_id, status="ran", reason="open-row-cited", stdout=cited))
        else:
            to_run.append(step)
    if to_run:
        run_stages(
            to_run, ceremony=spec.dispatch_key, repo_root=run.repo_root, run_dir=run.run_dir,
            dispatch=table, rules=None,
        )
    return display


def _filed_row_paths(run: _Run) -> list[str]:
    """Repo-relative paths of the rows this run's `backlog:` steps wrote; a cited open row is not ours."""
    paths: list[str] = []
    for row in ledger.read(run.run_dir):
        if not row["id"].startswith(BACKLOG_PREFIX) or row["status"] != "ran" or row.get("reason") == "open-row-cited":
            continue
        outcome = run.captured.get(row["id"])
        lines = [ln.strip() for ln in (outcome.stdout if outcome else "").splitlines() if ln.strip()]
        if not lines:
            continue
        written = Path(lines[-1])
        if written.is_absolute():
            try:
                written = written.relative_to(run.repo_root)
            except ValueError:
                continue
        rel = written.as_posix()
        if rel not in paths:
            paths.append(rel)
    return paths


def _commit_filed_rows(run: _Run, spec: Any) -> None:
    """Land every row filed this run in ONE scoped commit; a failure becomes a ledger row, never an exception."""
    paths = _filed_row_paths(run)
    if not paths:
        return
    try:
        from coordinator_core.ops.ceremony import git_native

        msg_file = run.run_dir / "backlog-commit-msg.txt"
        msg_file.write_text(
            f"{spec.name}: file failure rows (run {run.run_id})\n\n" + "\n".join(f"- {p}" for p in paths) + "\n",
            encoding="utf-8", newline="\n",
        )
        result = git_native.commit_scoped(paths, msg_file, run.repo_root)
        if not result.ok:
            ledger.append(run.run_dir, _row("backlog-commit", "failed", (result.stderr or result.stdout or "commit failed").strip()[:300]))
    except Exception as exc:  # noqa: BLE001 -- the finish must still complete and print its receipt
        ledger.append(run.run_dir, _row("backlog-commit", "failed", f"{type(exc).__name__}: {exc}"[:300]))


def _render(run: _Run, spec: Any, steps: Sequence[StepSpec], results: "Mapping[str, Any] | None") -> str:
    rows = ledger.read(run.run_dir)
    decisions = _read_decisions(run.run_dir)
    if spec.render is not None:
        text = spec.render(rows, decisions, run.observations, run.envelope, date=run.day, branch=run.branch, steps=steps)
    else:
        text = receipt.render(
            spec, rows, decisions, run.observations, run.envelope, results,
            facts={"title": f"# {spec.name} receipt: {run.day} ({run.branch})", "steps": {s.id: s for s in steps}},
        )
    (run.run_dir / BRIEFING_FILE).write_text(text, encoding="utf-8", newline="\n")
    return text


def _child_digest(stdout: str) -> "dict[str, Any] | None":
    """The chained launch's digest: its own JSON stdout, else the file named on its `digest:` line."""
    try:
        obj = json.loads(stdout)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        return obj
    match = re.search(r"^digest:\s*(.+?)\s*$", stdout, re.MULTILINE)
    if match:
        try:
            obj = _read_json(Path(match.group(1)))
        except (OSError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None
    return None


def _chain_step(spec: Any) -> StepSpec:
    chain = list(spec.chain)
    return StepSpec(
        id=f"chain:{chain[0]}", stage=(spec.finish_stages or ("chain",))[-1], kind="cli", target=chain[0],
        args=tuple(chain[1:]), depends_on=None, already_satisfied=False, detail="",
        heading=receipt.OBSERVATIONS, cost_class="launch",
    )


def _tail(
    run: _Run, spec: Any, decisions: Sequence[Decision]
) -> tuple[list[Any], list[Any], list[Any]]:
    """The spec's finish steps split into those before the finish directives, before the first render, and after it."""
    order = list(spec.finish_stages)
    cut = order.index(RENDER_STAGE) if RENDER_STAGE in order else len(order)
    pre: list[Any] = []
    before: list[Any] = []
    after: list[Any] = []
    for item in spec.finish_steps(decisions, _rows(run), run=_lock_record(run)) or ():
        step = item if isinstance(item, StepSpec) else item[0]
        if step.stage == PRE_DIRECTIVE_STAGE:
            pre.append(item)
        else:
            (after if step.stage in order and order.index(step.stage) > cut else before).append(item)
    return pre, before, after


def _finish_run(
    run: _Run,
    spec: Any,
    table: Dispatch,
    digest: Mapping[str, Any],
    results: "Mapping[str, Any] | None",
    *,
    gate_failed: bool = False,
) -> _Finished:
    """Run the tail for `run` and return the receipt; releases the run's lock.

    `results` is None when no workflow ran (template missing): each digest group with entries
    is then a `failed` row. A gate failure runs the failure backlog and the receipt only.
    """
    hooks = _hooks(spec)
    table = _wrap_dispatch(table, run, spec)
    render_steps: list[StepSpec] = list(run.steps)
    filed: set[str] = set()
    undecided: dict[str, str] = {}
    chained: "dict[str, Any] | None" = None
    chain_workflow = ""
    tail: "tuple[list[Any], list[Any], list[Any]] | None" = None
    try:
        if results is None:
            for group in digest["agent_work"]["groups"]:
                if not group["entries"]:
                    continue
                reason = TEMPLATE_MISSING
                if group["id"] == "judgment":
                    reason += ": undecided " + ", ".join(e["key"] for e in group["entries"])
                ledger.append(run.run_dir, _row(f"agent:{group['id']}", "failed", reason))
        elif not gate_failed:
            entries = [e["inputs"] for g in digest["agent_work"]["groups"] if g["id"] == "judgment" for e in g["entries"]]
            _, missed = decide.record_agent_decisions(run.run_dir, entries, _with_result_payloads(results))
            for u in missed:
                undecided[u["id"]] = u["reason"]
                ledger.append(run.run_dir, _row(f"judgment:{u['id']}", "failed", u["reason"]))
            decisions = _read_decisions(run.run_dir)
            tail = _tail(run, spec, decisions)
            owed, skipped = _split(tail[0])
            _run_batch(run, spec, owed, skipped, table, allow_suite=False, rules=None, gating=False)
            render_steps += owed + [s for s, _ in skipped]
            mark = len(run.steps)
            opened = _finish_directives(run, spec, decisions, table)
            _check_acting(run, spec, decisions, opened)
            render_steps += run.steps[mark:]
            if hooks.check_results is not None:
                for row_id, reason in hooks.check_results(digest, results):
                    ledger.append(run.run_dir, _row(row_id, "failed", reason))
        after: list[Any] = []
        if not gate_failed:
            _, before, after = tail or _tail(run, spec, _read_decisions(run.run_dir))
            if spec.chain:
                after.append(_chain_step(spec))
            owed, skipped = _split(before)
            _run_batch(run, spec, owed, skipped, table, allow_suite=False, rules=None, gating=False)
            render_steps += owed + [s for s, _ in skipped]
        render_steps += _file_failures(run, spec, table, filed)
        text = _render(run, spec, render_steps, results)
        if not gate_failed:
            owed, skipped = _split(after)
            _run_batch(run, spec, owed, skipped, table, allow_suite=False, rules=None, gating=False)
            render_steps += owed + [s for s, _ in skipped]
            render_steps += _file_failures(run, spec, table, filed)
            if spec.chain:
                outcome = run.captured.get(_chain_step(spec).id)
                if outcome is not None:
                    chained = _child_digest(outcome.stdout)
                    chain_workflow = next((ln for ln in outcome.stdout.splitlines() if "Workflow(" in ln), "")
            text = _render(run, spec, render_steps, results)
        if chained is not None:
            merged = dict(digest, chained=chained)
            if not validate_digest(merged):
                _write_json(run.run_dir / DIGEST_FILE, merged)
        _commit_filed_rows(run, spec)
        _write_json(run.run_dir / DONE_FILE, {"briefing": str(run.run_dir / BRIEFING_FILE)})
        return _Finished(text, undecided=bool(undecided), chained_workflow=chain_workflow)
    finally:
        single_flight.release_run(run.lock_path, run.lock_run_id)


def _with_result_payloads(results: Mapping[str, Any]) -> dict[str, Any]:
    """`results` with each `decision.payload` also set on its result, where `decide.record_agent_decisions` reads it."""
    groups: dict[str, list[Any]] = {}
    for group_id, items in (results.get("groups") or {}).items():
        groups[group_id] = [
            dict(item, payload=item["decision"]["payload"])
            if isinstance(item, Mapping)
            and isinstance(item.get("decision"), Mapping)
            and item["decision"].get("payload") is not None
            else item
            for item in items or []
        ]
    return {"groups": groups}


def _emit_digest(obj: Mapping[str, Any]) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _stub_digest(spec: Any, repo_root: Path, state: str, note: str) -> dict[str, Any]:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return build_digest(
        ceremony=spec.name, run_id=f"{stamp}-{note}", repo_root=repo_root.as_posix(), state=state,
        finish_argv=[], groups=[],
    )


def _lock_run_id_of(run_dir: Path) -> "str | None":
    """The lock run id a launch stamped before it wrote any state file, or None."""
    try:
        return (run_dir / LOCK_RUN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _find_run_dir(spec: Any, repo_root: Path, lock_run_id: str) -> "Path | None":
    base = ledger.run_dir(repo_root, spec.run_dir_name, "x").parent
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
        elif _lock_run_id_of(path) == lock_run_id:
            return path
    return None


def _print_finished(done: _Finished) -> int:
    sys.stdout.write(done.text)
    if done.chained_workflow:
        print(done.chained_workflow)
    return EXIT_GATE_FAILED if done.undecided else EXIT_OK


def _finish_stored(spec: Any, repo_root: Path, run_dir: Path, table: Dispatch, results: Mapping[str, Any], prog: str) -> _Finished:
    run = _load_state(repo_root, run_dir, prog)
    return _finish_run(run, spec, table, _read_json(run_dir / DIGEST_FILE), results)


def launch(
    spec: Any,
    *,
    repo_root: Path,
    flags: Mapping[str, Any],
    session_id: str,
    prog_argv: "Sequence[str] | None" = None,
    dispatch: "Dispatch | None" = None,
    plugin_root: "Path | None" = None,
    today: "str | None" = None,
) -> int:
    """Run one launch of `spec` and return its exit code.

    `flags` maps each launch flag the caller passed to its value; a flag outside
    `spec.launch_flags` is refused by name with exit 2 before any lock is taken. `dispatch`,
    `plugin_root` and `today` are test seams.
    """
    prog_args = list(prog_argv or ("ceremony-run", spec.name))
    prog = prog_args[0]
    unknown = sorted(set(flags) - set(spec.launch_flags))
    if unknown:
        print(f"{prog}: unknown launch flag: {', '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE
    repo_root = Path(repo_root)
    base = dict(default_dispatch())
    base.update(dispatch or {})
    marker = spec.marker_path(repo_root) if spec.marker_path is not None else None
    prior = single_flight._read_holder(
        single_flight._lock_dir(repo_root, spec.run_dir_name) / single_flight._LOCK_NAME
    )
    adm = single_flight.acquire(
        repo_root, ceremony=spec.run_dir_name, marker_path=marker, today=today, session_id=session_id,
    )
    if adm.state == single_flight.ALREADY_RAN:
        _emit_digest(_stub_digest(spec, repo_root, "already-ran", "already-ran"))
        return EXIT_OK
    if adm.state == single_flight.IN_FLIGHT:
        held = _find_run_dir(spec, repo_root, adm.run_id) if adm.run_id else None
        if held is not None and (held / AGENT_RESULTS_FILE).is_file() and not (held / DONE_FILE).is_file():
            results = _read_json(held / AGENT_RESULTS_FILE)
            errors = validate_agent_results(results)
            if not errors:
                return _print_finished(_finish_stored(spec, repo_root, held, base, results, prog))
            print(f"{prog}: {held / AGENT_RESULTS_FILE}: {'; '.join(errors)}", file=sys.stderr)
        _emit_digest(_stub_digest(spec, repo_root, "in-flight", "in-flight"))
        return EXIT_IN_FLIGHT

    day = today or adm.run_dir.name
    run_id = _mint_run_id()
    run_dir = ledger.run_dir(repo_root, spec.run_dir_name, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / LOCK_RUN_FILE).write_text(adm.run_id, encoding="utf-8", newline="\n")
    try:
        adm.run_dir.rmdir()
    except OSError:
        pass
    run = _Run(
        repo_root=repo_root, run_id=run_id, run_dir=run_dir, lock_path=str(adm.lock_path),
        lock_run_id=adm.run_id, day=day, date=_dt.date.fromisoformat(day), branch=_current_branch(repo_root),
        prog=prog, flags=dict(flags),
    )
    if prior is not None and prior["run_id"] != adm.run_id and prior["day"] == day:
        run.carry = dict.fromkeys(_dead_run_ran_ids(spec, repo_root, prior["run_id"]), prior["run_id"])
    try:
        return _launch_admitted(spec, run, adm, base, plugin_root, prog_args)
    except BaseException:
        single_flight.release_run(run.lock_path, run.lock_run_id)
        raise


def _launch_admitted(
    spec: Any, run: _Run, adm: single_flight.Admission, base: Dispatch, plugin_root: "Path | None",
    prog_args: Sequence[str],
) -> int:
    if adm.abandoned_run_id:
        ledger.append(run.run_dir, _row(f"run:{adm.abandoned_run_id}", "failed", "never-finished"))
    table = _wrap_dispatch(base, run, spec)
    _run_segments(run, spec, table)
    if run.resolution is not None:
        for point in run.resolution.observations:
            rationale = (point.get("recommendation") or {}).get("rationale", "")
            run.observations.append(f"{point.get('question', '')} ({point.get('id', '')}): {rationale}".strip())
    _save_state(run)

    gate_failed = run.halted_by is not None
    groups = [] if gate_failed else _build_groups(run, spec)
    digest = build_digest(
        ceremony=spec.name, run_id=run.run_id, repo_root=run.repo_root.as_posix(),
        state="failed" if gate_failed else "ran",
        finish_argv=[*prog_args, "finish", "--run-id", run.run_id], groups=groups,
    )
    _write_json(run.run_dir / DIGEST_FILE, digest)

    if gate_failed:
        sys.stdout.write(_finish_run(run, spec, base, digest, {"groups": {}}, gate_failed=True).text)
        return EXIT_GATE_FAILED
    if not any(g["entries"] for g in groups):
        return _print_finished(_finish_run(run, spec, base, digest, {"groups": {}}))

    root = plugin_root if plugin_root is not None else emit.resolve_plugin_root()
    template = (Path(root) / spec.template) if root else Path(spec.template)
    if not template.is_file():
        done = _finish_run(run, spec, base, digest, None)
        sys.stdout.write(done.text)
        print(f"{run.prog}: workflow template not found: {template.as_posix()}", file=sys.stderr)
        return EXIT_TEMPLATE_MISSING
    script = emit.emit(digest, template=template, out_path=run.run_dir / SCRIPT_FILE, repo_root=run.repo_root)
    single_flight.mark_awaiting_finish(adm)
    print(f"digest: {(run.run_dir / DIGEST_FILE).as_posix()}")
    print(f'Workflow({{scriptPath: "{script.as_posix()}"}})')
    return EXIT_OK


def finish(
    spec: Any,
    *,
    repo_root: Path,
    run_id: str,
    agent_results_path: "str | None",
    prog_argv: "Sequence[str] | None" = None,
    dispatch: "Dispatch | None" = None,
) -> int:
    """Finish run `run_id`: exit 2 for an unknown id or bad results, 5 when a judgment point stayed undecided.

    A run that already finished prints its stored receipt and runs nothing.
    """
    prog = list(prog_argv or ("ceremony-run", spec.name))[0]
    repo_root = Path(repo_root)
    base = dict(default_dispatch())
    base.update(dispatch or {})
    if not RUN_ID_RE.fullmatch(run_id or ""):
        print(f"{prog}: unknown run id: {run_id}", file=sys.stderr)
        return EXIT_USAGE
    run_dir = ledger.run_dir(repo_root, spec.run_dir_name, run_id)
    if not (run_dir / STATE_FILE).is_file():
        print(f"{prog}: unknown run id: {run_id}", file=sys.stderr)
        return EXIT_USAGE
    if (run_dir / DONE_FILE).is_file():
        sys.stdout.write((run_dir / BRIEFING_FILE).read_text(encoding="utf-8"))
        return EXIT_OK
    path = Path(agent_results_path) if agent_results_path else run_dir / AGENT_RESULTS_FILE
    if path.is_file():
        try:
            results = _read_json(path)
        except ValueError as exc:
            print(f"{prog}: {path}: not JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        errors = validate_agent_results(results)
        if errors:
            print(f"{prog}: {path}: {'; '.join(errors)}", file=sys.stderr)
            return EXIT_USAGE
    elif agent_results_path:
        print(f"{prog}: agent results not found: {path}", file=sys.stderr)
        return EXIT_USAGE
    else:
        results = {"groups": {}}
    return _print_finished(_finish_stored(spec, repo_root, run_dir, base, results, prog))
