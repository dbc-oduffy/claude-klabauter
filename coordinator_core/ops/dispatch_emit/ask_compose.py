"""warp --ask script composer: the one .mjs from an ask (or an existing sizing) to a reviewed run.

The script sizes (raw ask only), asks the engine for a gate verdict, halts or runs the routed arm
(XS stage; S plan-author then stage; M+ plan-blitz then stage), executes the staged manifest rows
and composes the review wave once. Every early exit assigns the typed `_halted` and later phases
run under `if (!_halted)`: the terminal return is the script's one exit. Rows, briefs and pathspecs are Python-composed by
dispatch.ask_stage; the script only interprets the manifest. Pure composition: no spawn.
"""

from __future__ import annotations

import json
import shlex
from datetime import date
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.git.git_state import head_sha
from coordinator_core.ops.dispatch_emit import emit as _emit
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR, EmBrief, brief_decl_js
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    ASK_PHASES,
    PM_INTENT_FILE,
    HALT_REFUSAL,
    HALT_ROOM,
    HALT_TOUCHPOINT,
    NO_PLAN_HAND_OFF_FLAG,
    OP_ASK_GATE,
    OP_ASK_STAGE,
    PM_EXIT_QUESTION_KEY,
    RUN_DIR_ROOT,
    SIZING_SCOUT_AGENT_TYPE,
    STOP_AFTER_PLAN,
)
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import STAGE_FN as _PLAN_BLITZ_FN
from coordinator_core.ops.dispatch_emit.requirements_ledger import (
    LEDGER_VIEW_FILE,
    STATUSES as LEDGER_STATUSES,
)
from coordinator_core.ops.dispatch_emit.pm_adjudication import (
    ADJUDICATOR_AGENT_TYPE,
    IRREVERSIBLE_GATE_SOURCE,
)
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ARM_M_PLUS,
    ARM_ROADMAP,
    ARM_S,
    ARM_XS,
    SizingFireRefused,
    load_sizing,
    resolve_arm,
)
from coordinator_core.ops.dispatch_emit.wake_digest import (
    CAP_HELPER_JS,
    TERMINAL_COMMIT_CLI_HELPER_JS,
    TERMINAL_COMMIT_CLI_PROPERTY_JS,
    next_action_parts,
)
from coordinator_core.ops.dispatch_emit.work_label import build_work_label
from coordinator_core.ops.review_mint.execute_review import (
    CRITERION_JUDGE_PHASE_TITLE,
    NO_SLICES_HALT,
    compose_criterion_judge,
    compose_execute_review,
)
from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN, parse_execute_review
from coordinator_core.ops.review_mint.wave_bookkeeping import review_wave_bookkeeping_stem
from coordinator_core.ops.workflow_scaffold import _js_string_literal

_BLITZ_TRAIL = "blitz"
_INVOKE = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'
_DOC_NEW = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-doc-new"'
_SIZING_ASSEMBLE = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/sizing-assemble"'
_ANCHOR_CLAUSE = (
    "Repo root: {root} -- run `cd {root}` as its own standalone Bash call first; every "
    "repo-relative path below resolves against it."
)
_REVIEW_RESULT_NAMES = ("_reviewPrep", "_reviewWave", "_deliveryVerdict", "_reviewIntegration")


class AskComposeRefused(ValueError):
    """The ask cannot be composed into a script; the message names the cause."""


def _lit(text: str) -> str:
    return _js_string_literal(text)


def _cat(*parts: str) -> str:
    """JS string-concatenation expression; a part is a quoted literal or a raw `js:` expression."""
    return " + ".join(p[3:] if p.startswith("js:") else _emit._resolve_markers_plus(p) for p in parts)


def _agent(
    prompt_expr: str,
    *,
    label: str,
    phase: str,
    agent_type: str,
    schema: dict,
    agent_model: Optional[str] = None,
    effort: Optional[str] = None,
) -> str:
    effort_opt = f"effort: {_lit(effort)}, " if effort else ""
    return (
        f"agent({prompt_expr}, {{ label: {_lit(label)}, phase: {_lit(phase)}, "
        f"agentType: {_lit(agent_type)}, {_emit._model_opt(agent_type, agent_model)}, {effort_opt}"
        f"schema: {json.dumps(schema, sort_keys=True)} }})"
    )


#: Model and effort per stage. Sonnet executes and runs mechanical engine calls at low effort;
#: Opus only where judgment carries the run: the S plan, and the ceo XL exit. Each Opus stage
#: uses an agent type whose definition pins `model: opus`, so `block_ungranted_opus_subagent`
#: admits it and `enforce_agent_model_pin` sees an equal model and a cheaper-or-equal effort.
_MECHANICAL_EFFORT = "low"
_PLAN_AUTHOR_AGENT_TYPE = "coordinator:plan-author"
_PLAN_AUTHOR_MODEL, _PLAN_AUTHOR_EFFORT = "opus", "low"
_XL_EXIT_MODEL, _XL_EXIT_EFFORT = "opus", "low"
_COMMIT_AGENT_TYPE = "coordinator:git-commit-agent"
#: The git-commit-agent definition pins haiku; one fixed command needs nothing more.
_COMMIT_MODEL = "haiku"
_DOCS_MODEL = "sonnet"
#: The run's delivery note: what was asked, what shipped, review findings, judge verdict.
DELIVERY_NOTE_DIR = "docs/delivery"
#: Every APM ruling in-run: a repo memo never outranks a live PM verbatim (an executor once
#: followed a memo over the ask on the strength of an APM ruling).
_KEEP_THE_ASK = (
    " The PM's verbatim words (the sizing's pm_verbatims, and {run_dir}/ask.md when it exists) "
    "outrank any in-repo memo, plan or doctrine: never rule against them on a memo's strength. "
    "Where they conflict, keep the ask and name the conflict in your ruling."
)


def _obj(required: list[str], properties: dict) -> dict:
    return {"type": "object", "required": required, "properties": properties}


_STR = {"type": "string"}
_GATE_SCHEMA = _obj(
    ["arm", "halt"],
    {
        "arm": {"type": ["string", "null"]},
        "halt": {"type": ["object", "null"]},
        "baton": {"type": ["object", "null"]},
        "tshirt": _STR,
        "route": _STR,
        "resume_plan": {"type": ["string", "null"]},
    },
)
_SCOUT_SCHEMA = _obj(
    ["tshirt", "intent", "name", "premise_provenance", "premise_evidence", "exit_criterion", "writes"],
    {
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
            "items": _obj(["title", "owner_repo"], {"title": _STR, "owner_repo": _STR, "requires": _STR}),
        },
    },
)
_ASSEMBLE_SCHEMA = _obj(
    ["exit_code", "stdout"], {"exit_code": {"type": "integer"}, "stdout": _STR, "stderr": _STR}
)
_ACCEPT_SCHEMA = _obj(
    ["verdict", "pmOnly", "ruling"],
    {
        "verdict": {"type": "string", "enum": ["ruled", "pm-only"]},
        "pmOnly": {"type": "boolean"},
        "pmOnlyGround": _STR,
        "ruling": _STR,
        "statement": _STR,
    },
)
_ACCEPT_RESULT_SCHEMA = _obj(["ok"], {"ok": {"type": "boolean"}, "error": _STR})
_PLAN_SCHEMA = _obj(["plan_rel"], {"plan_rel": _STR})
_XL_SCHEMA = _obj(
    ["verdict", "pmOnly", "ruling"],
    {
        "verdict": {"type": "string", "enum": ["ruled", "pm-only"]},
        "pick": {"type": "string", "enum": ["roadmap", "accept_multi_session"]},
        "pmOnly": {"type": "boolean"},
        "pmOnlyGround": _STR,
        "ruling": _STR,
    },
)
_DOCS_SCHEMA = _obj(
    ["doc_paths", "doc_writes"],
    {
        "doc_paths": {"type": "array", "items": _STR},
        "doc_writes": {"type": "array", "items": _STR},
        "note_path": _STR,
    },
)

