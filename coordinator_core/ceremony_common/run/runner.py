"""Stage runner: one child per step, one terminal ledger row per step, composition budget armed.

Ceremony-neutral: the dispatch-table key and budget name arrive as `ceremony`; the step list,
the dispatch callables and the rule table arrive as arguments. The runner never waits on a
person and never reads a step's `depends_on`.
"""
from __future__ import annotations

import asyncio
import contextvars
import importlib
import json
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from coordinator_core.composition_budget import CompositionBudget
from coordinator_core.contract.apply_base import assert_dispatchable
from coordinator_core.telemetry.composition_record import flush_composition_record, make_fleet_budget
from . import ledger
from .contract import DEADLINE_S, RUN_ID_RE, StepResult, StepSpec

OUTPUT_CAP = 4000
REASON_COLD = "cold"

_BIN_DIR = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
_ALLOWLIST = Path(__file__).resolve().parents[2] / "ops" / "warm_entrypoint_allowlist.json"


class BudgetRefused(RuntimeError):
    """The composition budget refused a child a rule tried to start."""


@dataclass(frozen=True)
class ChildOutcome:
    """What one child reported. `process_ms` is None when no figure exists (a warm op that returned none)."""

    exit_code: int
    process_ms: float | None = None
    spawns: int | None = None
    stdout: str = ""
    stderr: str = ""
    reason: str = ""


HELD_PREFIX = "held:"


@dataclass(frozen=True)
class Hold:
    """A rule's verdict that the gate is open but later steps must wait for a finish-time act.

    The gating step's row is `ran` with reason `held:<reason>`; later steps in the held stages
    (every later step when `RunHooks.held_stages` is None) get `halted-by-gate`.
    """

    reason: str


Dispatcher = Callable[..., ChildOutcome]
Rule = Callable[..., "bool | str | Hold"]


@dataclass
class _RuleScope:
    repo_root: Path
    budget: CompositionBudget
    measure: Dispatcher
    children: list[tuple[list[str], str, ChildOutcome]] = field(default_factory=list)


_RULE_SCOPE: contextvars.ContextVar[_RuleScope | None] = contextvars.ContextVar("rule_scope", default=None)


def rule_child(argv: Sequence[str], *, cost_class: str = "engine") -> ChildOutcome:
    """Start one measured child from inside a rule; it is counted against the budget and ledgered.

    Raises RuntimeError outside a rule, ValueError for an `over-bar` class, BudgetRefused when
    the composition budget is spent.
    """
    scope = _RULE_SCOPE.get()
    if scope is None:
        raise RuntimeError("rule_child is only callable from a rule run by run_stages")
    if cost_class == "over-bar":
        raise ValueError("a rule may not start an over-bar child")
    if not scope.budget.check():
        raise BudgetRefused(scope.budget.breached_units[-1])
    scope.budget.record_invocation("rule-child")
    outcome = scope.measure(list(argv), repo_root=scope.repo_root, deadline_s=DEADLINE_S[cost_class])
    scope.children.append((list(argv), cost_class, outcome))
    return outcome


def _clip(text: str) -> str:
    return text if len(text) <= OUTPUT_CAP else text[:OUTPUT_CAP] + "...[truncated]"


def _first_line(*texts: str) -> str:
    for text in texts:
        for line in text.splitlines():
            if line.strip():
                return line.strip()
    return ""


def _warm_entrypoints() -> frozenset[str]:
    try:
        return frozenset(json.loads(_ALLOWLIST.read_text(encoding="utf-8")).get("entrypoints", ()))
    except (OSError, ValueError):
        return frozenset()


