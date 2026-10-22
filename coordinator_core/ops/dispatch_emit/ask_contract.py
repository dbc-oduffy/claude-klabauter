"""Names shared by the warp --ask seam: phases, op names, halt kinds, gate verdict, stage manifest.

Signatures implemented in their own modules:
  ask_gate.gate(repo_root: Path, sizing_rel: str, *, writes: Sequence[str] = (),
                 run_id: str | None = None) -> GateVerdict
  ask_stage.stage(repo_root: Path, *, run_id: str, plan_rel: str | None = None,
                  sizing_rel: str | None = None, writes: Sequence[str] = (),
                  ledger_resume: bool = False) -> StageManifest
                  (`ledger_resume=True` admits a spine with zero dispatchable rows, returning a
                  whole manifest with rows [])
  ask_compose.compose_ask_script(*, repo_root: str, prompt: str | None, sizing_rel: str | None,
                  run_id: str, session_id: str | None, baton: dict | None = None,
                  accept_pending: bool = False, em_brief: EmBrief | None = None,
                  stop_after: str | None = None, ask_source: str = "pm") -> str
                  (`baton` = {"path", "deliverable_id"}; the accept phase composes when
                  `accept_pending` or on a raw ask)
  ask_plan_blitz.wrap_stage(plan_blitz_text: str, *, agent_prefix_var: str | None = None)
                  -> tuple[str, list[str]]
  sizing_fire.resumable_plan(sizing, sizing_rel: str, repo_root, arm) -> ResumePlan | None
                  (`ResumePlan` is declared here; a plan with every row landed raises SizingFireRefused
                  unless a ledger-halt pointer names it, then `ResumePlan.ledger` carries the pointer;
                  an all-landed plan with a rulable register row resumes with a synthesised
                  `LedgerHalt(halted=ALL_LANDED_HALT_KIND)`, never written by write_ledger_halt)
  ask_brief.load_em_brief(repo_root, *, brief, brief_file, context=(), session_id="") -> EmBrief | None
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, NamedTuple, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops import _research_contract as _rc
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit.design_route import DESIGN_ROUTE_KEY, ROUTES as DESIGN_ROUTES
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.route_preference import ROUTE_PREFERENCE_FILE, ROUTE_VERDICT_KEY
from coordinator_core.session.record_homes import home_dir

ASK_PHASES = ("size", "gate", "accept", "plan", "stage", "execute", "review")
#: Serial groups, in run order. "stage" is the title ask_compose no longer composes (no standalone stage agent); the constant remains for `ASK_FALSIFIER_PHASE`.
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
HALT_DEPENDENTS_UNSTARTED = "dependents_unstarted"
HALT_SPLIT = "split"
HALT_RESEARCH = "research"
#: Raised by the ask script when an ask_stage refusal starts with design_route.STAGE_REFUSAL_PREFIX;
#: the PM's recorded design_approval clears it.
HALT_DESIGN_REVIEW = "design-review"
#: Halt kind of a run revised to stop after a phase: a clean terminal that commits the run artifacts.
HALT_PARKED = "parked"
STOP_AFTER_PLAN = "plan"
#: The one name for a stop-after phase: the engine-ask `args` key, the `asks[i]` key, the emit param
#: and the slice sizing's `em_analysis` key. Only `STOP_AFTER_PLAN` is a legal value.
STOP_AFTER_KEY = "stop_after"
_PARKED_NEXT_ACTION_HEAD = "Run /mise-prep on "
_PARKED_NEXT_ACTION_TAIL = ", then /mise-en-place."
_PARKED_NEXT_ACTION_NO_PLAN = "the run's plan"


def parked_next_action(plan_rel: str | None) -> str:
    """The `next_action` of a parked halt: what the EM runs on the landed plan."""
    return f"{_PARKED_NEXT_ACTION_HEAD}{plan_rel or _PARKED_NEXT_ACTION_NO_PLAN}{_PARKED_NEXT_ACTION_TAIL}"


def parked_next_action_js(plan_expr: str) -> str:
    """A JS expression rendering `parked_next_action` over the runtime variable `plan_expr`."""
    return (
        f"({plan_expr} ? {json.dumps(_PARKED_NEXT_ACTION_HEAD)} + {plan_expr} + {json.dumps(_PARKED_NEXT_ACTION_TAIL)}"
        f" : {json.dumps(_PARKED_NEXT_ACTION_HEAD + _PARKED_NEXT_ACTION_NO_PLAN + _PARKED_NEXT_ACTION_TAIL)})"
    )


#: Record-dir file holding a run's revised stop-after phase.
STOP_AFTER_FILE = "stop-after.json"
#: `dispatch.ask_stage` reply key marking a parked run (no manifest handle follows).
PARKED_REPLY_KEY = "parked"
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
    "required": [*SCOUT_SCHEMA["required"], "exit_code"],
    "properties": {
        **SCOUT_SCHEMA["properties"],
        "exit_code": {"type": "integer"},
        "stdout": _STR,
        "stderr": _STR,
        "arm": {"type": ["string", "null"]},
        "halt": {"type": ["object", "null"]},
        "baton": {"type": ["object", "null"]},
        "sizing_path": {"type": ["string", "null"]},
        "resume_plan": {"type": ["string", "null"]},
        "resume_rows": {"type": ["array", "null"], "items": _STR},
        "spine_plan": {"type": ["string", "null"]},
        "route": _STR,
        "route_override_reason": {"type": ["string", "null"]},
        ROUTE_VERDICT_KEY: {"type": ["object", "null"]},
        "design_route": {"type": "string", "enum": list(DESIGN_ROUTES)},
        "manifest": {"type": ["object", "null"]},
        "falsifier": {"type": ["object", "null"]},
        "research": {
            "type": ["object", "null"],
            "required": ["class"],
            "properties": {
                "class": {"type": "string", "enum": list(_rc.VALUE_CLASSES)},
                "appetite": {"type": "string", "enum": list(_rc.APPETITES)},
                "sources": {"type": "array", "items": {"type": "string", "enum": list(_rc.SOURCES)}},
                "depth": {"type": "string", "enum": list(_rc.DEPTHS)},
                "targets": {"type": "array", "items": _STR},
                "seeds": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "pattern": r"^(" + "|".join(_rc.SEED_KINDS) + r")=\S+$",
                    },
                },
                "questions": {"type": "array", "items": _STR},
            },
        },
        "acceptance_rows": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "required": ["statement", "writes", "check"],
                "properties": {
                    "statement": _STR,
                    "writes": {"type": "array", "items": _STR},
                    "check": _STR,
                },
            },
        },
    },
}
#: Scout prompt passage: when to return `acceptance_rows`. Appended to the ask scout's prompt.
ACCEPTANCE_ROWS_CLAUSE = (
    "Return `acceptance_rows` (each row: statement, writes, check) only when every row can name "
    "its file writes -- exact files, no glob and no directory -- and a runnable check that proves "
    "it; otherwise return it null. Never pad rows to fill the field."
)
#: Scout prompt passage: when to return `research`. Appended to the ask scout's prompt.
RESEARCH_CLASSIFY_CLAUSE = (
    "Return `research` (class, appetite, sources, depth, targets as `<source>=<ref>`, questions) "
    "only when the ask's deliverable is knowledge -- a question answered, a survey, a comparison, "
    "a diagnosis written up -- rather than a change to files; otherwise return it null. A pure "
    "research ask returns `writes` as []: the research route's findings artifact is not a write. "
    "An ask that also asks for a build returns both `research` and its `writes`. "
    "Class: `scouts` for a bare question a few searches answer; `corpus` for a comprehension "
    "ask -- understand a repo or topic, survey, compare or rank candidates -- at any target count; "
    "`deep` only when the EM is stuck delivering something and needs the unblock team; `spike` "
    "for a binary verdict on one unproven mechanism. More thoroughness is `depth` or "
    "`appetite: large`, never `deep`. "
    "URLs the ask names to study go in `seeds` as `<kind>=<url>`, kind video, doc, repo or paper, "
    "and never in `targets`."
)
#: `ask_gate` halt route for a research sizing: the hand-back to the sizing door.
RESEARCH_HANDBACK_ROUTE = "research"
#: `ask_gate` refusal for a research sizing whose request carries `writes`.
RESEARCH_MIXED_REFUSAL = (
    "Research ask also names file writes. Submit the research ask alone to /coordinator:research. "
    "Once research.close (with `sizing`) or `workflow.revise build_only=true` drops the sizing's "
    "research block, the build ask fires."
)
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


def research_assemble_flags_js(scouted: str, quote: str) -> str:
    """JS expression text appending the `--research-*` sizing-assemble flags from
    ``<scouted>.research``; the empty string when that is not a non-null object.
    Starts with `` + `` so it follows the command literal directly."""
    r = f"{scouted}.research"
    each = lambda field, flag: (  # noqa: E731
        f"({r}.{field} || []).map(function (v) {{ return ' {flag} ' + {quote}(v); }}).join('')"
    )
    return (
        f" + ({r} !== null && typeof {r} === 'object' ? ''"
        f" + ' --research-class ' + {quote}({r}.class)"
        f" + ({r}.appetite ? ' --research-appetite ' + {quote}({r}.appetite) : '')"
        f" + {each('sources', '--research-source')}"
        f" + ({r}.depth ? ' --research-depth ' + {quote}({r}.depth) : '')"
        f" + {each('targets', '--research-target')}"
        f" + {each('seeds', '--research-seed')}"
        f" + {each('questions', '--research-question')}"
        f" : '')"
    )


def acceptance_rows_assemble_flags_js(scouted: str, quote: str) -> str:
    """JS expression text appending ``--acceptance-rows-json`` from ``<scouted>.acceptance_rows``;
    the empty string unless that is a non-empty array. Starts with `` + `` so it follows the
    command literal directly."""
    a = f"{scouted}.acceptance_rows"
    return (
        f" + (Array.isArray({a}) && {a}.length > 0"
        f" ? ' --acceptance-rows-json ' + {quote}(JSON.stringify({a})) : '')"
    )


def design_route_assemble_flags_js(scouted: str, quote: str) -> str:
    """JS expression text appending ``--design-route <v>`` from ``<scouted>.design_route``;
    the empty string unless that is one of ``design_route.ROUTES``. Starts with `` + `` so it
    follows the command literal directly."""
    d = f"{scouted}.design_route"
    routes = ", ".join(f"'{r}'" for r in DESIGN_ROUTES)
    return f" + ([{routes}].includes({d}) ? ' --design-route ' + {quote}({d}) : '')"


#: Scout prompt passage: when to return `design_route`. Appended to the ask scout's prompt.
DESIGN_ROUTE_CLAUSE = (
    "Return `design_route` (canvas, reviewer or none) per the DoE page "
    "coordinator/docs/wiki/coordinator-tripwires/a-large-design-ask-goes-to-the-canvas.md; "
    "omit it when the ask is not a design ask."
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
    "revisions.jsonl",
    "run.json",
    "requirements.md",
    "manifest.json",
    ASK_TEXT_FILE,
    PM_INTENT_FILE,
    RELAYED_ASK_FILE,
    ROUTE_PREFERENCE_FILE,
)
#: `dispatch.land_run` params key whose dict value (a non-empty str `halted`) selects the
#: halted-run record-only mode.
HALTED_RECORD_PARAM = "halted_by"
#: `dispatch.land_run` params key for the roadmap fan-out record:
#: `{plans: [repo-relative plan path, ...]}`.
FANOUT_RECORD_PARAM = "fanout"
#: `dispatch.emit` result key naming where an ask's script went when the halted run's path was not taken.
OUTPUT_PATH_REDIRECT_KEY = "output_path_redirect"
#: Field names of that entry: the halted script, the fresh script, the halted run id, its recorded
#: emitter session, and one `OUTPUT_PATH_REDIRECT_REASONS` member.
OUTPUT_PATH_REDIRECT_FIELDS = ("from", "to", "halted_run", "prior_session", "reason")
#: `emitter-gone`: the halted run's recorded emitter session is no longer live (ended, or handed off and ended).
OUTPUT_PATH_REDIRECT_REASONS = ("emitter-gone",)
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
    plan = halted.get(HALT_PLAN_FIELD)
    if isinstance(plan, str) and _relative_inside(plan) and plan == "".join(plan.split()):
        lines.append(f"{HALT_PLAN_TRAILER}: {plan}")
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


#: halted.json key a ledger-check halt records its blocking register row ids under.
HALT_BLOCKING_FIELD = "blocking"
#: `resume_ledger` verdict key carrying the plan's em-discharged rows as `{row_id: text}`.
RESUME_LEDGER_EM_DISCHARGED_FIELD = "em_discharged"
#: `resume_ledger` verdict key carrying the halt commit's full 40-hex sha.
RESUME_LEDGER_HALT_SHA_FIELD = "halt_sha"
#: `resume_ledger` verdict key carrying `{row_id: {remaining: [str], evidence: str}}` for blocking rows.
RESUME_LEDGER_REMAINING_FIELD = "remaining"
#: `resume_ledger` verdict key carrying the revise note texts, oldest first.
RESUME_LEDGER_NOTES_FIELD = "revise_notes"
#: `resume_ledger` verdict key carrying the blocking ids whose register anchor is a `revision@` anchor.
RESUME_LEDGER_REVISED_FIELD = "revised"
#: `resume_ledger` verdict key carrying the ids an earlier halt judged met that a later no-landing halt re-listed.
RESUME_LEDGER_CARRIED_MET_FIELD = "carried_met"
#: `resume_ledger` verdict key carrying the open plan rows the halted run staged that no checkpoint
#: or halt commit landed; `[]` when every staged row landed.
RESUME_LEDGER_UNLANDED_FIELD = "unlanded"
#: `resume_ledger` verdict key carrying the `ledger_remaining_paths.remaining_leg_plan` result.
RESUME_LEDGER_LEG_FIELD = "remaining_leg"
#: `remaining_leg.action` values: run the leg, skip it (re-ruling-only revise), or refuse before dispatch.
LEG_DISPATCH, LEG_SKIP, LEG_REFUSE = "dispatch", "skip", "refuse"
#: Prefix of the compose halt reason when the ledger-remaining leg is refused.
LEG_REFUSAL_HEAD = "ledger-remaining leg refused: "
#: halted.json key a ledger-check halt records `[{id, remaining, evidence}]` for its blocking rows under.
HALT_BLOCKING_ITEMS_FIELD = "blocking_items"
#: `dispatch.land_run` param carrying `{run_id, halt_sha, blocking}` on a ledger resume.
TERMINAL_RESUME_LEDGER_PARAM = "resume_ledger"
#: Ledger failure reason for a blocking row judged met when no declared product path changed since the halt.
UNCHANGED_SINCE_HALT_REASON = "no declared product path changed since the halt commit"
_HALT_SUBJECT_RE = re.compile(r"^warp: record halted (\S+) run artifacts(?: \(process files\))?\s*$")


def halted_run_id(subject: str) -> str | None:
    """The run id a `halted_record_message` subject (with or without the ' (process files)'
    suffix) names; None for any other subject or an unsafe id. Pure."""
    match = _HALT_SUBJECT_RE.match(subject) if isinstance(subject, str) else None
    if match is None or not safe_id(match.group(1)):
        return None
    return match.group(1)


@dataclass(frozen=True)
class HaltRecord:
    """A ledger-check halt's committed record: the plan it named (None when halted.json predates
    the `plan` key), its sizing, every row id it staged (rows, owed, gated), and the register row
    ids that blocked it (None when unrecorded)."""

    run_id: str
    plan_rel: str | None
    sizing_rel: str
    staged: frozenset[str]
    blocking: tuple[str, ...] | None
    #: `{row_id: {remaining: [str], evidence: str}}`; empty when halted.json predates the key.
    remaining: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: Register row ids the run staged as rulable (manifest `rulable_ids`); empty when the manifest predates the key.
    rulable: frozenset[str] = frozenset()

    @property
    def met(self) -> frozenset[str]:
        """Rulable ids the halt did not list as blocking; empty when `blocking` was unrecorded."""
        if self.blocking is None:
            return frozenset()
        return self.rulable - frozenset(self.blocking)


#: Mirrors `halted_record_commit._blocking_items`, which writes this shape; change both together.
def _read_remaining(halted: dict) -> dict[str, dict[str, Any]]:
    items = halted.get(HALT_BLOCKING_ITEMS_FIELD)
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        rem = item.get("remaining")
        evidence = item.get("evidence")
        out[item["id"]] = {
            "remaining": [r for r in rem if isinstance(r, str)] if isinstance(rem, list) else [],
            "evidence": evidence if isinstance(evidence, str) else "",
        }
    return out


def _read_json_object(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _json_object_from_bytes(data: bytes) -> dict | None:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_json_at_commit(repo_root: Path, sha: str, rel: str) -> dict | None:
    """The JSON object at ``rel`` in commit ``sha``'s tree, read in-process; None when the blob
    is absent, unreadable or not a JSON object."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.git.rollback_check import ABSENT, _blob_at_commit

    try:
        common_dir = resolve_git_common_dir(repo_root)
        blob = _blob_at_commit(common_dir, sha, rel)
        if blob is None or blob is ABSENT:
            return None
        obj = read_object(common_dir, blob)
    except (OSError, ValueError):
        return None
    if obj is None or obj[0] != "blob":
        return None
    return _json_object_from_bytes(obj[1])