# Mirrors emit.py::_REPO_ROOT_DECLARATION: the token arrives as a fire arg, never as script bytes.
_RUN_TOKEN_DECLARATION = (
    "  const _RUN_TOKEN = (typeof args !== 'undefined' && args && typeof args.runToken === 'string' "
    "&& args.runToken) ? args.runToken : null;"
)
_COMMIT_SCHEMA = _obj(
    ["committed"],
    {
        "committed": {"type": "boolean"},
        "sha": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
        "refused": {"type": ["string", "null"]},
        "sizing_shipped": {"type": ["object", "null"]},
        "close": {"type": ["object", "null"]},
    },
)
_LEDGER_CHECK_SCHEMA = _obj(
    ["verdict", "items"],
    {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "all_of_pm_ask": {"type": "boolean"},
        "items": {
            "type": "array",
            "items": _obj(
                ["id", "status"],
                {
                    "id": _STR,
                    "status": {"type": "string", "enum": list(LEDGER_STATUSES)},
                    "essential": {"type": "boolean"},
                    "superseded_by": _STR,
                    "evidence": _STR,
                },
            ),
        },
        "conflicts": {"type": "array", "items": _STR},
    },
)
_LEDGER_CHECK_AGENT_TYPE = "coordinator:code-reviewer"
#: The stage-start read every judgment-bearing stage makes: the EM can revise the ask mid-run
#: (`workflow.revise`), so a stage that read the ask only at emit would act on a stale one.
_LEDGER_CLAUSE = (
    "Requirements ledger: read {ledger} at the start of this stage, before anything else. It holds "
    "every PM requirement for this run in arrival order: the PM's own words and each revision the "
    "EM sent while the run was in flight, and it can grow mid-run. Each item outranks every brief, "
    "plan and in-repo memo; a later revision governs over an earlier item it changes. Carry every "
    "item into what you hand on."
)
_MANIFEST_SCHEMA = _obj(
    ["run_dir", "rows", "review_declared_paths", "marker_path"],
    {
        "run_dir": _STR,
        "plan_id": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
        "rows": {
            "type": "array",
            "items": _obj(
                ["id", "agent_type", "model", "brief_path", "writes", "wave", "deps"],
                {
                    "id": _STR,
                    "agent_type": _STR,
                    "model": _STR,
                    "brief_path": _STR,
                    "writes": {"type": "array", "items": _STR},
                    "wave": {"type": "integer"},
                    "deps": {"type": "array", "items": _STR},
                },
            ),
        },
        "review_declared_paths": {"type": "array", "items": _STR},
        "marker_path": _STR,
        "gated": {"type": "array", "items": _obj(["id"], {"id": _STR, "reason": _STR, "gate": _STR, "owner_repo": _STR, "closure_key": {}})},
    },
)


_LEDGER_PREFIX_VAR = "_LEDGER_PREFIX"


def _ledger_shadowed(blitz_fn: str, *, em_brief: bool) -> str:
    """`planBlitz` defined inside a scope whose `agent` prefixes the ledger clause, so every
    plan-blitz prompt reads the requirements ledger first. An EM-brief shadow inside the function
    calls `_askAgent`, rebound here to the ledger-prefixed agent: the brief and the ledger compose."""
    rebind = "    const _askAgent = (p, o) => agent(p, o);\n" if em_brief else ""
    return (
        "  const _outerAgent = agent;\n"
        f"  const {_PLAN_BLITZ_FN} = (() => {{\n"
        f"    const agent = (p, o) => _outerAgent({_LEDGER_PREFIX_VAR} + p, o);\n"
        f"{rebind}{blitz_fn}\n"
        f"    return {_PLAN_BLITZ_FN};\n"
        "  })();"
    )


_SCOPE_SLOT = "SCOPE_SLOT_X"
_ROW_VERIFY_PROMPT_OPEN = "`Run and report on the scoped test target(s) for ${id}: `"


def _scoped_test_call(
    agent_type_host: Optional[str], review_edits_base: str = "HEAD", prompt_head: Optional[str] = None
) -> str:
    """The plan route's terminal test agent call, scoped at run time to the manifest's declared paths.

    The plan is authored inside the run, so the scope cannot resolve at compose time; the
    stage's `review_declared_paths` stand in. Trap: no falsifier leg -- the plan's falsifier
    is unknown until the plan exists.
    """
    call = _emit._test_agent_call_expr(
        [_SCOPE_SLOT], agent_type_host=agent_type_host, review_edits_base=review_edits_base, prompt_head=prompt_head
    )
    return call.replace(f"[{_SCOPE_SLOT}]", "[' + _manifest.review_declared_paths.join(', ') + ']")


_USAGE_LIMIT_RE = r"/(usage|session|rate|weekly|5-hour) limit|limit reached|resets \d{1,2}(:\d{2})?\s*(am|pm)?|quota/i"
HALT_USAGE_LIMIT = "usage_limit"
HALT_DEPENDENTS_UNSTARTED = "dependents_unstarted"


def _usage_limit_helper_js() -> str:
    """`_haltOnUsageLimit(e)`: a limit-shaped agent failure becomes a resumable `_halted`; any other error rethrows.

    The run id the harness resumes from is not visible to the script, so the halt carries a hint to
    read it from the Workflow result.
    """
    return (
        f"  const _USAGE_LIMIT_RE = {_USAGE_LIMIT_RE};\n"
        "  function _haltOnUsageLimit(e) {\n"
        "    const msg = String((e && (e.message ?? e)) ?? '');\n"
        "    if (!_USAGE_LIMIT_RE.test(msg)) throw e;\n"
        f"    _halted = {{ halted: {_lit(HALT_USAGE_LIMIT)}, run_id: _runId, detail: msg.slice(0, 300), "
        "resume_from_run_id: 'this Workflow run id (wf_...)', "
        "next_action: 'After the limit resets, call Workflow with this scriptPath and resumeFromRunId set to this run id.' };\n"
        "  }"
    )


_PLAN_SLOT = "PLAN_PATH_SLOT_X"
_RUNTIME_PLAN_PATH_JS = "(_planRel ?? _sizingRel)"
_JUDGE_PATH_CLAUSE = (
    "\n\nThe plan_path below is the plan, or on the XS route (no plan is authored) the sizing: "
    "then its exit_criterion is the spec and there is no Verification section."
)


def _criterion_judge_call(
    review, stage_schemas: dict, *, run_base_sha: str, head: str, agent_type_host: Optional[str]
) -> Optional[str]:
    """The roster judge's call EXPRESSION, naming the plan (or XS sizing) known only at run time.

    `compose_criterion_judge` takes `plan_path` as a compile-time literal; the slot stands in and
    is spliced out for the runtime expression. Trap: no falsifier leg -- the plan's falsifier is
    unknown until the plan exists, and the judge reads the plan itself.
    """
    call = compose_criterion_judge(
        review,
        stage_schemas=stage_schemas,
        plan_path=_PLAN_SLOT,
        run_base_sha=run_base_sha,
        falsifier=None,
        prompt_head=head + _JUDGE_PATH_CLAUSE,
        host_degraded=agent_type_host == _emit._AGENT_TYPE_HOST_DEGRADED,
    )
    if call is None:
        return None
    if call.count(_PLAN_SLOT) != 2:
        raise AskComposeRefused("criterion judge prompt no longer names its plan path exactly twice")
    return call.replace(_PLAN_SLOT, f"' + {_RUNTIME_PLAN_PATH_JS} + '")


def _known_arm(repo_root: str, sizing_rel: Optional[str]) -> Optional[str]:
    """The arm of an existing sizing, or None when unreadable (the gate verb owns that refusal)."""
    if not sizing_rel:
        return None
    try:
        return resolve_arm(load_sizing(Path(repo_root), sizing_rel))
    except (SizingFireRefused, OSError, ValueError, KeyError):
        return None


def _xl_exit_open(repo_root: str, sizing_rel: Optional[str]) -> bool:
    """An existing sizing whose XL exit the APM picks in-run: `pm-decision`, no `xl_exit`, ceo mode."""
    if not sizing_rel:
        return False
    from coordinator_core.ops.sizing_acceptance import apm_picks_xl_exit

    try:
        sizing = load_sizing(Path(repo_root), sizing_rel)
    except (SizingFireRefused, OSError, ValueError, KeyError):
        return False
    return (
        sizing.get("route") == "pm-decision"
        and sizing.get("xl_exit") is None
        and apm_picks_xl_exit(sizing.get("interaction_mode"))
    )