_STDIN_SHIM = (
    "import os, runpy, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
    "os.dup2(fd, 0)\n"
    "os.close(fd)\n"
    "sys.stdin = open(0, encoding='utf-8', errors='replace', closefd=False)\n"
    "sys.argv = sys.argv[2:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def _measure_cold(
    cmd: Sequence[str], *, repo_root: Path, deadline_s: float, io_dir: Path | None = None,
    stdin_text: str | None = None,
) -> ChildOutcome:
    """Run `cmd` once under process-time accounting; a child past the deadline is reported, not awaited.

    `stdin_text` reaches a `python <script> ...` command as its stdin through a shim that stands
    in for the child's interpreter entry, so it adds no process.
    """
    import tempfile

    from coordinator_core.benchmarks.process_time import single_invocation_tree_process_time

    out_dir = Path(tempfile.mkdtemp(prefix="run-io-", dir=str(io_dir) if io_dir else None))
    out_path, err_path = out_dir / "stdout", out_dir / "stderr"
    if stdin_text is not None:
        in_path = out_dir / "stdin"
        in_path.write_text(stdin_text, encoding="utf-8")
        cmd = [cmd[0], "-c", _STDIN_SHIM, str(in_path), *cmd[1:]]
    box: dict[str, Any] = {}

    def _work() -> None:
        try:
            box["result"] = single_invocation_tree_process_time(
                list(cmd), cwd=str(repo_root), stdout_path=str(out_path), stderr_path=str(err_path)
            )
        except BaseException as exc:  # noqa: BLE001 -- reported as a failed outcome
            box["error"] = exc

    worker = threading.Thread(target=_work, name="run-child", daemon=True)
    worker.start()
    worker.join(deadline_s)
    if worker.is_alive():
        return ChildOutcome(exit_code=124, reason="deadline")
    if "error" in box:
        raise box["error"]
    res = box["result"]
    read = lambda p: p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""  # noqa: E731
    outcome = ChildOutcome(
        exit_code=int(res["rc"]),
        process_ms=float(res["process_time_ms"]),
        spawns=int(res["procs"]),
        stdout=read(out_path),
        stderr=read(err_path),
    )
    shutil.rmtree(out_dir, ignore_errors=True)
    return outcome


def _warm_request(method: str, params: dict[str, Any], deadline_s: float) -> dict[str, Any] | None:
    from coordinator_core.warm.client import try_warm_dispatch

    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return try_warm_dispatch(msg, read_deadline_secs=deadline_s)


def _cold_cli(step: StepSpec, *, repo_root: Path, deadline_s: float, stdin_text: str | None = None) -> ChildOutcome:
    script = _BIN_DIR / f"{step.target}.py"
    out = _measure_cold(
        [sys.executable, str(script), *step.args], repo_root=repo_root, deadline_s=deadline_s, stdin_text=stdin_text
    )
    return ChildOutcome(
        exit_code=out.exit_code, process_ms=out.process_ms, spawns=out.spawns,
        stdout=out.stdout, stderr=out.stderr, reason=out.reason or REASON_COLD,
    )


def run_bin_cold(target: str, args: Sequence[str], *, repo_root: Path, deadline_s: float) -> ChildOutcome:
    """Run `coordinator/bin/<target>.py` as one measured cold child, for a step whose bareword is outside the dispatch row."""
    return _cold_cli(
        StepSpec(
            id=target, stage="", kind="cli", target=target, args=tuple(args), depends_on=None,
            already_satisfied=False, detail="", heading="", cost_class="engine",
        ),
        repo_root=repo_root, deadline_s=deadline_s,
    )