def read_halt_record(repo_root: Path, run_id: str, at_sha: str | None = None) -> HaltRecord | None:
    """Read `halted.json` and `manifest.json` from the run's record dir. None on an unsafe run id,
    an unreadable or malformed file, or a halt that is not `ledger-check`. Two file reads.

    A refire that rebinds the halted run id rewrites `halted.json` on its own halt, so the
    ledger-check record survives only in its halt commit. When the worktree record is not a
    ledger-check halt and ``at_sha`` is a 40-hex sha, both files are read from that commit's tree
    instead: at most two extra blob reads, no spawn. Scratch records are never committed."""
    try:
        base = record_dir(repo_root, run_id)
    except ValueError:
        return None
    halted = _read_json_object(base / "halted.json")
    manifest = _read_json_object(base / "manifest.json")
    if (halted is None or manifest is None or halted.get("halted") != "ledger-check") and (
        isinstance(at_sha, str) and _FULL_SHA_RE.fullmatch(at_sha)
    ):
        rel = f"{RECORD_DIR_ROOT}/{run_id}"
        halted = _read_json_at_commit(Path(repo_root), at_sha, f"{rel}/halted.json")
        manifest = _read_json_at_commit(Path(repo_root), at_sha, f"{rel}/manifest.json")
    if halted is None or manifest is None or halted.get("halted") != "ledger-check":
        return None
    sizing_rel = halted.get("sizing_rel")
    if not isinstance(sizing_rel, str) or not sizing_rel:
        return None
    staged: set[str] = set()
    for key in ("rows", "owed", "gated"):
        items = manifest.get(key, [])
        if not isinstance(items, list):
            return None
        staged.update(i["id"] for i in items if isinstance(i, dict) and isinstance(i.get("id"), str))
    plan = halted.get(HALT_PLAN_FIELD)
    blocking = halted.get(HALT_BLOCKING_FIELD)
    rulable = manifest.get("rulable_ids")
    return HaltRecord(
        run_id=run_id,
        plan_rel=plan if isinstance(plan, str) and plan else None,
        sizing_rel=sizing_rel,
        staged=frozenset(staged),
        blocking=tuple(blocking) if isinstance(blocking, list) and all(isinstance(b, str) for b in blocking) else None,
        remaining=_read_remaining(halted),
        rulable=frozenset(r for r in rulable if isinstance(r, str)) if isinstance(rulable, list) else frozenset(),
    )