def _read_plan_blitz() -> str:
    from coordinator_core.ops.dispatch_emit.ask_plan_blitz import load_plan_blitz_text

    return load_plan_blitz_text()


def _phase_titles(
    *,
    with_size: bool,
    blitz_phases: list[str],
    review_titles: list[str],
    judged: bool = False,
    with_accept: bool = False,
    through_plan: bool = False,
    extra: Sequence[str] = (),
) -> list[str]:
    titles: list[str] = []
    for phase in ASK_PHASES:
        if phase == "size" and not with_size:
            continue
        if phase == "accept" and not with_accept:
            continue
        titles.append(phase)
        if phase == "plan":
            titles.extend(t for t in blitz_phases if t not in titles)
            if through_plan:
                return titles
    judge_titles = [CRITERION_JUDGE_PHASE_TITLE] if judged else []
    for title in [_emit._EXECUTE_PHASE_TITLE, *review_titles, *judge_titles, _emit._TEST_PHASE_TITLE, *extra]:
        if title not in titles:
            titles.append(title)
    return titles


_REVIEW_NOOP_EXIT = "return { halted: 'no-op',"
_REVIEW_NOOP_TAIL = "wave: null, integration: null }; }"
_REVIEW_SLICES_GUARD = "  if (!_reviewPrep || (!(_reviewPrep.slices ?? []).length && !_verifyOnly)) {"


def _single_exit_review(blocks: list[str]) -> str:
    """The review blocks with prep's no-op `return` turned into a `_halted` assignment.

    Everything after the no-op check, the rest of prep and every later block, runs under
    `if (!_halted)`, so the no-op reaches the terminal return like any other halt.
    """
    prep = blocks[0]
    if prep.count(_REVIEW_NOOP_EXIT) != 1 or _REVIEW_NOOP_TAIL not in prep or _REVIEW_SLICES_GUARD not in prep:
        raise AskComposeRefused("review prep block no longer has the no-op exit shape single-exit composition rewrites")
    prep = prep.replace(_REVIEW_NOOP_EXIT, "_halted = { halted: 'no-op',")
    prep = prep.replace(_REVIEW_SLICES_GUARD, "  if (!_halted && (!_reviewPrep || (!(_reviewPrep.slices ?? []).length && !_verifyOnly))) {", 1)
    prep = prep.replace(f"return {{ halted: '{NO_SLICES_HALT}'", f"_halted = {{ halted: '{NO_SLICES_HALT}'", 1)
    rest = "\n\n".join(blocks[1:])
    return prep + ("\n\n  if (!_halted) {\n" + rest + "\n  }" if rest else "")


def _row_runner_js() -> str:
    """Declarations `_emit._run_row_helper_js` reads, with no plan-scoped halting (one run, one plan)."""
    return "\n".join(
        [
            _emit._row_state_decls_js({}, ()),
            f"  const _ROW_VERIFY_SCHEMA = {_emit.stage_schema_literal('row_verification_result')};",
            _emit._run_row_helper_js(None),
        ]
    )


