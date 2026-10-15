"""Names shared by the warp --ask seam: phases, op names, halt kinds, gate verdict, stage manifest.

Signatures implemented in their own modules:
  ask_gate.gate(repo_root: Path, sizing_rel: str, *, writes: Sequence[str] = ()) -> GateVerdict
  ask_stage.stage(repo_root: Path, *, run_id: str, plan_rel: str | None = None,
                  sizing_rel: str | None = None, writes: Sequence[str] = ()) -> StageManifest
  ask_compose.compose_ask_script(*, repo_root: str, prompt: str | None, sizing_rel: str | None,
                  run_id: str, session_id: str | None, baton: dict | None = None,
                  accept_pending: bool = False, em_brief: EmBrief | None = None,
                  stop_after: str | None = None, ask_source: str = "pm") -> str
                  (`baton` = {"path", "deliverable_id"}; the accept phase composes when
                  `accept_pending` or on a raw ask)
  ask_plan_blitz.wrap_stage(plan_blitz_text: str, *, agent_prefix_var: str | None = None)
                  -> tuple[str, list[str]]
  sizing_fire.resumable_plan(sizing, sizing_rel: str, repo_root, arm) -> ResumePlan | None
                  (`ResumePlan` is declared here; a plan with every row landed raises SizingFireRefused)
  ask_brief.load_em_brief(repo_root, *, brief, brief_file, context=(), session_id="") -> EmBrief | None
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.session.record_homes import home_dir

ASK_PHASES = ("size", "gate", "accept", "plan", "stage", "execute", "review")
#: Serial groups, in run order. "stage" is also the one title the standalone stage fallback and
#: the standalone falsifier share when both spawn (`ASK_STAGE_PHASE`).
ASK_STAGE_PHASE = "stage"
ASK_FALSIFIER_PHASE = ASK_STAGE_PHASE
#: The merged tail agent: scoped tests, criterion and ledger verdict, declared docs. Its label
#: and its single phase title; it replaces the "Scoped test run", "Criterion judge", "docs" and
#: "ledger-check" legs.
ASK_VERIFY_LABEL = "verify"
ASK_VERIFY_PHASE = "Verify"
#: Keys the merged leg must return, pinned by `test_ask_contract`; `ask_compose._verify_schema`
#: builds the real schema from its own properties and does not read these tuples. `test` and
#: `judge` are objects holding the first two tuples (their `status` keys collide, so they nest);
#: ledger and docs keys sit at the top level.
ASK_VERIFY_TEST_KEYS = ("status", "summary")
ASK_VERIFY_JUDGE_KEYS = ("status", "observation", "differs_from_baseline", "baseline_output", "roots_read")
ASK_VERIFY_LEDGER_KEYS = ("items", "all_of_pm_ask", "conflicts")
ASK_VERIFY_DOCS_KEYS = ("doc_paths", "note_path", "stale_docs")
ASK_VERIFY_RETURN_KEYS = ("test", "judge", *ASK_VERIFY_LEDGER_KEYS, *ASK_VERIFY_DOCS_KEYS)
OP_ASK_GATE, OP_ASK_STAGE = "dispatch.ask_gate", "dispatch.ask_stage"
HALT_ROOM, HALT_TOUCHPOINT, HALT_REFUSAL = "room", "touchpoint", "refusal"
HALT_SPLIT = "split"
STOP_AFTER_PLAN = "plan"
SIZING_SCOUT_AGENT_TYPE = "coordinator:sizing-scout"
NO_PLAN_HAND_OFF_FLAG = "--no-plan-hand-off"
DOC_NEW_CMD = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-doc-new"'
SIZING_ASSEMBLE_CMD = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/sizing-assemble"'
#: The sizing scout's return fields; every scout prompt concatenates this one passage.
SCOUT_RETURN_CLAUSE = (
    "Return its inputs: tshirt (XS|S|M|L|XL|XXL), intent (one sentence), "
    "name (a short kebab-case slug), premise_provenance (executed|read|not-applicable) with "
    "premise_evidence, exit_criterion (one sentence stating what done means; never name a suite tier or a "
    "test-runner command in it -- write 'tests covering touched files pass'), and as `writes` "
    "the repo-relative files the ask will create, edit or delete -- every file the ask names, "
    "plus any you find it must touch. An XS with empty `writes` is refused at the gate. Set "
    "jtbd_unclear true when the job the ask serves is unclear (it routes the ask to shaping), "
    "and well_trodden_step_change true only when the ask proposes a new direction on ground "
    "recently built (it also routes the ask to shaping, a PM conversation). An ask that "
    "continues a workstream an existing sizing, plan, ruling or baton already scoped is "
    "not a step change, however much it rebuilds. Return "
    "as `gated` every deliverable the ask declares as owned by another repo or blocked on an "
    "external gate (title, owner_repo, requires): never fold one into `writes` and never omit "
    "one."
)
_STR = {"type": "string"}
SCOUT_SCHEMA = {
    "type": "object",
    "required": ["tshirt", "intent", "name", "premise_provenance", "premise_evidence", "exit_criterion", "writes"],
    "properties": {
        "tshirt": {"type": "string", "enum": ["XS", "S", "M", "L", "XL", "XXL"]},
        "intent": _STR,
        "name": _STR,
        "premise_provenance": {"type": "string", "enum": ["executed", "read", "not-applicable"]},
        "premise_evidence": _STR,
        "exit_criterion": _STR,
        "writes": {"type": "array", "items": _STR},
        "jtbd_unclear": {"type": "boolean"},
        "well_trodden_step_change": {"type": "boolean"},
        "gated": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "owner_repo"],
                "properties": {"title": _STR, "owner_repo": _STR, "requires": _STR},
            },
        },
    },
}
#: The ask scout's return: SCOUT_SCHEMA plus its closing acts (sizing-assemble, then
#: dispatch.ask_gate). Separate so the grind scout's embedded schema stays byte-identical.
ASK_SCOUT_SCHEMA = {
    **SCOUT_SCHEMA,
    "properties": {
        **SCOUT_SCHEMA["properties"],
        "exit_code": {"type": "integer"},
        "stdout": _STR,
        "stderr": _STR,
        "arm": {"type": ["string", "null"]},
        "halt": {"type": ["object", "null"]},
        "baton": {"type": ["object", "null"]},
        "resume_plan": {"type": ["string", "null"]},
        "resume_rows": {"type": ["array", "null"], "items": _STR},
        "spine_plan": {"type": ["string", "null"]},
        "route": _STR,
        "manifest": {"type": ["object", "null"]},
        "falsifier": {"type": ["object", "null"]},
    },
}
ASSEMBLE_SCHEMA = {
    "type": "object",
    "required": ["exit_code", "stdout"],
    "properties": {"exit_code": {"type": "integer"}, "stdout": _STR, "stderr": _STR},
}


def scout_assemble_flags_js(scouted: str, quote: str, extra: str = "") -> str:
    """JS expression text appending the sizing-assemble flags a scout's return fills.

    ``scouted`` names the JS variable holding the scout's return and ``quote`` the JS
    shell-quoting function in scope; ``extra`` is spliced after ``--exit-criterion``.
    Starts with `` + `` so it follows the command literal directly.
    """
    return (
        f" + ' --tshirt ' + {quote}({scouted}.tshirt)"
        f" + ' --intent ' + {quote}({scouted}.intent) + ' --name ' + {quote}({scouted}.name)"
        f" + ' --premise-provenance ' + {quote}({scouted}.premise_provenance)"
        f" + ' --premise-evidence ' + {quote}({scouted}.premise_evidence)"
        f" + ' --exit-criterion ' + {quote}({scouted}.exit_criterion){extra}"
        f" + ({scouted}.jtbd_unclear === true ? ' --jtbd-unclear' : '')"
        f" + ({scouted}.well_trodden_step_change === true ? ' --well-trodden-step-change' : '')"
    )
PM_EXIT_QUESTION_KEY = "pm_exit_question"
RUN_DIR_ROOT = "scratch/warp"
#: Engine-written beside the run's briefs: the ask as submitted (raw asks), and the PM's verbatim
#: intent every executor brief carries and every reviewer, judge and docs prompt reads first.
ASK_TEXT_FILE = "ask.md"
PM_INTENT_FILE = "pm-intent.md"
#: A relayed ask (an EM's relay, not the PM's words) lives here and never in `ask.md`, so `ask.md`
#: holds PM words or does not exist.
RELAYED_ASK_FILE = "relayed-ask.md"
ASK_SOURCE_PM, ASK_SOURCE_RELAYED = "pm", "relayed"
ASK_SOURCES = (ASK_SOURCE_PM, ASK_SOURCE_RELAYED)
RELAYED_ASK_HEADING = "## Relayed ask (not the PM's words)"
ASK_MANIFEST_MARKER = "// coordinator:ask-run-manifest v1 "
RECORD_DIR_ROOT = Path(home_dir(".", "warp")).as_posix()
SPINE_SUFFIX = ".spine.md"
#: Fixed record names a run's record directory holds, plus any `*SPINE_SUFFIX` file.
RUN_RECORD_FILES = (
    "requirements.jsonl",
    "run.json",
    "requirements.md",
    "manifest.json",
    ASK_TEXT_FILE,
    PM_INTENT_FILE,
    RELAYED_ASK_FILE,
)
#: `dispatch.terminal_commit` params key whose dict value (a non-empty str `halted`) selects the
#: halted-run record-only mode.
HALTED_RECORD_PARAM = "halted_by"
HALT_REASON_FIELDS = ("reason", "detail", "refused", "error")
HALT_REASON_MAX_CHARS = 300


def halted_record_message(run_id: str, halted: dict) -> str:
    """Commit message for a halted run's record-only commit: subject, blank line, `Halted-By:`
    and, when the halt carries a reason, one `Halt-Reason:` line (first non-empty of
    `HALT_REASON_FIELDS`, whitespace collapsed, cut to `HALT_REASON_MAX_CHARS`). Pure."""
    lines = [f"warp: record halted {run_id} run artifacts", "", f"Halted-By: {halted.get('halted')}"]
    for field in HALT_REASON_FIELDS:
        value = halted.get(field)
        text = " ".join(str(value).split()) if value else ""
        if text:
            lines.append(f"Halt-Reason: {text[:HALT_REASON_MAX_CHARS]}")
            break
    return "\n".join(lines)


def record_dir(repo_root: Path, run_id: str) -> Path:
    """The run's record directory: `state/warp/<run_id>`, except an in-flight run whose record
    file sits only under `scratch/warp/<run_id>`. Only a record file selects scratch, never the
    directory's existence. Raises ValueError on an unsafe run_id or an escape."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        raise ValueError(f"run_id {run_id!r} is not a single safe path segment")
    root = Path(repo_root)
    state = contained_path(root / RECORD_DIR_ROOT / run_id, [root / RECORD_DIR_ROOT])
    if state is None:
        raise ValueError(f"record dir for {run_id!r} escapes {RECORD_DIR_ROOT}")
    if state.is_dir():
        return state
    scratch = contained_path(root / RUN_DIR_ROOT / run_id, [root / RUN_DIR_ROOT])
    if scratch is not None and scratch.is_dir():
        if any((scratch / name).is_file() for name in RUN_RECORD_FILES) or any(
            child.name.endswith(SPINE_SUFFIX) for child in scratch.iterdir()
        ):
            return scratch
    return state