def carry_forward(
    newest: tuple[HaltRecord, bool], earlier: Sequence[tuple[HaltRecord, bool]]
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]], tuple[str, ...]]:
    """`(blocking, remaining, carried_met)` for a resume from ``newest``. Each argument is a
    `(record, lands_chunks)` pair; ``earlier`` is newest first. An id an earlier halt judged met
    and every newer halt re-listed is carried met, unless a halt between landed a chunk (its
    blocking was judged on changed work). Remaining per still-blocking id is newest first, merged
    across the walked records, deduplicated. Pure."""
    head, head_lands = newest
    pool = set(head.blocking or ())
    walked = [head]
    carried: set[str] = set()
    if not head_lands:
        for rec, lands in earlier:
            walked.append(rec)
            carried |= pool & rec.met
            if lands:
                break
            if rec.blocking is not None:
                pool &= set(rec.blocking)
    blocking = tuple(i for i in (head.blocking or ()) if i not in carried)
    remaining: dict[str, dict[str, Any]] = {}
    for item in blocking:
        steps: list[str] = []
        evidence = ""
        for rec in walked:
            entry = rec.remaining.get(item)
            if entry is None:
                continue
            steps.extend(s for s in entry.get("remaining", ()) if s not in steps)
            evidence = evidence or entry.get("evidence", "")
        if steps or evidence:
            remaining[item] = {"remaining": steps, "evidence": evidence}
    return blocking, remaining, tuple(i for i in (head.blocking or ()) if i in carried)