def _dispatch_cli(
    step: StepSpec, *, repo_root: Path, run_dir: Path, deadline_s: float, stdin_text: str | None = None
) -> ChildOutcome:
    """Allowlisted barewords go through the warm door; others start a cold child."""
    if step.target in _warm_entrypoints():
        params: dict[str, Any] = {"argv": list(step.args), "cwd": str(repo_root), "entrypoint": step.target}
        if stdin_text is not None:
            params["stdin"] = stdin_text
        resp = _warm_request("invoke.from_argv", params, deadline_s)
        if resp is not None:
            result = resp.get("result")
            if isinstance(result, dict):
                ms = result.get("process_ms")
                return ChildOutcome(
                    exit_code=int(result.get("exit_code", 1)),
                    process_ms=float(ms) if ms is not None else None,
                    spawns=0,
                    stdout=str(result.get("stdout", "")),
                    stderr=str(result.get("stderr", "")),
                )
            return ChildOutcome(exit_code=1, spawns=0, stderr=_clip(json.dumps(resp.get("error", resp))))
    return _cold_cli(step, repo_root=repo_root, deadline_s=deadline_s, stdin_text=stdin_text)


def _dispatch_op(
    step: StepSpec, *, repo_root: Path, run_dir: Path, deadline_s: float, stdin_text: str | None = None
) -> ChildOutcome:
    params = json.loads(step.args[0]) if step.args else {}
    resp = _warm_request(step.target, params, deadline_s)
    if resp is None:
        from coordinator_core import ipc

        start = time.process_time()
        resp = asyncio.run(ipc.dispatch_message({"jsonrpc": "2.0", "id": 1, "method": step.target, "params": params}))
        spent_ms = (time.process_time() - start) * 1000.0
    else:
        spent_ms = None
    if "error" in resp:
        return ChildOutcome(exit_code=1, process_ms=spent_ms, spawns=0, stderr=json.dumps(resp["error"]))
    return ChildOutcome(exit_code=0, process_ms=spent_ms, spawns=0, stdout=json.dumps(resp.get("result")))


def _dispatch_inproc(
    step: StepSpec, *, repo_root: Path, run_dir: Path, deadline_s: float, stdin_text: str | None = None
) -> ChildOutcome:
    """`target` is `package.module:function`, called as `function(repo_root)` and returning `(exit_code, text)`.

    The manifest's inproc targets (`probes.*`, `touch_weekly_marker`) share that shape; the text
    becomes the step's stdout so the briefing can render it.
    """
    module_name, _, func_name = step.target.partition(":")
    func = getattr(importlib.import_module(module_name), func_name)
    start = time.process_time()
    code, text = func(repo_root)
    return ChildOutcome(
        exit_code=int(code),
        process_ms=(time.process_time() - start) * 1000.0,
        spawns=0,
        stdout=text,
    )


def default_dispatch() -> dict[str, Dispatcher]:
    """The production kind -> callable table; tests inject fakes for all keys, `rule-child` included."""
    return {
        "cli": _dispatch_cli,
        "op": _dispatch_op,
        "inproc": _dispatch_inproc,
        "rule-child": lambda argv, *, repo_root, deadline_s: _measure_cold(
            argv, repo_root=repo_root, deadline_s=deadline_s
        ),
    }


def _result_from(step: StepSpec, outcome: ChildOutcome) -> StepResult:
    status = "ran" if outcome.exit_code == 0 else "failed"
    reason = outcome.reason
    if outcome.reason == "deadline":
        status = "failed"
    elif status == "failed" and (not reason or reason == REASON_COLD):
        reason = f"exit {outcome.exit_code}: {_first_line(outcome.stderr, outcome.stdout)}".rstrip(": ")
    return StepResult(
        id=step.id, status=status, reason=reason, exit_code=outcome.exit_code,
        process_ms=outcome.process_ms, spawns=outcome.spawns,
        stdout=_clip(outcome.stdout), stderr=_clip(outcome.stderr),
    )