def compose_ask_script(
    *,
    repo_root: str,
    prompt: Optional[str],
    sizing_rel: Optional[str],
    run_id: str,
    session_id: Optional[str],
    review_roster_fragment: Optional[dict] = None,
    review_stage_schemas: Optional[dict] = None,
    wrap_stage: Optional[Callable[[str], tuple[str, list[str]]]] = None,
    plan_blitz_text: Optional[str] = None,
    script_path: Optional[str] = None,
    plan_blitz_args: Optional[dict] = None,
    writes: Sequence[str] = (),
    cross_repo_approved: bool = False,
    roadmap_blitz_text: Optional[str] = None,
    agent_type_host: Optional[str] = None,
    baton: Optional[dict] = None,
    accept_pending: bool = False,
    preamble: Optional[str] = None,
    em_brief: Optional[EmBrief] = None,
    stop_after: Optional[str] = None,
    in_run_commit: bool = False,
) -> str:
    """The .mjs text for one ask: a raw `prompt`, or an existing `sizing_rel` (size phase omitted).

    The review roster and stage schemas load from DoE when not injected; `wrap_stage` and
    `plan_blitz_text` default to `ask_plan_blitz.wrap_stage` over the resolved plugin asset, which
    is embedded only when the arm can be M+. `script_path` is the repo-relative path the script
    is written to, carried into `next_action.params` for `dispatch.terminal_commit`.
    `plan_blitz_args` is spread first into the planBlitz call, so `mode`, `repoRoot` and `batons`
    always win; `writes` seeds the emit-time write set the gate and stage ops receive.
    `baton` ({"path", "deliverable_id"}) is passed to the size scaffold and the gate verb. The
    accept phase (an APM ruling recorded in-run, then a re-gate) composes when `accept_pending`
    or on a raw ask; it never writes a `pm_quote`. `preamble` heads every row executor's prompt,
    as on the plan route; it is refused when the script carries an M+ or roadmap branch, whose
    executors this script never prompts. `em_brief` is declared once as `_EM_BRIEF` and prefixes
    every agent prompt the script composes, plan-blitz's included (through `wrap_stage`'s
    `agent_prefix_var`, which shadows `agent` over the script-level `_askAgent`); the roadmap
    arm refuses it. A gate verdict's `resume_plan` is staged at S and revises at M+ instead of
    authoring. A raw ask sizes through the scout agent and one composed
    `sizing-assemble --write` step; a gate halt carries the assemble decision's `pm_exit_question`.
    `stop_after=STOP_AFTER_PLAN` ends the script at the plan wave with the single exit
    `{planned, sizing, run_id}` (XS: `{route: 'dispatch', sizing}`); stage, execute and review
    are not composed.
    `in_run_commit` composes the docs and commit stages: the run lands its own terminal commit and
    reports the close-job record; without it the EM runs the commit. The run token is read at
    runtime from `args.runToken`; no token bytes are ever emitted.
    On a raw ask in ceo mode an XL `pm-decision` sizing gets its exit picked in-run by the APM;
    `roadmap_blitz_text` (or the plugin asset) embeds the roadmap arm that pick can lead to.
    """
    if bool(prompt) == bool(sizing_rel):
        raise AskComposeRefused("compose_ask_script takes exactly one of prompt / sizing_rel")
    if stop_after not in (None, STOP_AFTER_PLAN):
        raise AskComposeRefused(f"stop_after takes {STOP_AFTER_PLAN!r} or nothing, got {stop_after!r}")

    if review_roster_fragment is None or review_stage_schemas is None:
        from coordinator_core.ops.dispatch_emit.op import _load_review_inputs

        review_roster_fragment, review_stage_schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
    review = parse_execute_review(review_roster_fragment)

    known_arm = _known_arm(repo_root, sizing_rel)
    if preamble and known_arm in (None, ARM_M_PLUS, ARM_ROADMAP):
        raise AskComposeRefused(
            f"--preamble reaches only XS/S row executors; this script's arm is {known_arm or 'unsized'}, "
            "whose executors run from a later emit. Pass --preamble to that plan emit instead."
        )
    blitz_fn: str = ""
    blitz_phases: list[str] = []
    if known_arm in (None, ARM_M_PLUS, ARM_ROADMAP):
        if wrap_stage is None:
            from coordinator_core.ops.dispatch_emit import ask_plan_blitz

            wrap_stage = ask_plan_blitz.wrap_stage
        blitz_text = plan_blitz_text if plan_blitz_text is not None else _read_plan_blitz()
        blitz_fn, blitz_phases = (
            wrap_stage(blitz_text, agent_prefix_var=EM_BRIEF_VAR) if em_brief else wrap_stage(blitz_text)
        )
        if f"async function {_PLAN_BLITZ_FN}(" not in blitz_fn:
            raise AskComposeRefused(f"wrapped plan-blitz stage does not define async function {_PLAN_BLITZ_FN}")

    xl_open = _xl_exit_open(repo_root, sizing_rel)
    # A raw ask is sized in-run, so any size can reach the XL exit.
    xl_possible = not sizing_rel or xl_open
    rb_fn: str = ""
    rb_phases: list[str] = []
    if xl_possible and not em_brief:
        from coordinator_core.ops.dispatch_emit import ask_roadmap
        from coordinator_core.ops.dispatch_emit.ask_plan_blitz import AskPlanBlitzRefused

        try:
            rb_text = roadmap_blitz_text if roadmap_blitz_text is not None else ask_roadmap.load_roadmap_blitz_text()
            rb_fn, rb_phases = ask_roadmap.wrap_roadmap_stage(rb_text)
        except (AskPlanBlitzRefused, OSError):
            # Unembedded, an in-run roadmap pick halts naming the `--ask --sizing` re-fire.
            rb_fn, rb_phases = "", []

    if known_arm == ARM_ROADMAP:
        if em_brief:
            raise AskComposeRefused("the roadmap arm takes no EM brief; drop --brief/--brief-file/--context")
        from coordinator_core.ops.dispatch_emit.ask_roadmap import compose_roadmap_script

        return compose_roadmap_script(
            repo_root=repo_root,
            sizing_rel=sizing_rel,
            run_id=run_id,
            interaction_mode=load_sizing(Path(repo_root), sizing_rel).get("interaction_mode"),
            roadmap_text=roadmap_blitz_text,
            plan_blitz_fn=blitz_fn,
            plan_blitz_phases=blitz_phases,
            plan_blitz_args=plan_blitz_args,
        )

    run_dir = f"{RUN_DIR_ROOT}/{run_id}"
    manifest_rel = f"{run_dir}/manifest.json"
    anchor = _ANCHOR_CLAUSE.format(root=repo_root)
    brief_prefix = f"{_emit._SHARED_PATH_MARKER_DELIM}{EM_BRIEF_VAR}{_emit._SHARED_PATH_MARKER_DELIM}" if em_brief else ""
    head = brief_prefix + _emit._BRIEF_PRECEDENCE_CLAUSE
    session_tail = _emit._dispatching_session_id_paragraph(session_id)
    agent_type = _emit._EXECUTOR_AGENT_TYPE

    def prompt_of(*lines_and_exprs: str) -> str:
        return _cat(f"{head}\n\n{anchor}\n\n", *lines_and_exprs)

    ledger_rel = f"{run_dir}/{LEDGER_VIEW_FILE}"
    ledger_clause = "\n\n" + _LEDGER_CLAUSE.format(ledger=ledger_rel)
    intent_clause = (
        f"\n\nFirst read {run_dir}/{PM_INTENT_FILE} when it exists: the PM's own words and the exit "
        "criterion. They outrank every brief, plan and in-repo memo; judge the change against them, "
        "and treat a claim the change leaves untrue, or a brief that weakened it, as a finding."
    ) + ledger_clause
    review_head = head + intent_clause + session_tail
    run_base_sha = head_sha(repo_root) or ""
    review_blocks = compose_execute_review(
        review,
        stage_schemas=review_stage_schemas,
        plan_path=manifest_rel,
        run_base_sha=run_base_sha,
        declared_paths_js="_manifest.review_declared_paths",
        prompt_head=review_head,
        run_key=run_id,
        prep_suffix_js="' plan: ' + (_planRel ?? _sizingRel)",
        host_degraded=agent_type_host == _emit._AGENT_TYPE_HOST_DEGRADED,
    )
    review_titles = [title for title, _ in review_blocks]
    judge_expr = _criterion_judge_call(
        review,
        review_stage_schemas,
        run_base_sha=run_base_sha,
        head=head + intent_clause,
        agent_type_host=agent_type_host,
    )

    b: list[str] = []
    b.append(f"  const REPO_ROOT = {_lit(repo_root)};")
    b.append(f"  const _runId = {_lit(run_id)};")
    b.append(f"  const _SESSION_ID = {_lit(session_id or '')};")
    b.append(f"  let _sizingRel = {_lit(sizing_rel) if sizing_rel else 'null'};")
    writes_literal = json.dumps(list(writes))
    b.append(f"  let _writes = {writes_literal};")
    b.append(f"  const _crossRepoApproved = {'true' if cross_repo_approved else 'false'};")
    b.append("  let _planRel = null;")
    b.append("  let _gated = [];")
    b.append("  let _manifest = null;")
    b.append("  let _viaRoadmap = false;")
    if in_run_commit:
        b.append(_RUN_TOKEN_DECLARATION)
        b.append("  let _docWrites = [];")
    b.append("  let _nextAction = null;")
    b.append("  let _docs = null;")
    b.append("  let _commit = null;")
    b.append("  let _ledger = null;")
    if rb_fn:
        from coordinator_core.ops.dispatch_emit.ask_roadmap import ROADMAP_DECLS

        b.extend(ROADMAP_DECLS)
        b.append(rb_fn)
    if em_brief:
        b.append(f"  {brief_decl_js(em_brief)}")
        b.append("  const _askAgent = agent;")
    b.append(f"  const {_LEDGER_PREFIX_VAR} = {_lit(_LEDGER_CLAUSE.format(ledger=ledger_rel) + chr(10) * 2)};")
    if blitz_fn:
        b.append(_ledger_shadowed(blitz_fn, em_brief=bool(em_brief)))
    runner_js = _row_runner_js()
    if em_brief:
        if runner_js.count(_ROW_VERIFY_PROMPT_OPEN) != 1:
            raise AskComposeRefused("row runner's verify prompt no longer has the shape the EM brief prefixes")
        runner_js = runner_js.replace(_ROW_VERIFY_PROMPT_OPEN, f"{EM_BRIEF_VAR} + {_ROW_VERIFY_PROMPT_OPEN}")
    b.append(runner_js)
    b.append(_usage_limit_helper_js())
    for name in (*_REVIEW_RESULT_NAMES, _emit._TEST_RESULT_VAR):
        b.append(f"  let {name} = null;")
    if judge_expr is not None:
        b.append(f"  let {_emit._FALSIFIER_RESULT_VAR} = null;")

    if not sizing_rel:
        b.append("  phase('size');")
        scout_prompt = prompt_of(
            "Size this ask afresh, never reusing or editing an existing sizing, by following the "
            "sizing skill (`coordinator:sizing`). Judge only; write no file and run no "
            "`sizing-assemble`. Return its inputs: tshirt (XS|S|M|L|XL|XXL), intent (one sentence), "
            "name (a short kebab-case slug), premise_provenance (executed|read|not-applicable) with "
            "premise_evidence, exit_criterion (one sentence stating what done means), and as `writes` "
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
            + ledger_clause
            + "\n\nAsk:\n"
            + (prompt or "")
        )
        b.append(
            f"  const _scouted = await {_agent(scout_prompt, label='size', phase='size', agent_type=SIZING_SCOUT_AGENT_TYPE, schema=_SCOUT_SCHEMA, agent_model='sonnet')};"
        )
        b.append("  _writes = _scouted.writes ?? [];")
        b.append("  _gated = _scouted.gated ?? [];")
        b.append(
            "  const _slug = String(_scouted.name || 'ask').toLowerCase().replace(/[^a-z0-9]+/g, '-')"
            ".replace(/^-+|-+$/g, '') || 'ask';"
        )
        b.append(f"  const _sizingPath = 'state/sizings/' + {_lit(date.today().isoformat() + '-')} + _slug + '.yaml';")
        b.append("  const _q = (v) => \"'\" + String(v ?? '').replace(/'/g, \"'\\\\''\") + \"'\";")
        deliverable = (
            f" + {_lit(' --deliverable-id ' + shlex.quote(baton['deliverable_id']))}" if baton and baton.get("deliverable_id") else ""
        )
        b.append(
            f"  const _assembleCmd = {_lit(_SIZING_ASSEMBLE)} + ' --tshirt ' + _q(_scouted.tshirt)"
            " + ' --intent ' + _q(_scouted.intent) + ' --name ' + _q(_scouted.name)"
            " + ' --premise-provenance ' + _q(_scouted.premise_provenance)"
            " + ' --premise-evidence ' + _q(_scouted.premise_evidence)"
            f" + ' --exit-criterion ' + _q(_scouted.exit_criterion){deliverable}"
            " + (_scouted.jtbd_unclear === true ? ' --jtbd-unclear' : '')"
            " + (_scouted.well_trodden_step_change === true ? ' --well-trodden-step-change' : '')"
            f" + ' --write ' + _q(_sizingPath) + ' ' + {_lit(NO_PLAN_HAND_OFF_FLAG)};"
        )
        assemble_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "Run exactly this one command and nothing else, then return its exit code as exit_code and "
            "its stdout verbatim as stdout (stderr as stderr). Do not edit, retry or repair it:\n\n",
            "js:_assembleCmd",
        )
        b.append(
            f"  const _assembled = await {_agent(assemble_prompt, label='assemble', phase='size', agent_type=agent_type, schema=_ASSEMBLE_SCHEMA)};"
        )
        b.append("  let _decision = null;")
        # An agent's Bash tool folds stderr into stdout, so sizing-assemble's stderr notes trail the
        # JSON; the decision is the span from the first `{` to the last column-0 `}`.
        b.append(
            "  try { const _out = String(_assembled.stdout ?? ''); "
            "_decision = JSON.parse(_out.slice(_out.indexOf('{'), _out.lastIndexOf('\\n}') + 2)); } "
            "catch (_e) { _decision = null; }"
        )
        b.append(
            "  if (_assembled.exit_code !== 0 || !_decision) { _halted = { halted: "
            f"{_lit(HALT_REFUSAL)}, reason: 'sizing-assemble failed (exit ' + _assembled.exit_code + '); "
            "the gate, plan, stage, execute and review phases were skipped', "
            "detail: String(_assembled.stderr ?? _assembled.stdout ?? '').slice(0, 300), run_id: _runId }; }"
        )
        b.append("  else { _sizingRel = _sizingPath; }")
        b.append(f"  const _pmExit = (_decision && _decision[{_lit(PM_EXIT_QUESTION_KEY)}]) || null;")

    b.append("  phase('gate');")
    gate_payload = "js:JSON.stringify({ sizing_path: _sizingRel, writes: _writes" + (
        ", baton: " + json.dumps(baton["path"]) if baton else ""
    ) + " })"
    gate_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        f"Run `{_INVOKE} {OP_ASK_GATE} '",
        gate_payload,
        "'` and return its JSON reply verbatim as arm, halt, baton and (when present) resume_plan. Also read the sizing at ",
        "js:_sizingRel",
        " and return its estimate.tshirt as tshirt and its route as route.",
    )
    gate_call = _agent(
        gate_prompt, label="gate", phase="gate", agent_type=agent_type, schema=_GATE_SCHEMA, effort=_MECHANICAL_EFFORT
    )
    b.append(
        f"  let _gate = {'_halted ? { arm: null, halt: null } : ' if not sizing_rel else ''}await {gate_call};"
    )

    if accept_pending or xl_possible:
        apm_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "phase: accept\n\nYou are the PM's delegate for one exit criterion. Nobody escalates to the "
            "human here: a scope, direction or priority matter is yours as the APM. Read the sizing at ",
            "js:_sizingRel",
            " and judge its exit_criterion.statement: is it a done-state this ask can be held to? Rule "
            "it as written (verdict 'ruled', statement omitted), or amend it by returning the better "
            "one-sentence statement. When no statement is on record, author one from the sizing's "
            "intent: verdict 'ruled' with the one-sentence done-state as statement. Return the ruling verbatim as ruling. Never rule on a merge, "
            "publish, push to main or cross-repo commit gate: return verdict 'pm-only' with "
            "pmOnlyGround and pmOnly true ONLY when the matter is important AND urgent AND has no clear "
            "right answer, or needs such an external or irreversible action. Being unsure is not a "
            "ground. You stage and commit nothing." + _KEEP_THE_ASK.format(run_dir=run_dir) + ledger_clause,
        )
        apm_call = _agent(
            apm_prompt, label="accept", phase="accept", agent_type=ADJUDICATOR_AGENT_TYPE,
            schema=_ACCEPT_SCHEMA, agent_model="sonnet"
        )
        accept_payload = (
            "js:JSON.stringify({ sizing: _sizingRel, apm_ruling: _apm.ruling, ruling_ref: _runId"
            + ", ...(_apm.statement ? { statement: _apm.statement } : {}) })"
        )
        run_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            f"Run `{_INVOKE} sizing.accept_exit_criterion '",
            accept_payload,
            "'` and return ok true when it succeeds. If it replies `{\"error\": ...}` return ok false "
            "and that message as error.",
        )
        run_call = _agent(
            run_prompt, label="accept-record", phase="accept", agent_type=agent_type,
            schema=_ACCEPT_RESULT_SCHEMA, effort=_MECHANICAL_EFFORT,
        )
        xl_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "phase: xl-exit\n\nYou are the PM's delegate for one XL exit; this sizing runs in ceo mode, "
            "so nobody escalates to the human here. Read the sizing at ",
            "js:_sizingRel",
            " (intent, estimate, exit_criterion, research) and pick its exit: 'accept_multi_session' when "
            "the work is one deliverable a single multi-session plan can carry; 'roadmap' when it spans two "
            "or more named workstreams or needs an initiative or goal link. Return verdict 'ruled', the "
            "pick, and your one-paragraph reasoning verbatim as ruling. Return verdict 'pm-only' with "
            "pmOnly true and pmOnlyGround ONLY when the job-to-be-done itself is unstated, or the work "
            "needs a merge, publish, push to main or cross-repo commit. Being unsure is not a ground. "
            "You stage and commit nothing." + _KEEP_THE_ASK.format(run_dir=run_dir) + ledger_clause,
        )
        xl_call = _agent(
            xl_prompt, label="xl-exit", phase="xl-exit", agent_type=ADJUDICATOR_AGENT_TYPE,
            schema=_XL_SCHEMA, agent_model=_XL_EXIT_MODEL, effort=_XL_EXIT_EFFORT,
        )
        xl_record_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            f"Run `{_INVOKE} sizing.record_xl_exit '",
            "js:JSON.stringify({ sizing: _sizingRel, xl_exit: _xl.pick, apm_ruling: _xl.ruling, ruling_ref: _runId })",
            "'` and return ok true when its reply has exit_code 0. Otherwise return ok false and its "
            "error as error.",
        )
        xl_record_call = _agent(
            xl_record_prompt, label="xl-exit-record", phase="xl-exit", agent_type=agent_type,
            schema=_ACCEPT_RESULT_SCHEMA, effort=_MECHANICAL_EFFORT,
        )
        irreversible = f"new RegExp({_lit(IRREVERSIBLE_GATE_SOURCE)}, 'i')"
        b.append(
            # Each round discharges one halt in-run and re-gates: an XL sizing in ceo mode can
            # owe both the exit pick and the exit-criterion acceptance. No progress ends the loop.
            "  for (let _round = 0; _round < 3 && _gate.halt; _round++) {\n"
            "    let _progressed = false;\n"
            f"    if (_gate.halt.kind === {_lit(HALT_TOUCHPOINT)}) {{\n"
            "    phase('accept');\n"
            "    let _apm = null;\n"
            f"    try {{ _apm = await {apm_call}; }} catch (_e) {{ _apm = null; }}\n"
            f"    const _apmGated = {irreversible}.test(_apm?.ruling ?? '');\n"
            "    if (_apm && _apm.verdict === 'ruled' && _apm.pmOnly !== true && !_apmGated && _apm.ruling) {\n"
            "      let _recorded = null;\n"
            f"      try {{ _recorded = await {run_call}; }} catch (_e) {{ _recorded = null; }}\n"
            "      if (_recorded && _recorded.ok === true) {\n"
            f"        _gate = await {gate_call};\n"
            "        _progressed = true;\n"
            "      }\n"
            "    }\n"
            f"    if (_gate.halt && _gate.halt.kind === {_lit(HALT_TOUCHPOINT)} && _apm) {{\n"
            "      _gate = { ..._gate, halt: { ..._gate.halt, apm: { verdict: _apm.verdict, "
            "pmOnlyGround: _apm.pmOnlyGround ?? (_apmGated ? 'external-or-irreversible' : null), ruling: _apm.ruling ?? null } } };\n"
            "    }\n"
            f"    }} else if ({'true' if xl_possible else 'false'} && _gate.halt.kind === {_lit(HALT_ROOM)} && _gate.halt.apm_xl_exit === true) {{\n"
            "    phase('xl-exit');\n"
            "    let _xl = null;\n"
            f"    try {{ _xl = await {xl_call}; }} catch (_e) {{ _xl = null; }}\n"
            f"    const _xlGated = {irreversible}.test(_xl?.ruling ?? '');\n"
            "    if (_xl && _xl.verdict === 'ruled' && _xl.pmOnly !== true && !_xlGated && _xl.ruling "
            "&& ['roadmap', 'accept_multi_session'].includes(_xl.pick)) {\n"
            "      let _xr = null;\n"
            f"      try {{ _xr = await {xl_record_call}; }} catch (_e) {{ _xr = null; }}\n"
            "      if (_xr && _xr.ok === true) {\n"
            f"        _gate = await {gate_call};\n"
            "        _progressed = true;\n"
            "      }\n"
            "    }\n"
            f"    if (!_progressed && _xl) {{\n"
            "      _gate = { ..._gate, halt: { ..._gate.halt, apm: { verdict: _xl.verdict, pick: _xl.pick ?? null, "
            "pmOnlyGround: _xl.pmOnlyGround ?? (_xlGated ? 'external-or-irreversible' : null), ruling: _xl.ruling ?? null } } };\n"
            "    }\n"
            "    }\n"
            "    if (!_progressed) break;\n"
            "  }"
        )
    if sizing_rel:
        b.append(
            "  if (_gate.halt || !_gate.arm) { _halted = { halted: (_gate.halt && _gate.halt.kind) || "
            f"{_lit(HALT_REFUSAL)}, ..._gate.halt, sizing: _sizingRel, run_id: _runId }}; }}"
        )
    else:
        b.append(
            "  if (_gate.halt || !_gate.arm) { if (!_halted) _halted = { halted: (_gate.halt && _gate.halt.kind) || "
            f"{_lit(HALT_REFUSAL)}, ..._gate.halt, sizing: _sizingRel, run_id: _runId"
            f", ...(_pmExit ? {{ {PM_EXIT_QUESTION_KEY}: _pmExit }} : {{}}) }}; }}"
        )

    stage_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        f"Run `{_INVOKE} {OP_ASK_STAGE} '",
        # ask_stage takes exactly one of plan_path / sizing_path: the plan
        # when a plan phase authored one, else the XS sizing.
        "js:JSON.stringify(_planRel ? { run_id: _runId, plan_path: _planRel, writes: _writes, "
        "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, "
        "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) } "
        ": { run_id: _runId, sizing_path: _sizingRel, writes: _writes, gated: _gated, "
        "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID })",
        "'` and return its JSON reply verbatim. If it replies `{\"error\": ...}`, return that "
        "message as `error` with run_dir and marker_path empty and rows and review_declared_paths "
        "empty -- never an empty manifest without the error.",
    )
    plan_author = _cat(
        f"{head}\n\n{anchor}\n\n",
        "Author the plan for the sizing at ",
        "js:_sizingRel",
        f": scaffold it with `{_DOC_NEW} --type plan --sizing-object <that sizing path> --title "
        "\"<title>\" --out docs/plans/<sizing-stem>.md` (that launcher, never `python3` on the "
        "script; never author the frontmatter block by hand), then change its existing `scope_mode:` line to `spec-dispatch`, derive its "
        "spine from the sizing, then FILL the "
        "scaffold: no PLACEHOLDER, `path/to/file` or `<REPLACE:` marker may remain anywhere in the "
        "plan -- stage refuses a plan that still carries one. Each row states an outcome: the PM's "
        "verbatim words it serves, what is true when it is done, and the files it may touch; the "
        "executor designs the change, so never write the code, a diff or an 'implement exactly' "
        "instruction into a row. The sizing's pm_verbatims outrank any in-repo memo or doctrine. "
        "Return its repo-relative path as plan_rel."
        + ledger_clause
        + " Every ledger item is one the plan's rows must serve; quote each in the row that serves it."
        + (
            " Read the sizing's `scout_evidence` entries, if it has any, before deriving the spine."
            if plan_blitz_args
            else ""
        ),
    )
    b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_S)}) {{")
    b.append("    if (_gate.resume_plan) { _planRel = _gate.resume_plan; } else {")
    b.append("    phase('plan');")
    b.append(
        f"    _planRel = (await {_agent(plan_author, label='plan', phase='plan', agent_type=_PLAN_AUTHOR_AGENT_TYPE, schema=_PLAN_SCHEMA, agent_model=_PLAN_AUTHOR_MODEL, effort=_PLAN_AUTHOR_EFFORT)}).plan_rel;"
    )
    b.append("    }")
    b.append("  }")
    if blitz_fn:
        b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_M_PLUS)}) {{")
        b.append("    phase('plan');")
        # gateReportPath binds from the runtime `_sizingRel`: a raw ask's sizing is minted in-run.
        blitz_spread = f"...{json.dumps(plan_blitz_args, sort_keys=True)}, " if plan_blitz_args else ""
        b.append(
            f"    const _blitz = await {_PLAN_BLITZ_FN}({{ {blitz_spread}mode: 'single', repoRoot: REPO_ROOT, waveIndex: 0, "
            "gateReportPath: REPO_ROOT + '/' + _sizingRel, "
            f"trailDir: {_lit(run_dir + '/' + _BLITZ_TRAIL)}, batons: [{{ ...(_gate.baton ?? {{}}), "
            "sized: true, sizingObject: _sizingRel, tshirt: _gate.tshirt, route: _gate.baton?.route ?? _gate.route, "
            "planPath: _gate.resume_plan ?? null, executionOpen: true }] });"
        )
        b.append("    const _ready = (_blitz?.ready ?? [])[0];")
        b.append("    _planRel = (_ready && typeof _ready === 'object') ? _ready.planPath : _ready;")
        b.append(
            f"    if (!_planRel) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'plan-blitz reported no ready plan', blitz: _blitz, sizing: _sizingRel, run_id: _runId }; }"
        )
        b.append("  }")
    else:
        b.append(
            f"  if (!_halted && _gate.arm === {_lit(ARM_M_PLUS)}) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'sizing resolved to M+ at emit time but the plan-blitz stage was not embedded', "
            "sizing: _sizingRel, run_id: _runId }; }"
        )

    if stop_after:
        b.append(
            "  return _halted ? { halted_by: _halted, sizing: _sizingRel, run_id: _runId } : "
            f"(_gate.arm === {_lit(ARM_XS)} ? {{ route: 'dispatch', sizing: _sizingRel }} : "
            "{ planned: [_planRel], sizing: _sizingRel, run_id: _runId });"
        )
        return _finish_script(
            b,
            manifest_rel,
            "Size, gate and plan from an ask, then stop before stage.",
            _phase_titles(
                with_size=not sizing_rel,
                blitz_phases=blitz_phases,
                review_titles=[],
                with_accept=accept_pending or not sizing_rel,
                through_plan=True,
            ),
        )

    if xl_possible:
        b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_ROADMAP)}) {{")
        b.append("    _viaRoadmap = true;")
        if rb_fn and blitz_fn:
            from coordinator_core.ops.dispatch_emit.ask_roadmap import roadmap_stage_lines

            b.extend(
                roadmap_stage_lines(
                    repo_root=repo_root, run_id=run_id, interaction_mode="ceo", plan_blitz_args=plan_blitz_args
                )
            )
        else:
            b.append(
                f"    _halted = {{ halted: {_lit(HALT_REFUSAL)}, reason: 'the sizing resolved to the roadmap arm, "
                "which this script does not embed: re-fire emit-dispatch-workflow --ask --sizing ' + _sizingRel, "
                "sizing: _sizingRel, run_id: _runId };"
            )
        b.append("  }")
    b.append("  if (!_viaRoadmap) {")
    b.append("  if (!_halted) {")
    b.append("  phase('stage');")
    b.append(
        f"  _manifest = await {_agent(stage_prompt, label='stage', phase='stage', agent_type=agent_type, schema=_MANIFEST_SCHEMA, effort=_MECHANICAL_EFFORT)};"
    )
    b.append("  if (!_manifest.error) { for (const g of (_manifest.gated ?? [])) { _incompleteChunks.push(g.id); } }")
    b.append(
        "  if (_manifest.error || !(_manifest.rows ?? []).length) { _halted = { halted: "
        f"{_lit(HALT_REFUSAL)}, kind: {_lit(HALT_REFUSAL)}, reason: _manifest.error || "
        "'stage staged no rows', sizing: _sizingRel, plan: _planRel, run_id: _runId }; }"
    )
    b.append("  }")

    b.append("  if (!_halted) {")
    b.append("  phase('execute');")
    b.append("  const _rows = {};")
    b.append("  const _waves = [...new Set(_manifest.rows.map((r) => r.wave))].sort((a, b) => a - b);")
    b.append("  for (const w of _waves) {")
    b.append("    for (const r of _manifest.rows.filter((x) => x.wave === w)) {")
    row_prompt = _cat(
        f"{brief_prefix}{_emit._prompt_head(preamble)}\n\n{anchor}\n\n",
        "Your brief is the file ",
        "js:r.brief_path",
        f" -- read it completely, then execute it as written.{ledger_clause}{session_tail}",
    )
    b.append(
        f"      _rows[r.id] = _runRow(r.id, (r.deps ?? []).map((d) => _rows[d]), null, async () => agent({row_prompt}, "
        f"{{ label: {_lit(build_work_label(''))} + r.id, phase: {_lit(_emit._EXECUTE_PHASE_TITLE)}, "
        f"agentType: r.agent_type, model: r.model, stallMs: {_emit._EXECUTOR_STALL_MS} }}));"
    )
    b.append("    }")
    b.append("  }")
    b.append("  await Promise.all(Object.values(_rows));")
    b.append("  await Promise.all(_verifications);")
    b.append("  }")
    b.append(
        "  if (!_halted) { const _r = _heldDependentsReason(); if (_r) "
        f"_halted = {{ halted: {_lit(HALT_DEPENDENTS_UNSTARTED)}, detail: _r }}; }}"
    )

    review_text = _single_exit_review(
        [_emit._unconst(block, _REVIEW_RESULT_NAMES) for _, block in review_blocks]
    )
    b.append("  phase('review');")
    b.append("  if (!_halted) {\n" + review_text + "\n  }")
    test_call = _scoped_test_call(agent_type_host, run_base_sha or "HEAD", brief_prefix or None)
    test_guard = f"_gate.arm !== {_lit(ARM_XS)} && (_manifest.review_declared_paths ?? []).length"
    if judge_expr is None:
        b.append(f"  if (!_halted && {test_guard}) {{")
        b.append(f"    phase({_lit(_emit._TEST_PHASE_TITLE)});")
        b.append("    try {")
        b.append(f"    {_emit._TEST_RESULT_VAR} = await {test_call};")
        b.append("    } catch (e) { _haltOnUsageLimit(e); }")
        b.append("  }")
    else:
        b.append("  if (!_halted) {")
        b.append(f"    phase({_lit(_emit._TEST_PHASE_TITLE)});")
        b.append("    try {")
        b.append(
            f"    [{_emit._TEST_RESULT_VAR}, {_emit._FALSIFIER_RESULT_VAR}] = await parallel([\n"
            f"      () => ({test_guard}) ? {test_call} : null,\n"
            f"      () => {_emit._never_stranding_criterion(judge_expr, judge=True)},\n"
            "    ]);"
        )
        b.append("    } catch (e) { _haltOnUsageLimit(e); }")
        b.append("  }")
    review_vars = _emit.review_stage_vars(
        review,
        bookkeeping_stem_literal=_lit(review_wave_bookkeeping_stem(run_id, None)),
        plan_id_literal="(_manifest.plan_id ?? null)",
    )
    na_kind, na_op, na_params = next_action_parts(
        has_commit_request=True,
        review_vars=review_vars,
        test_var=_emit._TEST_RESULT_VAR,
        falsifier_var=_emit._FALSIFIER_RESULT_VAR if judge_expr is not None else None,
        verification_var="_verifications",
        test_absent_status="not_run",
        script_path=script_path,
        session_id=session_id,
    )
    ledger_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        "phase: ledger-check\n\nRead-only: edit, stage and commit nothing. You are the run's final "
        "checker: judge the result against every requirement the PM has on record, never against the "
        f"plan alone. Re-read the requirements ledger at {ledger_rel} now (revisions may have landed "
        f"since the run started), then {run_dir}/{PM_INTENT_FILE} when it exists, then the run's change "
        "(`git status --porcelain` and `git diff`), the plan when one exists (",
        "js:(_planRel ?? 'none')",
        f"), and the briefs under {run_dir}/briefs. Return one entry in items for EVERY ledger item, by "
        "its id (R1, R2, ...): status 'met' when the change makes the item literally true; 'drifted' "
        "when it was lost or reinterpreted in transit -- a brief, plan, memo or stage narrowed, "
        "softened or re-read it (name where in evidence); 'superseded' only when a LATER revision item "
        "replaces it, named as superseded_by; otherwise 'not_met'. Set essential false only for an "
        "item that is an aside rather than something the PM asked to have delivered; when the ledger "
        "says all_of_pm_ask yes, every item is mandatory and essential does not apply. evidence is one "
        "or two sentences naming the file or behaviour. Return all_of_pm_ask as the ledger states it, "
        "and list any in-repo memo or doctrine that conflicts with the ask as conflicts. verdict is "
        "'pass' only when every item is judged, none drifted, and no mandatory or essential item is "
        "not_met; otherwise 'fail'.",
    )
    ledger_call = _agent(
        ledger_prompt, label="ledger-check", phase="ledger-check",
        # A bare host has no coordinator review type; the prompt carries the whole charter.
        agent_type="general-purpose" if agent_type_host == _emit._AGENT_TYPE_HOST_DEGRADED else _LEDGER_CHECK_AGENT_TYPE,
        schema=_LEDGER_CHECK_SCHEMA, agent_model="sonnet", effort=_MECHANICAL_EFFORT,
    )
    b.append("  if (!_halted && _manifest && !_manifest.error) {")
    b.append("    phase('ledger-check');")
    b.append(f"    try {{ _ledger = await {ledger_call}; }} catch (e) {{ _haltOnUsageLimit(e); }}")
    b.append(
        "    if (!_halted && (!_ledger || _ledger.verdict !== 'pass')) { _halted = { halted: 'ledger-check', "
        "items: (_ledger?.items ?? []).filter((i) => i && i.status !== 'met'), "
        f"conflicts: _ledger?.conflicts ?? [], ledger: {_lit(ledger_rel)}, sizing: _sizingRel, run_id: _runId, "
        "next_action: 'Revise the ask with coordinator-invoke workflow.revise (run_id, verbatim), or fix the "
        "change, then re-fire.' }; }"
    )
    b.append("  }")
    b.append("  }")
    b.append(f"  {CAP_HELPER_JS}")
    b.append(f"  {TERMINAL_COMMIT_CLI_HELPER_JS}")
    b.append(
        "  _nextAction = (_manifest && !_manifest.error) "
        f"? {{ kind: {na_kind}, op: {na_op}, params: {na_params} }} "
        ": { kind: 'none', op: null, params: null };"
    )
    b.append(
        "  if (_nextAction.params && _ledger) { _nextAction.params.ledger_verdict = { items: (_ledger.items ?? [])"
        ".map((i) => ({ id: i.id, status: i.status, essential: i.essential !== false, "
        "...(i.superseded_by ? { superseded_by: i.superseded_by } : {}) })) }; }"
    )
    stage_titles: list[str] = []
    if in_run_commit:
        stage_titles = ["docs", "commit"]
        _append_docs_and_commit(b, head=head, anchor=anchor, repo_root=repo_root, run_id=run_id, manifest_rel=manifest_rel)
    b.append(
        "  return { arm: _gate.arm, sizing: _sizingRel, plan: _planRel, run_id: _runId, "
        f"manifest: {_lit(manifest_rel)}, rows: (_manifest?.rows ?? []).map((r) => r.id), "
        "incomplete: _incompleteChunks, "
        "withheld: ((_manifest && !_manifest.error) ? (_manifest.gated ?? []) : []).map((g) => ({ id: g.id, owner_repo: g.owner_repo ?? '', closure_key: g.closure_key ?? null })), "
        "blocked: _blockedChunks, held_by: _heldBy, unanswered: _unansweredBriefs, "
        "stopped_by: _stoppedBy, not_started: _notStarted, halted_by: _halted, "
        "review: { prep: _reviewPrep, wave: _reviewWave, delivery: _deliveryVerdict, "
        "integration: _reviewIntegration }, "
        + (
            "roadmap: _viaRoadmap ? { gate: _rb, ready: _ready, executed: _executed, workstream_complete: _complete } : null, "
            if rb_fn else ""
        )
        + "ledger: _ledger, docs: _docs, commit: _commit, next_action: _nextAction, "
        f"{TERMINAL_COMMIT_CLI_PROPERTY_JS} }};"
    )

    return _finish_script(
        b,
        manifest_rel,
        (
            f"Plan, stage, execute, review from accepted sizing {Path(sizing_rel).stem}."
            if sizing_rel
            else "One in-session run from an ask to a reviewed result: size, gate, plan, stage, execute, review."
        ),
        _phase_titles(
            with_size=not sizing_rel,
            blitz_phases=blitz_phases,
            review_titles=review_titles,
            judged=judge_expr is not None,
            with_accept=accept_pending or not sizing_rel,
            extra=[
                *(["xl-exit"] if accept_pending or not sizing_rel else []),
                *(["roadmap-blitz", *rb_phases, "workstream-complete"] if rb_fn else []),
                "ledger-check",
                *stage_titles,
            ],
        ),
    )


def _finish_script(b: list[str], manifest_rel: str, description: str, phase_titles: list[str]) -> str:
    """The script text from its composed body lines, refused over the Workflow runner's byte cap."""
    meta = _emit._meta_block("warp-ask", description, phase_titles)
    script = (
        f"{_emit._NODE_CHECK_DOES_NOT_APPLY_COMMENT}\n{meta}\n"
        f"{ASK_MANIFEST_MARKER}{manifest_rel}\n"
        + "\n".join(b)
        + "\n"
    )
    if len(script.encode("utf-8")) > _emit._WORKFLOW_SCRIPT_BYTE_CAP:
        raise AskComposeRefused(
            f"composed ask script is over the Workflow runner's {_emit._WORKFLOW_SCRIPT_BYTE_CAP}-byte cap"
        )
    return script


def _append_docs_and_commit(
    b: list[str], *, head: str, anchor: str, repo_root: str, run_id: str, manifest_rel: str
) -> None:
    """The run's last two stages, under a live terminal-commit `_nextAction` and no halt: a Sonnet
    docs stage (stale README and wiki pages, the delivery note) whose reported files ride the
    commit as `doc_paths`, then a git-commit-agent running `dispatch.terminal_commit` with the run
    token. The commit lands product only: plans, sizings, briefs and sidecars are process records
    it never carries. A landed commit turns `_nextAction` to the close-job record the reply carries: the EM has no commit to run.

    Trap: the commit command must stay one bare `coordinator-invoke` segment -- the
    `block_subagent_commit` run-token leg admits nothing else.
    """
    note_rel = f"{DELIVERY_NOTE_DIR}/{run_id}.md"
    live = "!_halted && !_viaRoadmap && _nextAction && _nextAction.kind === 'terminal_commit' && _RUN_TOKEN"
    docs_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        "phase: docs\n\nThis run's change is on disk, uncommitted. Read the sizing at ",
        "js:_sizingRel",
        "js:(_planRel ? ', the plan at ' + _planRel : '')",
        f", and the run manifest at {manifest_rel}. The run's verdicts, verbatim:\n",
        "js:_cap(JSON.stringify({ ledger: _ledger, delivery: _deliveryVerdict, integration: _reviewIntegration, "
        f"wave: _reviewWave, tests: {_emit._TEST_RESULT_VAR}, criterion: (typeof {_emit._FALSIFIER_RESULT_VAR} === 'undefined' ? null : {_emit._FALSIFIER_RESULT_VAR}) }}), 12000)",
        "\n\n(0) Before you edit anything, decide the complete set of repo-relative paths you will write "
        "(the sizing's `doc_consumers` targets, the `ask_conflicts` paths, stale README and wiki pages, "
        "and the delivery note); that set is your doc_writes, and you write nothing outside it. "
        "(1) Update every README and wiki page (docs/wiki/, wiki/) that the change makes stale "
        "(list the change with `git status --porcelain`); edit only what the change truly alters. "
        "Then update every path the sizing's `doc_consumers` lists (each restates the rule this ask "
        "changes; a `:<line>` or `#<anchor>` suffix names where), and amend or supersede the text at "
        "each `ask_conflicts` path so it no longer contradicts the ask -- the ask outranks it. "
        f"(2) Write the delivery note at {note_rel}: every requirements-ledger item ({RUN_DIR_ROOT}/"
        f"{run_id}/{LEDGER_VIEW_FILE}) with the ledger checker's status for it, what shipped (files "
        "and why), each review finding and how it was resolved, the test result, and every decision "
        "or deviation taken in-run with its reason -- read them off the row briefs under ",
        f"{RUN_DIR_ROOT}/{run_id}/briefs",
        ", which are never committed. The commit carries product files plus the delivery note, "
        "README and wiki pages, and the sizing's doc_consumers and ask_conflicts paths only: never edit a plan, sizing or other process record to carry "
        "this. Never write under scratch/, never stage or commit. Return as doc_writes the set "
        "you declared in (0), as doc_paths every repo-relative file you wrote or edited, and the note's "
        "path as note_path.",
    )
    docs_call = _agent(
        docs_prompt, label="docs", phase="docs", agent_type=_emit._EXECUTOR_AGENT_TYPE,
        schema=_DOCS_SCHEMA, agent_model=_DOCS_MODEL,
    )
    commit_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        "phase: commit\n\nThen run exactly this one command as its own Bash call: no `cd &&`, no "
        "wrapper, no env prefix, no edit to it.\n\n",
        "js:_terminalCommitCli({ ..._nextAction.params, doc_paths: (_docs?.doc_paths ?? []), doc_writes: _docWrites, run_id: _runId, run_token: _RUN_TOKEN })",
        "\n\nReturn from its JSON reply committed, sha, error, refused, sizing_shipped and close, verbatim "
        "(null where absent). Run nothing else.",
    )
    commit_call = _agent(
        commit_prompt, label="commit", phase="commit", agent_type=_COMMIT_AGENT_TYPE,
        schema=_COMMIT_SCHEMA, agent_model=_COMMIT_MODEL, effort=_MECHANICAL_EFFORT,
    )
    b.append(f"  if ({live}) {{")
    b.append("    phase('docs');")
    b.append(f"    try {{ _docs = await {docs_call}; }} catch (e) {{ _haltOnUsageLimit(e); }}")
    b.append("    _docWrites = _docs?.doc_writes ?? [];")
    b.append("  }")
    b.append(f"  if ({live}) {{")
    b.append("    phase('commit');")
    b.append(f"    try {{ _commit = await {commit_call}; }} catch (e) {{ _haltOnUsageLimit(e); }}")
    b.append(
        "    if (_commit && _commit.committed === true) { _nextAction = { kind: 'close_job', op: null, "
        "params: { run_id: _runId, close: _commit.close ?? null } }; }"
    )
    b.append("  }")