#: The run's delivery note (`<dir>/<run_id>.md`): what was asked, what shipped, review findings,
#: judge verdict. The first of an ask run's declared doc writes.
DELIVERY_NOTE_DIR = "docs/delivery"


LEDGER_HALT_FILE = "ledger-halt.json"
HALT_PLAN_FIELD = "plan"
#: Commit trailer naming the plan a halt commit landed chunks for; `resume_landed_rows` reads it.
HALT_PLAN_TRAILER = "Halt-Plan"
RESUME_LEDGER_KEY = "resume_ledger"
#: `LedgerHalt.halted` of a ledger-check pointer (the default); a commit-halt pointer says "commit".
LEDGER_HALT_KIND = "ledger-check"
#: `LedgerHalt.halted` of a resume synthesised at gate time from an all-landed plan; never a pointer on disk.
ALL_LANDED_HALT_KIND = "all-landed"
#: `resume_ledger` verdict key carrying the halted kind; absent means `LEDGER_HALT_KIND`.
RESUME_LEDGER_KIND_FIELD = "kind"
#: The run's recorded review outcome, in its record dir; the source of a re-fire's `inline_review`.
RUN_OUTCOME_FILE = "run-outcome.json"
RUN_OUTCOME_KEYS = ("inline_review", "incomplete_chunks", "landed_chunks", "ledger_verdict", "plan_path")
#: `inline_review` key naming the halted run whose review a resumed landing reuses.
RESUMED_RUN_ID_KEY = "resumed_run_id"