def _apply_rule(
    step: StepSpec, result: StepResult, rule_id: str, rule: Rule | None, scope: _RuleScope,
    *, run_dir: Path,
) -> StepResult:
    """Offer a gating step's non-zero exit to its rule; the rule's own children get their own rows."""
    resolved: "bool | str | Hold" = False
    token = _RULE_SCOPE.set(scope)
    try:
        if rule is not None:
            resolved = rule(step, result, repo_root=scope.repo_root, run_dir=run_dir)
    except Exception as exc:  # noqa: BLE001 -- a raising rule is an unresolved gate
        resolved = f"rule {rule_id} raised {type(exc).__name__}: {exc}"
    finally:
        _RULE_SCOPE.reset(token)
        for index, (_argv, cost_class, child) in enumerate(scope.children, start=1):
            suffix = "" if index == 1 else f"#{index}"
            ledger.append(
                run_dir,
                StepResult(
                    id=f"{step.id}:rule:{rule_id}{suffix}",
                    status="ran" if child.exit_code == 0 else "failed",
                    reason=f"cost-class:{cost_class}",
                    exit_code=child.exit_code, process_ms=child.process_ms, spawns=child.spawns,
                    stdout=_clip(child.stdout), stderr=_clip(child.stderr),
                ),
            )
        scope.children.clear()
    if isinstance(resolved, Hold):
        return StepResult(
            id=step.id, status="ran", reason=f"{HELD_PREFIX}{resolved.reason}", exit_code=result.exit_code,
            process_ms=result.process_ms, spawns=result.spawns, stdout=result.stdout, stderr=result.stderr,
        )
    if resolved is True:
        return StepResult(
            id=step.id, status="ran", reason=f"resolved-by:{rule_id}", exit_code=result.exit_code,
            process_ms=result.process_ms, spawns=result.spawns, stdout=result.stdout, stderr=result.stderr,
        )
    if isinstance(resolved, str) and resolved:
        return StepResult(
            id=step.id, status="failed", reason=resolved, exit_code=result.exit_code,
            process_ms=result.process_ms, spawns=result.spawns, stdout=result.stdout, stderr=result.stderr,
        )
    return result


def run_stages(
    steps: Sequence[StepSpec],
    *,
    ceremony: str,
    repo_root: "Path | str",
    run_dir: Path,
    dispatch: Mapping[str, Dispatcher] | None = None,
    budget: CompositionBudget | None = None,
    rules: Mapping[str, Rule] | None = None,
    allow_suite: bool = False,
    suite_grant: str | None = None,
    held_stages: "frozenset[str] | None" = None,
) -> list[StepResult]:
    """Run `steps` in order, appending exactly one terminal ledger row per step.

    A raising step is a `failed` row and the run continues. A gating step that ends unresolved
    (failed, raised, deadline, not-composed) gives every remaining step `halted-by-gate`, unless
    it is `best_effort`. A gating step whose rule returned a `Hold` ends `ran` and gives the
    remaining steps in `held_stages` (all of them when None) `halted-by-gate` with the hold's
    reason. A `suite` step starts only with `allow_suite=True` and a `ran` row for
    the step id `suite_grant`. Raises before any child starts when a `cli` step's bareword is outside the dispatch row.
    """
    repo = Path(repo_root)
    table = dict(default_dispatch())
    table.update(dispatch or {})
    rule_table = dict(rules or {})
    for step in steps:
        if step.kind == "cli":
            assert_dispatchable(ceremony, step.target)

    owned_budget = budget if budget is not None else make_fleet_budget(ceremony)
    scope = _RuleScope(repo_root=repo, budget=owned_budget, measure=table["rule-child"])
    results: list[StepResult] = []
    halted_by: str | None = None
    held_by: str | None = None
    done: dict[str, StepResult] = {}
    try:
        for step in steps:
            result = StepResult(id=step.id, status="failed", reason="runner-aborted")
            try:
                if halted_by is not None:
                    result = StepResult(id=step.id, status="halted-by-gate", reason=halted_by)
                elif held_by is not None and (held_stages is None or step.stage in held_stages):
                    result = StepResult(id=step.id, status="halted-by-gate", reason=held_by)
                else:
                    result = _run_step(
                        step, table, rule_table, scope, run_dir=run_dir, done=done,
                        allow_suite=allow_suite, suite_grant=suite_grant,
                    )
                    if step.gates_next and not step.best_effort and result.status in ("failed", "not-composed"):
                        halted_by = step.id
                    elif (
                        step.gates_next and not step.best_effort
                        and result.status == "ran" and result.reason.startswith(HELD_PREFIX)
                    ):
                        held_by = result.reason
            except Exception as exc:  # noqa: BLE001 -- the row is the report
                result = StepResult(id=step.id, status="failed", reason=f"raised {type(exc).__name__}: {exc}")
                if step.gates_next and not step.best_effort:
                    halted_by = step.id
            finally:
                ledger.append(run_dir, result)
            results.append(result)
            done[result.id] = result
    finally:
        failed = any(r.status == "failed" for r in results)
        flush_composition_record(
            owned_budget,
            "directive_failed" if failed or halted_by else "success",
            repo_root=repo,
            exit_code_label="GATE_HALT" if halted_by else ("STEP_FAILED" if failed else "OK"),
        )
    return results


