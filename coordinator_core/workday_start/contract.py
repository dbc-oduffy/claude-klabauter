"""Runner contract: stages, step and ledger shapes, digest and agent-results validation.

Plain data and dataclasses; the only I/O is reading the digest schema file. Every other
module in the package, and the DoE workflow template, builds against these names.
"""
from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

LAUNCH_STAGES: tuple[str, ...] = (
    "hygiene",
    "step0",
    "orient",
    "normalize",
    "triage",
    "memos",
    "inbox-assemble",
    "rollup",
    "sentinels",
    "freshness",
    "weekly",
    "backfill-scan",
    "alignment-inputs",
    "decide",
)

FINISH_STAGES: tuple[str, ...] = (
    "decisions",
    "failure-backlog",
    "briefing",
    "goal-event",
    "marker",
    "orientation-cache",
    "copies-cache",
    "structural-index",
    "ceremony-hook",
)

STEP_KINDS: tuple[str, ...] = ("cli", "op", "inproc")
COST_CLASSES: tuple[str, ...] = ("engine", "remote", "over-bar", "unmeasured")
STEP_STATUSES: tuple[str, ...] = ("ran", "cadence-skipped", "not-composed", "halted-by-gate", "failed")
DIGEST_STATES: tuple[str, ...] = ("ran", "already-ran", "in-flight", "failed")

# Kill deadline per cost class, in seconds. A choice read against the 500ms bar, not a measurement.
DEADLINE_S: Mapping[str, float] = {
    "engine": 10.0,
    "unmeasured": 30.0,
    "remote": 60.0,
    "over-bar": 0.0,
}

RULE_IDS: tuple[str, ...] = (
    "step0-stale-branch",
    "step0-reconcile-outcome",
    "recommendation",
    "memo-inbox",
    "no-live-disposition",
)

# Dispositions that name a person as the actor. No decider is offered one; no Decision carries one.
HANDBACK_DISPOSITIONS: frozenset[str] = frozenset({"surface_to_pm", "pm_reviews_manually"})

LEDGER_ROW_KEYS: tuple[str, ...] = (
    "id", "status", "reason", "exit_code", "process_ms", "spawns", "stdout", "stderr",
)
DECISIONS_FILE = "decisions.jsonl"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_IN_FLIGHT = 3
EXIT_TEMPLATE_MISSING = 4
EXIT_GATE_FAILED = 5

NARRATION_DENY_LIST: tuple[str, ...] = (
    "Next up",
    "Still to do",
    "Remaining steps",
    "TODO",
    "Parked for you",
    "once the PM has ruled",
    "your call",
    "for you to decide",
    "awaiting your",
    "needs your",
    "let me know",
    "please confirm",
)

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contract" / "workday-start-run-digest.schema.json"


@dataclass(frozen=True)
class StepSpec:
    """One step: the directive record's six keys plus the run fields."""

    id: str
    stage: str
    kind: str
    target: str
    args: tuple[str, ...]
    depends_on: str | None
    already_satisfied: bool
    detail: str
    heading: str
    cost_class: str
    gates_next: bool = False
    on_exit: Mapping[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    """The terminal ledger row of one step.

    `reason` is empty for a plain run; otherwise `already-satisfied`, the gating step's id
    (halted-by-gate), `deadline`, the breached budget dial, an over-bar step's bug-backlog id,
    or `resolved-by:<rule id>`.
    """

    id: str
    status: str
    reason: str = ""
    exit_code: int | None = None
    process_ms: float | None = None
    spawns: int | None = None
    stdout: str = ""
    stderr: str = ""

    def to_row(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in LEDGER_ROW_KEYS}


@dataclass(frozen=True)
class Decision:
    """One judgment point's decision. `decided_by` is `rule` or `agent:<subagent type>`.

    `rule` is a member of RULE_IDS when a rule decided, else empty. `acted` lists the ledger
    step ids and commits the decision caused; empty when it leaves things as they are.
    """

    point_id: str
    question: str
    decision: str
    decided_by: str
    rule: str
    rationale: str
    evidence: str
    acted: tuple[str, ...] = ()

    def to_row(self) -> dict[str, Any]:
        row = {k: getattr(self, k) for k in self.__dataclass_fields__}
        row["acted"] = list(self.acted)
        return row


def step_from_directive(
    directive: Mapping[str, Any],
    *,
    stage: str,
    heading: str,
    cost_class: str,
    kind: str = "cli",
    gates_next: bool = False,
    on_exit: Mapping[int, str] | None = None,
) -> StepSpec:
    """Convert a `directives[]` entry to a StepSpec.

    Reads the six directive keys (`cli` as `target`) and ignores any other key. Never decides
    readiness: the caller hands in only steps whose gate is open.
    """
    return StepSpec(
        id=directive["id"],
        stage=stage,
        kind=kind,
        target=directive["cli"],
        args=tuple(directive.get("args") or ()),
        depends_on=directive.get("depends_on"),
        already_satisfied=bool(directive.get("already_satisfied")),
        detail=directive.get("detail") or "",
        heading=heading,
        cost_class=cost_class,
        gates_next=gates_next,
        on_exit=dict(on_exit or {}),
    )


@functools.cache
def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(obj: Any, def_name: str) -> list[str]:
    from coordinator_core.frontmatter.schema_validate import _validate_json_schema_node

    schema = _schema()
    errors = _validate_json_schema_node(obj, {"$ref": f"#/$defs/{def_name}"}, schema)
    return [f"{e['field']}: {e['error']}" for e in errors]


def validate_digest(obj: Any) -> list[str]:
    """Return schema errors for a digest; an empty list means valid."""
    return _validate(obj, "digest")


def validate_agent_results(obj: Any) -> list[str]:
    """Return schema errors for an agent-results object; an empty list means valid."""
    return _validate(obj, "agent_results")


def build_digest(
    *,
    ceremony: str,
    run_id: str,
    repo_root: str,
    state: str,
    finish_argv: list[str],
    groups: list[dict[str, Any]],
    briefing_path: str | None = None,
) -> dict[str, Any]:
    """Assemble a digest and raise ValueError listing schema errors if it is invalid."""
    digest = {
        "ceremony": ceremony,
        "run_id": run_id,
        "repo_root": repo_root,
        "state": state,
        "briefing_path": briefing_path,
        "finish_argv": list(finish_argv),
        "agent_work": {"groups": groups},
    }
    errors = validate_digest(digest)
    if errors:
        raise ValueError("invalid digest: " + "; ".join(errors))
    return digest