#: Anchor prefix of a register row minted from plan row `<id>`: `plan-row:<id>`.
PLAN_ROW_ANCHOR_PREFIX = "plan-row:"
#: Ledger failure reason when the register holds no row to judge.
EMPTY_REGISTER_REASON = "the register holds no requirement row: nothing was judged"
#: `empty_register_refusal(path)` is `EMPTY_REGISTER_REASON + head + path + tail`; the composed ask
#: script splices its runtime sizing path between the two.
EMPTY_REGISTER_REFUSAL_HEAD = " -- the requirement_register of "
EMPTY_REGISTER_REFUSAL_TAIL = " has no row; record the requirement with workflow.revise verbatim, then re-fire"
_EMPTY_REGISTER_UNNAMED_SIZING = "this run's sizing"


def empty_register_refusal(sizing_rel: Optional[str]) -> str:
    """The refusal text for a run whose register has no row: `EMPTY_REGISTER_REASON`, then the
    register named by ``sizing_rel`` (the run's sizing when None) and the remedy. Pure."""
    return (
        EMPTY_REGISTER_REASON
        + EMPTY_REGISTER_REFUSAL_HEAD
        + (sizing_rel or _EMPTY_REGISTER_UNNAMED_SIZING)
        + EMPTY_REGISTER_REFUSAL_TAIL
    )