#: The run's delivery note (`<dir>/<run_id>.md`): what was asked, what shipped, review findings,
#: judge verdict. The first of an ask run's declared doc writes.
DELIVERY_NOTE_DIR = "docs/delivery"


class ResumePlan(NamedTuple):
    """`sizing_fire.resumable_plan`'s return: the plan to resume and the ids of its rows still
    to run, in spine order."""

    plan_rel: str
    incomplete: tuple[str, ...]


@dataclass(frozen=True)
class GateVerdict:
    """dispatch.ask_gate reply: an arm to run, or a halt dict
    {"kind": HALT_*, "reason": str, "touchpoint"?: str, "route"?: str}; baton is the
    minted {"id", "path"} at an M+ arm."""

    arm: str | None
    halt: dict | None
    baton: dict | None = None
    # Set only when the engine's size rule discharged a null `exit_criterion.accepted`: the
    # sizing object cannot carry this, so the verdict is the record that nobody was asked.
    acceptance: dict | None = None
    # Plan path of an accepted, unfired sizing: the script stages it instead of authoring one.
    resume_plan: str | None = None
    # With `resume_plan`: the ids of its rows still to run; the script stages only these.
    resume_rows: list[str] | None = None
    # Plan path of the spine the gate minted at M+ from a sizing whose acceptance rows are all
    # concrete: the script reviews it instead of planning.
    spine_plan: str | None = None
    # Set only when the request carried `record`: {"ok": bool, "error"?: str}, the ruling's write.
    recorded: dict | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"arm": self.arm, "halt": self.halt, "baton": self.baton}
        if self.recorded is not None:
            out["recorded"] = self.recorded
        if self.acceptance is not None:
            out["acceptance"] = self.acceptance
        if self.resume_plan is not None:
            out["resume_plan"] = self.resume_plan
        if self.resume_rows is not None:
            out["resume_rows"] = list(self.resume_rows)
        if self.spine_plan is not None:
            out["spine_plan"] = self.spine_plan
        return out


