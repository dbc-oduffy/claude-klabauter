"""warp --ask script composer: the one .mjs from an ask (or an existing sizing) to a reviewed run.

The script sizes (raw ask only), asks the engine for a gate verdict, halts or runs the routed arm
(XS stage; S plan-author then stage; M+ plan-blitz then stage), executes the staged manifest rows
and composes the review wave once. Every early exit assigns the typed `_halted` and later phases
run under `if (!_halted)`: the terminal return is the script's one exit. Rows, briefs and pathspecs are Python-composed by
dispatch.ask_stage; the script only interprets the manifest. Pure composition: no spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.git.git_state import head_sha
from coordinator_core.ops.dispatch_emit import emit as _emit
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR, EmBrief, brief_decl_js
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    ASK_PHASES,
    HALT_REFUSAL,
    HALT_TOUCHPOINT,
    OP_ASK_GATE,
    OP_ASK_STAGE,
    RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import STAGE_FN as _PLAN_BLITZ_FN
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
    prompt_expr: str, *, label: str, phase: str, agent_type: str, schema: dict, agent_model: Optional[str] = None
) -> str:
    return (
        f"agent({prompt_expr}, {{ label: {_lit(label)}, phase: {_lit(phase)}, "
        f"agentType: {_lit(agent_type)}, {_emit._model_opt(agent_type, agent_model)}, "
        f"schema: {json.dumps(schema, sort_keys=True)} }})"
    )


def _obj(required: list[str], properties: dict) -> dict:
    return {"type": "object", "required": required, "properties": properties}


_STR = {"type": "string"}
_SIZE_SCHEMA = _obj(
    ["sizing_rel", "writes"],
    {
        "sizing_rel": _STR,
        "writes": {"type": "array", "items": _STR},
        "gated": {
            "type": "array",
            "items": _obj(["title", "owner_repo"], {"title": _STR, "owner_repo": _STR, "requires": _STR}),
        },
    },
)
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
    judge_titles = [CRITERION_JUDGE_PHASE_TITLE] if judged else []
    for extra in [_emit._EXECUTE_PHASE_TITLE, *review_titles, *judge_titles, _emit._TEST_PHASE_TITLE]:
        if extra not in titles:
            titles.append(extra)
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
            "  const _incompleteChunks = [];",
            f"  const {_emit.GATE_OWED_VAR} = {{}};",
            "  const _blockedChunks = [];",
            "  const _unansweredBriefs = [];",
            "  const _stoppedBy = [];",
            "  const _notStarted = [];",
            f"  const {_emit.PLAN_HELD_VAR} = {{}};",
            "  let _halted = null;",
            "  const _verifications = [];",
            "  const _rowPlan = {};",
            "  const _haltedPlans = new Set();",
            "  const _haltedPlanReasons = new Map();",
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
    authoring.
    """
    if bool(prompt) == bool(sizing_rel):
        raise AskComposeRefused("compose_ask_script takes exactly one of prompt / sizing_rel")

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

    review_head = head + session_tail
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
        head=head,
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
    if em_brief:
        b.append(f"  {brief_decl_js(em_brief)}")
        b.append("  const _askAgent = agent;")
    if blitz_fn:
        b.append(blitz_fn)
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
        size_prompt = prompt_of(
            "Size this ask afresh, never reusing or editing an existing sizing routed `shape`, by "
            "following the sizing skill (`coordinator:sizing`) to a sizing the "
            "gate can read, in this order: (1) run `sizing-assemble` for the estimate and route; "
            "(2) scaffold with `coordinator-doc-new --type sizing-object`, passing --tshirt, "
            "--route, --name, --premise executed|read|not-applicable with --premise-evidence, --exit-criterion (one sentence "
            "stating what done means) and --interaction-mode (the `interaction_mode` step (1)'s sizing-assemble returned, verbatim; never choose one); "
            + (
                f"also pass --deliverable-id {baton['deliverable_id']} to that scaffold call, and "
                if baton
                else ""
            )
            + "(3) edit the scaffolded file's `status` from `draft` to `sized`. Leave "
            "`exit_criterion.accepted` null: never accept it yourself; the gate halts at the "
            "touchpoint when the mode asks the PM. Return the sizing's repo-relative path as "
            "sizing_rel, and as `writes` the repo-relative files the ask will create, edit or "
            "delete -- every file the ask names, plus any you find it must touch. An XS with "
            "empty `writes` is refused at the gate. Return as `gated` every deliverable the ask declares "
            "as owned by another repo or blocked on an external gate (title, owner_repo, requires): "
            "never fold one into `writes` and never omit one.\n\nAsk:\n"
            + (prompt or "")
        )
        b.append(
            f"  const _sized = await {_agent(size_prompt, label='size', phase='size', agent_type=agent_type, schema=_SIZE_SCHEMA)};"
        )
        b.append("  _sizingRel = _sized.sizing_rel;")
        b.append("  _writes = _sized.writes ?? [];")
        b.append("  _gated = _sized.gated ?? [];")

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
    gate_call = _agent(gate_prompt, label="gate", phase="gate", agent_type=agent_type, schema=_GATE_SCHEMA)
    b.append(f"  let _gate = await {gate_call};")

    if accept_pending or not sizing_rel:
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
            "ground. You stage and commit nothing.",
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
            run_prompt, label="accept-record", phase="accept", agent_type=agent_type, schema=_ACCEPT_RESULT_SCHEMA
        )
        b.append(
            f"  if (_gate.halt && _gate.halt.kind === {_lit(HALT_TOUCHPOINT)}) {{\n"
            "    phase('accept');\n"
            "    let _apm = null;\n"
            f"    try {{ _apm = await {apm_call}; }} catch (_e) {{ _apm = null; }}\n"
            f"    const _apmGated = new RegExp({_lit(IRREVERSIBLE_GATE_SOURCE)}, 'i').test(_apm?.ruling ?? '');\n"
            "    if (_apm && _apm.verdict === 'ruled' && _apm.pmOnly !== true && !_apmGated && _apm.ruling) {\n"
            "      let _recorded = null;\n"
            f"      try {{ _recorded = await {run_call}; }} catch (_e) {{ _recorded = null; }}\n"
            "      if (_recorded && _recorded.ok === true) {\n"
            f"        _gate = await {gate_call};\n"
            "      }\n"
            "    }\n"
            f"    if (_gate.halt && _gate.halt.kind === {_lit(HALT_TOUCHPOINT)} && _apm) {{\n"
            "      _gate = { ..._gate, halt: { ..._gate.halt, apm: { verdict: _apm.verdict, "
            "pmOnlyGround: _apm.pmOnlyGround ?? (_apmGated ? 'external-or-irreversible' : null), ruling: _apm.ruling ?? null } } };\n"
            "    }\n"
            "  }"
        )
    b.append(
        "  if (_gate.halt || !_gate.arm) { _halted = { halted: (_gate.halt && _gate.halt.kind) || "
        f"{_lit(HALT_REFUSAL)}, ..._gate.halt, sizing: _sizingRel, run_id: _runId }}; }}"
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
        "script; never hand-write frontmatter) and set `scope_mode: spec-dispatch`, derive its "
        "spine from the sizing, then FILL the "
        "scaffold: no PLACEHOLDER, `path/to/file` or `<REPLACE:` marker may remain anywhere in the "
        "plan -- stage refuses a plan that still carries one. Return its repo-relative path as plan_rel."
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
        f"    _planRel = (await {_agent(plan_author, label='plan', phase='plan', agent_type=agent_type, schema=_PLAN_SCHEMA)}).plan_rel;"
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

    b.append("  if (!_halted) {")
    b.append("  phase('stage');")
    b.append(
        f"  _manifest = await {_agent(stage_prompt, label='stage', phase='stage', agent_type=agent_type, schema=_MANIFEST_SCHEMA)};"
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
        f" -- read it completely, then execute it as written.{session_tail}",
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
    b.append(
        f"  {CAP_HELPER_JS}\n"
        f"  {TERMINAL_COMMIT_CLI_HELPER_JS}\n"
        "  return { arm: _gate.arm, sizing: _sizingRel, plan: _planRel, run_id: _runId, "
        f"manifest: {_lit(manifest_rel)}, rows: (_manifest?.rows ?? []).map((r) => r.id), "
        "incomplete: _incompleteChunks, "
        "withheld: ((_manifest && !_manifest.error) ? (_manifest.gated ?? []) : []).map((g) => ({ id: g.id, owner_repo: g.owner_repo ?? '', closure_key: g.closure_key ?? null })), "
        "blocked: _blockedChunks, held_by: _heldBy, unanswered: _unansweredBriefs, "
        "stopped_by: _stoppedBy, not_started: _notStarted, halted_by: _halted, "
        "review: { prep: _reviewPrep, wave: _reviewWave, delivery: _deliveryVerdict, "
        "integration: _reviewIntegration }, "
        f"next_action: {{ kind: {na_kind}, op: {na_op}, params: {na_params} }}, "
        "...((_manifest && !_manifest.error) ? {} : { next_action: { kind: 'none', op: null, params: null } }),"
        f"{TERMINAL_COMMIT_CLI_PROPERTY_JS} }};"
    )

    meta = _emit._meta_block(
        "warp-ask",
        (
            f"Plan, stage, execute, review from accepted sizing {Path(sizing_rel).stem}."
            if sizing_rel
            else "One in-session run from an ask to a reviewed result: size, gate, plan, stage, execute, review."
        ),
        _phase_titles(with_size=not sizing_rel, blitz_phases=blitz_phases, review_titles=review_titles, judged=judge_expr is not None, with_accept=accept_pending or not sizing_rel),
    )
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
