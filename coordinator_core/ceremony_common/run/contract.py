"""Ceremony-neutral runner contract: step and ledger shapes, digest and agent-results validation.

Plain data and dataclasses; the only I/O is reading the digest schema file. Every ceremony's
spec, the runner, and the DoE workflow template build against these names.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

STEP_KINDS: tuple[str, ...] = ("cli", "op", "inproc")
COST_CLASSES: tuple[str, ...] = ("engine", "remote", "over-bar", "unmeasured", "suite", "launch")
STEP_STATUSES: tuple[str, ...] = ("ran", "cadence-skipped", "not-composed", "halted-by-gate", "failed")
DIGEST_STATES: tuple[str, ...] = ("ran", "already-ran", "in-flight", "failed")

# Kill deadline per cost class, in seconds. A ceiling on a hung child, not an estimate: `suite`
# bounds one test-suite child, `launch` one chained ceremony launch, which kills its own steps
# by their own deadlines. Read against the 500ms bar, not a measurement.
DEADLINE_S: Mapping[str, float] = {
    "engine": 10.0,
    "unmeasured": 30.0,
    "remote": 60.0,
    "over-bar": 0.0,
    "suite": 900.0,
    "launch": 1200.0,
}

# Dispositions that name a person as the actor. No decider is offered one; no Decision carries one.
HANDBACK_DISPOSITIONS: frozenset[str] = frozenset({"surface_to_pm", "pm_reviews_manually"})

# Removed from every judgment entry beside the handback set, for every ceremony.
NEVER_OFFERED_DISPOSITIONS: frozenset[str] = frozenset({"discard"})

LEDGER_ROW_KEYS: tuple[str, ...] = (
    "id", "status", "reason", "exit_code", "process_ms", "spawns", "stdout", "stderr",
)
DECISIONS_FILE = "decisions.jsonl"

# `<git-common-dir>/coordinator-sessions/<ceremony><RUN_DIR_SUFFIX>/<run_id>`; ledger.run_dir builds it.
RUN_DIR_SUFFIX = "-runs"
RUN_ID_RE = re.compile(r"\d{8}T\d{6}Z-\d{6}[0-9a-f]{4}")

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

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contract" / "workday-start-run-digest.schema.json"


@dataclass(frozen=True)
class StepSpec:
    """One step: the directive record's keys plus the run fields.

    `stdin_from` names the step whose stdout feeds this step's stdin; `best_effort` makes a
    failure of this step non-fatal to the stage.
    """

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
    stdin_from: str | None = None
    best_effort: bool = False


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

    `rule` is a ceremony rule id when a rule decided, else empty. `acted` lists the ledger
    step ids and commits the decision caused; empty when it leaves things as they are.
    `payload` carries the structured data a decision hands to the finish steps it opens.
    """

    point_id: str
    question: str
    decision: str
    decided_by: str
    rule: str
    rationale: str
    evidence: str
    acted: tuple[str, ...] = ()
    payload: Mapping[str, Any] | None = None

    def to_row(self) -> dict[str, Any]:
        row = {k: getattr(self, k) for k in self.__dataclass_fields__}
        row["acted"] = list(self.acted)
        if self.payload is None:
            del row["payload"]
        else:
            row["payload"] = dict(self.payload)
        return row


def step_from_directive(
    directive: Mapping[str, Any],
    *,
    stage: str,
    heading: str,
    cost_class: str,
    kind: str | None = None,
    gates_next: bool = False,
    on_exit: Mapping[int, str] | None = None,
) -> StepSpec:
    """Convert a `directives[]` entry to a StepSpec.

    Reads `id`, `cli` (as `target`), `args`, `depends_on`, `already_satisfied`, `detail`,
    `stdin_from` and `best_effort`, and ignores any other key. A record with no `cli` is an op
    record: `op` becomes the target and the kind is `op`. Never decides readiness: the caller
    hands in only steps whose gate is open.
    """
    if directive.get("cli"):
        target, resolved_kind = directive["cli"], kind or "cli"
    else:
        target, resolved_kind = directive["op"], "op"
    return StepSpec(
        id=directive["id"],
        stage=stage,
        kind=resolved_kind,
        target=target,
        args=tuple(directive.get("args") or ()),
        depends_on=directive.get("depends_on"),
        already_satisfied=bool(directive.get("already_satisfied")),
        detail=directive.get("detail") or "",
        heading=heading,
        cost_class=cost_class,
        gates_next=gates_next,
        on_exit=dict(on_exit or {}),
        stdin_from=directive.get("stdin_from"),
        best_effort=bool(directive.get("best_effort")),
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
    chained: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a digest and raise ValueError listing schema errors if it is invalid.

    `chained` is the chained launch's own digest; the key is absent when there is none.
    """
    digest: dict[str, Any] = {
        "ceremony": ceremony,
        "run_id": run_id,
        "repo_root": repo_root,
        "state": state,
        "briefing_path": briefing_path,
        "finish_argv": list(finish_argv),
        "agent_work": {"groups": groups},
    }
    if chained is not None:
        digest["chained"] = dict(chained)
    errors = validate_digest(digest)
    if errors:
        raise ValueError("invalid digest: " + "; ".join(errors))
    return digest