@dataclass(frozen=True)
class ManifestRow:
    id: str
    agent_type: str
    model: str
    brief_path: str
    writes: tuple[str, ...]
    wave: int
    #: The rows this one waits on (`DagNode.after`): never a whole earlier wave.
    deps: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_type": self.agent_type,
            "model": self.model,
            "brief_path": self.brief_path,
            "writes": list(self.writes),
            "wave": self.wave,
            "deps": list(self.deps),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ManifestRow":
        return cls(
            id=data["id"],
            agent_type=data["agent_type"],
            model=data["model"],
            brief_path=data["brief_path"],
            writes=tuple(data["writes"]),
            wave=data["wave"],
            deps=tuple(data.get("deps") or ()),
        )


@dataclass(frozen=True)
class GatedRow:
    """A row withheld from the run; ``reason`` is ``external_gate`` or
    ``transitive_gate_closure`` and ``gate`` describes the blocking gate.
    ``owner_root`` is the owner repo's checkout on this box, filled at stage time;
    ``""`` when the box's registry has no entry for it."""

    id: str
    reason: str
    gate: str
    owner_repo: str = ""
    closure_key: Any = None
    owner_root: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "reason": self.reason, "gate": self.gate,
            "owner_repo": self.owner_repo, "closure_key": self.closure_key,
            "owner_root": self.owner_root,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GatedRow":
        return cls(
            id=data["id"], reason=data["reason"], gate=data["gate"],
            owner_repo=data.get("owner_repo") or "", closure_key=data.get("closure_key"),
            owner_root=data.get("owner_root") or "",
        )