def _child_run(stdout: str) -> tuple[str, str]:
    """Return `(state, run_id)` from a chained launch's output; either is empty when it printed none."""
    try:
        obj = json.loads(stdout)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        return str(obj.get("state", "")), str(obj.get("run_id", ""))
    match = RUN_ID_RE.search(stdout)
    return "", match.group(0) if match else ""


def _launch_result(result: StepResult) -> StepResult:
    """Record the chained child's run id, or `already-ran` when it started no new run."""
    if result.status != "ran":
        return result
    state, run_id = _child_run(result.stdout)
    if state == "already-ran":
        reason = "already-ran"
    elif run_id:
        reason = f"child-run:{run_id}"
    else:
        return result
    return StepResult(
        id=result.id, status="ran", reason=reason, exit_code=result.exit_code,
        process_ms=result.process_ms, spawns=result.spawns, stdout=result.stdout, stderr=result.stderr,
    )


def _run_step(
    step: StepSpec, table: Mapping[str, Dispatcher], rules: Mapping[str, Rule], scope: _RuleScope,
    *, run_dir: Path, done: Mapping[str, StepResult], allow_suite: bool = False, suite_grant: str | None = None,
) -> StepResult:
    if step.already_satisfied:
        return StepResult(id=step.id, status="ran", reason="already-satisfied")
    if step.cost_class == "over-bar":
        return StepResult(id=step.id, status="not-composed", reason="over-bar")
    stdin_text: str | None = None
    if step.stdin_from is not None:
        upstream = done.get(step.stdin_from)
        if upstream is None or upstream.status != "ran" or upstream.reason == "already-satisfied":
            return StepResult(id=step.id, status="not-composed", reason=step.stdin_from)
        stdin_text = upstream.stdout
    if step.cost_class == "suite":
        if not allow_suite:
            return StepResult(id=step.id, status="not-composed", reason="not-in-launch")
        granted = done.get(suite_grant) if suite_grant else None
        if granted is None or granted.status != "ran":
            return StepResult(id=step.id, status="not-composed", reason="no-grant")
    if not scope.budget.check():
        return StepResult(id=step.id, status="not-composed", reason=scope.budget.breached_units[-1])
    scope.budget.record_invocation(step.id)
    deadline_s = DEADLINE_S[step.cost_class]
    extra = {} if stdin_text is None else {"stdin_text": stdin_text}
    outcome = table[step.kind](step, repo_root=scope.repo_root, run_dir=run_dir, deadline_s=deadline_s, **extra)
    result = _result_from(step, outcome)
    if result.status == "failed" and step.gates_next and not step.best_effort and outcome.reason != "deadline":
        rule_id = step.on_exit.get(outcome.exit_code)
        if rule_id is not None:
            result = _apply_rule(step, result, rule_id, rules.get(rule_id), scope, run_dir=run_dir)
    if step.cost_class == "launch":
        result = _launch_result(result)
    return result