def rulable_ids(rows: Any) -> tuple[str, ...]:
    """Ids of the register rows whose status is neither met nor waived and that no recorded
    out-of-scope ruling withholds, in register order; a row that is not a mapping is skipped. Pure."""
    # Deferred: requirements_ledger imports this module at load.
    from coordinator_core.ops.dispatch_emit.requirements_ledger import ruled_out_ids

    mappings = [r for r in rows if isinstance(r, dict)]
    out = set(ruled_out_ids(mappings))
    return tuple(
        str(r.get(rr.ROW_ID))
        for r in mappings
        if r.get(rr.ROW_STATUS) not in (rr.STATUS_MET, rr.STATUS_WAIVED) and str(r.get(rr.ROW_ID)) not in out
    )


@dataclass(frozen=True)
class LedgerHalt:
    """The pointer a ledger halt leaves beside its plan's dispatch reports: the run that halted,
    the plan and sizing it ran, and the register row ids that blocked it.

    ``halted == ALL_LANDED_HALT_KIND`` is synthesised at gate time for a plan whose rows are all
    landed and never persisted: ``to_json`` raises ValueError and ``from_json`` refuses the kind.
    Its run_id is the newest run that landed the plan, or "none" when unknown; halt_sha is None;
    blocking is the rulable register ids not met; remaining is the newest halt record's remaining
    for those ids, else {}."""

    run_id: str
    plan_rel: str
    sizing_rel: str
    blocking: tuple[str, ...]
    #: The halt commit's full sha; filled from git at resume, never persisted in the pointer.
    halt_sha: str | None = None
    remaining: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    halted: str = LEDGER_HALT_KIND
    outcome: str | None = None
    #: Derived from the halt chain at resume; never persisted in the pointer.
    carried_met: tuple[str, ...] = ()
    #: Open plan rows that no checkpoint or halt commit of this plan landed; derived at resume, never persisted.
    unlanded: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        if self.halted == ALL_LANDED_HALT_KIND:
            raise ValueError("an all-landed resume is derived at gate time and never persisted")
        out: dict[str, Any] = {
            "run_id": self.run_id,
            HALT_PLAN_FIELD: self.plan_rel,
            "sizing": self.sizing_rel,
            "blocking": list(self.blocking),
        }
        if self.halted != LEDGER_HALT_KIND:
            out["halted"] = self.halted
        if self.outcome is not None:
            out["outcome"] = self.outcome
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "LedgerHalt":
        blocking = data["blocking"]
        if not isinstance(blocking, list) or not all(isinstance(b, str) for b in blocking):
            raise ValueError("blocking is not a list of str")
        halted = data.get("halted", LEDGER_HALT_KIND)
        outcome = data.get("outcome")
        if not isinstance(halted, str) or not halted:
            raise ValueError("halted is not a non-empty str")
        if halted == ALL_LANDED_HALT_KIND:
            raise ValueError("an all-landed resume is never a pointer on disk")
        if outcome is not None and not isinstance(outcome, str):
            raise ValueError("outcome is not a str")
        return cls(
            run_id=data["run_id"],
            plan_rel=data[HALT_PLAN_FIELD],
            sizing_rel=data["sizing"],
            blocking=tuple(blocking),
            halted=halted,
            outcome=outcome,
        )


