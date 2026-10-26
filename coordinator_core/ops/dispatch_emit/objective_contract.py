"""The durable-objective seam: file names, shapes, falsifier-output parsing and the pure verdict fold.

Stdlib only (cold-import leaf). The engine runs the falsifier script deterministically and records
its lines; nothing here judges. No input yields an indeterminate status: every doubt is `not_met`
with a reason.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

OBJECTIVE_FILE = "objective.json"
LOOP_FILE = "objective-loop.jsonl"
BASE_FILE = "base.json"

OP_WRITE = "dispatch.objective_write"
OP_CHECK = "dispatch.objective_check"
OP_CALIBRATE = "dispatch.objective_calibrate"

PINNED_AT_SIZING = "sizing"
PINNED_AT_WAVE0 = "wave0"
PINNED_AT_VALUES = (PINNED_AT_SIZING, PINNED_AT_WAVE0)

REASON_RULER_CHANGED = "ruler-changed"
REASON_UNPINNED = "unpinned"
REASON_SHA_MISMATCH = "sha_mismatch"
REASON_TIMEOUT = "timeout"
REASON_UNPARSEABLE = "unparseable"
REASON_MISSING_AXIS = "missing_axis"
REASON_DUPLICATE_AXIS = "duplicate_axis"
REASON_NO_SUMMARY = "no_summary"
REASON_SUMMARY_DISAGREES = "summary_disagrees"
REASON_BAD_PASS = "pass_not_bool"
REASON_NO_CHECK = "no_check_recorded"

LINE_CHECK = "check"
LINE_CALIBRATION = "calibration"
LINE_REACHER = "reacher"
LINE_RETHINK = "rethink"

STATUS_DONE = "done"
STATUS_NOT_MET = "not_met"

OUTCOME_MET = "met"
OUTCOME_IMPOSSIBLE = "impossible"
OUTCOME_DOCTRINE_CONFLICT = "doctrine_conflict"
OUTCOME_EXHAUSTED = "exhausted"
OUTCOMES = (OUTCOME_MET, OUTCOME_IMPOSSIBLE, OUTCOME_DOCTRINE_CONFLICT, OUTCOME_EXHAUSTED)
# exhausted is NON-terminal: after n failed attempts the run enters rethink-and-research, then a new plan.
TERMINAL_OUTCOMES = (OUTCOME_MET, OUTCOME_IMPOSSIBLE, OUTCOME_DOCTRINE_CONFLICT)


def is_terminal(outcome: str) -> bool:
    """True only for met, impossible and doctrine_conflict; exhausted re-enters the loop."""
    return outcome in TERMINAL_OUTCOMES

EXIT_VERIFIED = "verified"
EXIT_IMPOSSIBLE = "impossible"
EXIT_FIRST_OFFICER = "first_officer"
EXIT_CEILING = "ceiling"
EXIT_UNRESOLVED = "unresolved"

# Defaults when objective.json omits a knob. `ask_compose.objective_settings` reads attempts_n, tokens and
# ceiling_spend_usd into the terminal gate. `minutes` and `ceiling_wall_minutes` have no enforcement point:
# a Workflow script has no clock. ReacherInput/ReacherOutput have no consumer; the judge leg uses an inline schema.
MAX_REACHER_ITERATIONS = 3
FALSIFIER_TIMEOUT_S = 120.0
DEFAULT_MINUTES = 90
DEFAULT_TOKENS = 2_000_000
DEFAULT_CEILING_SPEND_USD = 50.0
DEFAULT_CEILING_WALL_MINUTES = 480

KIND_MEASURE = "measure"
KIND_BEHAVIOUR = "behaviour"
COMPARATORS = ("<=", ">=", "==", "bool")


@dataclass
class Axis:
    """A declared objective axis: the claim, its sizing-time baseline and its threshold."""

    id: str
    claim: str = ""
    baseline: Any = None
    threshold: Any = None


@dataclass
class AxisResult:
    """One axis's falsifier line. `passed` serialises as the line's `pass` key."""

    axis: str
    passed: bool
    kind: str = KIND_MEASURE
    command: str = ""
    base: Any = None
    now: Any = None
    threshold: Any = None
    comparator: str = "bool"
    evidence: str = ""
    reason: str = ""

    def to_line(self) -> dict:
        out = asdict(self)
        out["pass"] = out.pop("passed")
        return out


@dataclass
class Objective:
    """objective.json. Written once at sizing; only the pin fields may be set later, once."""

    verbatim: str
    falsifier_script: str
    axes: list = field(default_factory=list)
    falsifier_candidate_sha256: Optional[str] = None
    falsifier_sha256: Optional[str] = None
    pinned_at: Optional[str] = None
    written_at: str = ""
    attempts_n: int = MAX_REACHER_ITERATIONS
    minutes: int = DEFAULT_MINUTES
    tokens: int = DEFAULT_TOKENS
    ceiling_spend_usd: float = DEFAULT_CEILING_SPEND_USD
    ceiling_wall_minutes: int = DEFAULT_CEILING_WALL_MINUTES

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Objective":
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["axes"] = [a if isinstance(a, Axis) else Axis(**{k: v for k, v in a.items() if k in Axis.__dataclass_fields__})
                          for a in kwargs.get("axes") or []]
        return cls(**kwargs)


@dataclass
class LoopLine:
    """One objective-loop.jsonl line: a falsifier check, a calibration, a reacher iteration or a rethink."""

    wave: int
    iteration: int
    axes: list = field(default_factory=list)
    spend_usd: float = 0.0
    at: str = ""
    kind: str = LINE_CHECK
    check_cost_usd: float = 0.0
    check_llm_calls: int = 0
    falsifier_lines: list = field(default_factory=list)
    delta_plan_path: Optional[str] = None
    reason: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class ReacherInput:
    """What the terminal gate hands the reacher agent."""

    objective_path: str
    failing: list
    diff_range: str
    budget_left_usd: float
    iteration: int


@dataclass
class ReacherOutput:
    """What the reacher returns; it never declares done, only a passing falsifier does."""

    delta_plan_path: str
    rows: list = field(default_factory=list)


@dataclass
class Rethink:
    """One rethink-and-research stage: what fired it, the new approach (delta plan) and its verdict."""

    trigger: str
    approach_path: Optional[str] = None
    verdict: str = ""


@dataclass
class Calibration:
    """Wave-0 calibration: falsifier lines at base and at each credited sha, plus the verdict."""

    base: list = field(default_factory=list)
    credited: list = field(default_factory=list)
    verdict: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def falsifier_argv(script: Any) -> list:
    """The argv that runs a falsifier script: a `.py` goes through the running interpreter (no exec bit
    or shebang needed, Windows-safe); anything else runs directly. One launcher for write, calibrate and check."""
    import sys

    path = str(script)
    return [sys.executable, path] if path.endswith(".py") else [path]


def _axis_id(axis: Any) -> str:
    return axis.id if isinstance(axis, Axis) else str(axis.get("id") if isinstance(axis, Mapping) else axis)


def parse_falsifier_lines(stdout: str, axes: Sequence[Any]) -> list:
    """One AxisResult per declared axis, in declared order; any doubt gives `passed=False` with a reason.

    A non-JSON line fails every axis (unparseable); a missing or duplicate axis fails that axis; the
    final summary line is authoritative only when it agrees with the axis lines, else every axis fails.
    """
    ids = [_axis_id(a) for a in axes]
    parsed: dict = {}
    dupes: set = set()
    summary: Optional[dict] = None
    for raw in (stdout or "").splitlines():
        text = raw.strip()
        if not text:
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            return [AxisResult(axis=i, passed=False, reason=REASON_UNPARSEABLE, evidence=text[:200]) for i in ids]
        if obj.get("summary") is True:
            summary = obj
            continue
        name = obj.get("axis")
        if not isinstance(name, str):
            return [AxisResult(axis=i, passed=False, reason=REASON_UNPARSEABLE, evidence=text[:200]) for i in ids]
        if name in parsed:
            dupes.add(name)
        parsed[name] = obj

    results = []
    for axis_id in ids:
        obj = parsed.get(axis_id)
        if axis_id in dupes:
            results.append(AxisResult(axis=axis_id, passed=False, reason=REASON_DUPLICATE_AXIS))
        elif obj is None:
            results.append(AxisResult(axis=axis_id, passed=False, reason=REASON_MISSING_AXIS))
        elif not isinstance(obj.get("pass"), bool):
            results.append(AxisResult(axis=axis_id, passed=False, reason=REASON_BAD_PASS))
        else:
            results.append(AxisResult(
                axis=axis_id, passed=obj["pass"], kind=str(obj.get("kind") or KIND_MEASURE),
                command=str(obj.get("command") or ""), base=obj.get("base"), now=obj.get("now"),
                threshold=obj.get("threshold"), comparator=str(obj.get("comparator") or "bool"),
                evidence=str(obj.get("evidence") or ""),
            ))

    if summary is None:
        return [_failed(r, REASON_NO_SUMMARY) for r in results]
    failing = sorted(r.axis for r in results if not r.passed)
    claimed = summary.get("failing")
    agrees = isinstance(summary.get("pass"), bool) and summary["pass"] == (not failing)
    if agrees and isinstance(claimed, list) and sorted(str(x) for x in claimed) != failing:
        agrees = False
    if not agrees:
        return [_failed(r, REASON_SUMMARY_DISAGREES) for r in results]
    return results


def _failed(result: AxisResult, reason: str) -> AxisResult:
    result.passed = False
    result.reason = result.reason or reason
    return result


def _line_dict(line: Any) -> dict:
    return line.to_json() if isinstance(line, LoopLine) else dict(line)


def _axis_pass(entry: Any) -> bool:
    return isinstance(entry, Mapping) and entry.get("pass") is True


def _check_lines(loop_lines: Iterable[Any]) -> list:
    return [d for d in (_line_dict(x) for x in loop_lines or ()) if d.get("kind", LINE_CHECK) == LINE_CHECK]


def close_verdict(loop_lines: Iterable[Any], axis_ids: Sequence[str] = ()) -> dict:
    """`{status: done|not_met, unmet: [axis ids]}` folded from the last check line only.

    Calibration, reacher and rethink lines are skipped. An absent or empty loop, or one holding no
    check line, is `not_met` with every id in `axis_ids` unmet. Never indeterminate.
    """
    checks = _check_lines(loop_lines)
    if not checks:
        return {"status": STATUS_NOT_MET, "unmet": list(axis_ids), "reason": REASON_NO_CHECK}
    axes = checks[-1].get("axes") or []
    unmet = [str(a.get("axis")) for a in axes if not _axis_pass(a)]
    seen = {str(a.get("axis")) for a in axes if isinstance(a, Mapping)}
    unmet += [i for i in axis_ids if i not in seen]
    if not axes or unmet:
        return {"status": STATUS_NOT_MET, "unmet": unmet}
    return {"status": STATUS_DONE, "unmet": []}


def objective_loop_result(
    loop_lines: Iterable[Any],
    calibration: Optional[Any] = None,
    *,
    rethinks: Sequence[Any] = (),
    outcome: Optional[str] = None,
    exit_reason: Optional[str] = None,
    axis_ids: Sequence[str] = (),
) -> dict:
    """The RESULT `objective_loop` block.

    `outcome` (impossible|doctrine_conflict) and `exit_reason` come from the rethink or ceiling path;
    absent them, outcome derives from the fold: a pass is met, an unmet fold is exhausted
    (non-terminal, reason unresolved), so a run never reports an open or indeterminate outcome.
    `first_pass` says whether the loop was needed; it is not an outcome.
    """
    lines = [_line_dict(x) for x in loop_lines or ()]
    verdict = close_verdict(lines, axis_ids)
    iterations = []
    for line in lines:
        kind = line.get("kind", LINE_CHECK)
        if kind not in (LINE_CHECK, LINE_REACHER):
            continue
        iterations.append({
            "iteration": line.get("iteration"),
            "wave": line.get("wave"),
            "falsifier_lines": line.get("falsifier_lines") or line.get("axes") or [],
            "delta_plan_path": line.get("delta_plan_path"),
            "spend_usd": line.get("spend_usd", 0.0),
            "check_cost_usd": line.get("check_cost_usd", 0.0),
            "check_llm_calls": line.get("check_llm_calls", 0),
        })
    earlier = _check_lines(lines)[:-1]
    retried = any(d.get("kind") == LINE_REACHER for d in lines)
    failed_before = any(not all(_axis_pass(a) for a in (d.get("axes") or [])) or not d.get("axes") for d in earlier)
    first_pass = verdict["status"] == STATUS_DONE and not (retried or failed_before)
    if outcome is None:
        if verdict["status"] == STATUS_DONE:
            outcome = OUTCOME_MET
            exit_reason = exit_reason or EXIT_VERIFIED
        else:
            outcome = OUTCOME_EXHAUSTED
            exit_reason = exit_reason or EXIT_UNRESOLVED
    elif outcome not in OUTCOMES:
        raise ValueError(f"outcome {outcome!r} is not one of {OUTCOMES}")
    cal = calibration.to_json() if isinstance(calibration, Calibration) else calibration
    return {
        "iterations": iterations,
        "outcome": outcome,
        "first_pass": first_pass,
        "exit_reason": exit_reason,
        "unmet_axes": verdict["unmet"],
        "rethinks": [asdict(r) if isinstance(r, Rethink) else dict(r) for r in rethinks],
        "calibration": cal,
    }