@dataclass(frozen=True)
class OwedRow:
    """A row the run excluded because the EM or an operator performs it. ``reason`` is one of
    ``spine_read.EM_OWNED_REASONS``: ``em-performed``, ``memo-send``, ``operator``, or
    ``withheld_by_owed_dependency`` (a row transitively behind one of those)."""

    id: str
    reason: str
    detail: str

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "reason": self.reason, "detail": self.detail}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "OwedRow":
        return cls(id=data["id"], reason=data["reason"], detail=data.get("detail") or "")


@dataclass(frozen=True)
class StageManifest:
    """dispatch.ask_stage reply, also written as manifest.json in the run dir.
    ``gated`` lists rows withheld, never staged; an absent key reads as ``()``.
    ``owed`` lists rows the run excluded because the EM or an operator performs them, and the
    run never performs them; an absent key reads as ``()``.
    ``test_targets`` is the spine's terminal test scope; ``no_test_target`` is set (``{reason,
    unmapped_paths, testable_omissions}``) when the locator found no runnable target for a
    testable write -- the run's tests read degraded, never pass. ``doc_writes`` is the docs
    stage's declared write set, carried in the commit request's ``docs`` chunk."""

    run_dir: str
    rows: tuple[ManifestRow, ...]
    review_declared_paths: tuple[str, ...]
    marker_path: str
    plan_id: str | None = None
    gated: tuple[GatedRow, ...] = ()
    test_targets: tuple[str, ...] = ()
    no_test_target: dict[str, Any] | None = None
    doc_writes: tuple[str, ...] = ()
    #: Rows delivered in the engine plane, in the owner-clause row shape `{id, owner_repo,
    #: owner_root, root_class, engine}`; an absent key reads as ``()``.
    engine_rows: tuple[dict[str, Any], ...] = ()
    owed: tuple[OwedRow, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "owed": [o.to_json() for o in self.owed],
            "engine_rows": [dict(r) for r in self.engine_rows],
            "plan_id": self.plan_id,
            "run_dir": self.run_dir,
            "rows": [r.to_json() for r in self.rows],
            "review_declared_paths": list(self.review_declared_paths),
            "marker_path": self.marker_path,
            "gated": [g.to_json() for g in self.gated],
            "test_targets": list(self.test_targets),
            "no_test_target": self.no_test_target,
            "doc_writes": list(self.doc_writes),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StageManifest":
        return cls(
            run_dir=data["run_dir"],
            rows=tuple(ManifestRow.from_json(r) for r in data["rows"]),
            review_declared_paths=tuple(data["review_declared_paths"]),
            marker_path=data["marker_path"],
            plan_id=data.get("plan_id"),
            gated=tuple(GatedRow.from_json(g) for g in data.get("gated") or ()),
            test_targets=tuple(data.get("test_targets") or ()),
            no_test_target=data.get("no_test_target"),
            doc_writes=tuple(data.get("doc_writes") or ()),
            engine_rows=tuple(dict(r) for r in data.get("engine_rows") or ()),
            owed=tuple(OwedRow.from_json(o) for o in data.get("owed") or ()),
        )