def ledger_halt_path(repo_root: Path, plan_rel: str) -> Path:
    """`<repo_root>/<dispatch report dir>/<plan stem>/ledger-halt.json`."""
    from coordinator_core.ops.dispatch_emit.emit import _DISPATCH_REPORT_DIR

    return Path(repo_root) / _DISPATCH_REPORT_DIR / Path(plan_rel).stem / LEDGER_HALT_FILE


def _relative_inside(value: str) -> bool:
    p = PurePosixPath(value.replace("\\", "/"))
    return bool(value) and not p.is_absolute() and ".." not in p.parts and not PureWindowsPath(value).is_absolute()


def read_ledger_halt(path: Path) -> LedgerHalt | None:
    """The halt pointer at ``path``, or None on an unreadable file, bad JSON, a missing or
    mistyped field, an unsafe run_id, or an absolute or escaping path. One read; pure."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        halt = LedgerHalt.from_json(data)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if not (isinstance(halt.run_id, str) and safe_id(halt.run_id)):
        return None
    for rel in (halt.plan_rel, halt.sizing_rel):
        if not isinstance(rel, str) or not _relative_inside(rel):
            return None
    if halt.outcome is not None and not _relative_inside(halt.outcome):
        return None
    return halt


def write_run_outcome(repo_root: Path, run_id: str, outcome: dict[str, Any]) -> Path:
    """Atomically write the admitted keys of ``outcome`` to `<record_dir>/run-outcome.json` and
    return the path. Raises ValueError on an unsafe run id or an `inline_review` that is not a dict."""
    if not isinstance(outcome.get("inline_review"), dict):
        raise ValueError("run outcome needs an inline_review dict")
    path = record_dir(repo_root, run_id) / RUN_OUTCOME_FILE
    kept = {k: outcome[k] for k in RUN_OUTCOME_KEYS if k in outcome}
    atomic_write_bytes(path, (json.dumps(kept, indent=2) + "\n").encode("utf-8"))
    return path


def read_run_outcome(repo_root: Path, run_id: str) -> dict[str, Any] | None:
    """The run's recorded outcome (admitted keys only), or None on an unsafe run id, an unreadable
    or malformed file, or a missing `inline_review` dict. One read."""
    try:
        data = _read_json_object(record_dir(repo_root, run_id) / RUN_OUTCOME_FILE)
    except ValueError:
        return None
    if data is None or not isinstance(data.get("inline_review"), dict):
        return None
    return {k: data[k] for k in RUN_OUTCOME_KEYS if k in data}


#: Names the baton an M+ run minted. Its own file, not a `run.json` key: `requirements_ledger.start`
#: rewrites `run.json` with two keys, dropping any other, and its presence marks a run ledger-bearing.
RUN_BATON_FILE = "run-baton.json"


def _safe_baton_rel(baton_rel: Any) -> str | None:
    """``baton_rel`` as a forward-slash repo-relative path, or None when empty, absolute or climbing."""
    if not isinstance(baton_rel, str):
        return None
    posix = baton_rel.replace("\\", "/")
    if not posix.strip() or PurePosixPath(posix).is_absolute() or PureWindowsPath(posix).drive:
        return None
    if ".." in posix.split("/"):
        return None
    return posix


def write_run_baton(repo_root: Path, run_id: str, baton_rel: str) -> Path:
    """Atomically write the minted baton's repo-relative path to `<record_dir>/run-baton.json` and
    return the path. Raises ValueError on an unsafe run id or an empty, absolute or `..` baton path."""
    rel = _safe_baton_rel(baton_rel)
    if rel is None:
        raise ValueError(f"baton path {baton_rel!r} is not a repo-relative path")
    path = record_dir(repo_root, run_id) / RUN_BATON_FILE
    atomic_write_bytes(path, (json.dumps({"baton": rel}) + "\n").encode("utf-8"))
    return path


def read_run_baton(repo_root: Path, run_id: str) -> str | None:
    """The run's recorded baton path (forward-slash, repo-relative), or None on an unsafe run id, an
    unreadable or malformed file, or a non-str or unsafe `baton`. One read."""
    try:
        data = _read_json_object(record_dir(repo_root, run_id) / RUN_BATON_FILE)
    except ValueError:
        return None
    return None if data is None else _safe_baton_rel(data.get("baton"))


def write_ledger_halt(path: Path, halt: LedgerHalt) -> None:
    """Atomically write ``halt`` to ``path``."""
    atomic_write_bytes(path, (json.dumps(halt.to_json(), indent=2) + "\n").encode("utf-8"))


class ResumePlan(NamedTuple):
    """`sizing_fire.resumable_plan`'s return: the plan to resume and the ids of its rows still
    to run, in spine order; ``ledger`` is the halt pointer a ledger-halted run resumes from;
    ``landed_open`` is ``(row id, landing sha)`` for each row still ``open`` on disk that a
    commit landed."""

    plan_rel: str
    incomplete: tuple[str, ...]
    ledger: LedgerHalt | None = None
    landed_open: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class GateVerdict:
    """dispatch.ask_gate reply: an arm to run, or a halt dict
    {"kind": HALT_*, "reason": str, "touchpoint"?: str, "route"?: str,
    "fire"?: list[str]} (a research halt's one ready Workflow call); the verdict's
    "design_route" key carries the sizing's payload and never halts; baton is the
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
    # Set when a ledger-halted run's row was satisfied out of band: the halt pointer's JSON.
    resume_ledger: dict | None = None
    # The design-registry brief the plan author owes (design_conformance.brief_text); emitted
    # under DESIGN_BRIEF_KEY only when non-empty.
    design_brief: str = ""
    # The sizing's design_route payload (design_route.read); emitted under DESIGN_ROUTE_KEY
    # only when not None.
    design_route: dict | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"arm": self.arm, "halt": self.halt, "baton": self.baton}
        if self.design_brief:
            out["design_brief"] = self.design_brief
        if self.design_route is not None:
            out[DESIGN_ROUTE_KEY] = self.design_route
        if self.resume_ledger is not None:
            out[RESUME_LEDGER_KEY] = self.resume_ledger
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
    #: Rows sharing a non-empty `group` share one agent; each keeps its own `_runRow` entry.
    group: str = ""
    #: Work-class route (`agent_route.Route.to_json()`); `None` for a row the routing table did not touch.
    route: Optional[dict[str, Any]] = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "agent_type": self.agent_type,
            "model": self.model,
            "brief_path": self.brief_path,
            "writes": list(self.writes),
            "wave": self.wave,
            "deps": list(self.deps),
        }
        if self.group:
            out["group"] = self.group
        if self.route is not None:
            out["route"] = self.route
        return out

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
            group=data.get("group") or "",
            route=data.get("route"),
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
    """The manifest.json payload in the run dir, which the dispatch.ask_stage reply's manifest_handle points at.
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
    #: Register row ids still unruled (neither met nor waived) at stage time.
    rulable_ids: tuple[str, ...] = ()
    #: Register row ids the ledger judges, and those a recorded out-of-scope ruling withholds
    #: (`requirements_ledger.ledger_ids`); an absent key reads as ``()``.
    ledger_item_ids: tuple[str, ...] = ()
    withheld_ids: tuple[str, ...] = ()
    #: Sorted gate resume rows the re-planned spine no longer carries: reported, never staged;
    #: an absent key reads as ``()``.
    retired_resume_rows: tuple[str, ...] = ()
    #: The staged rows' `fanout_cost.FanoutCost.meta_dict()` (agents, basis, groups); None when unset.
    fanout: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        out = self._base_json()
        if self.fanout is not None:
            out["fanout"] = dict(self.fanout)
        return out

    def _base_json(self) -> dict[str, Any]:
        return {
            "rulable_ids": list(self.rulable_ids),
            "ledger_item_ids": list(self.ledger_item_ids),
            "withheld_ids": list(self.withheld_ids),
            "retired_resume_rows": list(self.retired_resume_rows),
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
            rulable_ids=tuple(data.get("rulable_ids") or ()),
            ledger_item_ids=tuple(data.get("ledger_item_ids") or ()),
            withheld_ids=tuple(data.get("withheld_ids") or ()),
            retired_resume_rows=tuple(data.get("retired_resume_rows") or ()),
            fanout=dict(data["fanout"]) if data.get("fanout") else None,
        )
