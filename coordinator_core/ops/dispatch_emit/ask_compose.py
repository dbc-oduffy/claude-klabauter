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
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    ASK_PHASES,
    HALT_REFUSAL,
    OP_ASK_GATE,
    OP_ASK_STAGE,
    RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import STAGE_FN as _PLAN_BLITZ_FN
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ARM_M_PLUS,
    ARM_ROADMAP,
    ARM_S,
    ARM_XS,
    SizingFireRefused,
    load_sizing,
    resolve_arm,
)
from coordinator_core.ops.dispatch_emit.wake_digest import next_action_parts
from coordinator_core.ops.dispatch_emit.work_label import build_work_label
from coordinator_core.ops.review_mint.execute_review import (
    CRITERION_JUDGE_PHASE_TITLE,
    compose_criterion_judge,
    compose_execute_review,
)
from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN, parse_execute_review
from coordinator_core.ops.review_mint.wave_bookkeeping import review_wave_bookkeeping_stem
from coordinator_core.ops.workflow_scaffold import _js_string_literal

_BLITZ_TRAIL = "blitz"
_INVOKE = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'
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
    return " + ".join(p[3:] if p.startswith("js:") else _lit(p) for p in parts)


def _agent(prompt_expr: str, *, label: str, phase: str, agent_type: str, schema: dict) -> str:
    return (
        f"agent({prompt_expr}, {{ label: {_lit(label)}, phase: {_lit(phase)}, "
        f"agentType: {_lit(agent_type)}, {_emit._model_opt(agent_type)}, "
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
    },
)
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
                ["id", "agent_type", "model", "brief_path", "writes", "wave"],
                {
                    "id": _STR,
                    "agent_type": _STR,
                    "model": _STR,
                    "brief_path": _STR,
                    "writes": {"type": "array", "items": _STR},
                    "wave": {"type": "integer"},
                },
            ),
        },
        "review_declared_paths": {"type": "array", "items": _STR},
        "marker_path": _STR,
        "gated": {"type": "array", "items": _obj(["id"], {"id": _STR, "reason": _STR, "gate": _STR, "owner_repo": _STR, "closure_key": {}})},
    },
)


_SCOPE_SLOT = "SCOPE_SLOT_X"


def _scoped_test_call(agent_type_host: Optional[str]) -> str:
    """The plan route's terminal test agent call, scoped at run time to the manifest's declared paths.

    The plan is authored inside the run, so the scope cannot resolve at compose time; the
    stage's `review_declared_paths` stand in. Trap: no falsifier leg -- the plan's falsifier
    is unknown until the plan exists.
    """
    call = _emit._test_agent_call_expr([_SCOPE_SLOT], agent_type_host=agent_type_host)
    return call.replace(f"[{_SCOPE_SLOT}]", "[' + _manifest.review_declared_paths.join(', ') + ']")


_USAGE_LIMIT_RE = r"/(usage|session|rate|weekly|5-hour) limit|limit reached|resets \d{1,2}(:\d{2})?\s*(am|pm)?|quota/i"
HALT_USAGE_LIMIT = "usage_limit"


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


def _phase_titles(*, with_size: bool, blitz_phases: list[str], review_titles: list[str]) -> list[str]:
    titles: list[str] = []
    for phase in ASK_PHASES:
        if phase == "size" and not with_size:
            continue
        titles.append(phase)
        if phase == "plan":
            titles.extend(t for t in blitz_phases if t not in titles)
    for extra in [_emit._EXECUTE_PHASE_TITLE, *review_titles, _emit._TEST_PHASE_TITLE]:
        if extra not in titles:
            titles.append(extra)
    return titles


_REVIEW_NOOP_EXIT = "return { halted: 'no-op',"
_REVIEW_NOOP_TAIL = "wave: null, integration: null }; }"
_REVIEW_SLICES_GUARD = "  if (!_reviewPrep || !(_reviewPrep.slices ?? []).length) {"


def _single_exit_review(blocks: list[str]) -> str:
    """The review blocks with prep's no-op `return` turned into a `_halted` assignment.

    Everything after the no-op check, the rest of prep and every later block, runs under
    `if (!_halted)`, so the no-op reaches the terminal return like any other halt.
    """
    prep = blocks[0]
    if prep.count(_REVIEW_NOOP_EXIT) != 1 or _REVIEW_NOOP_TAIL not in prep or _REVIEW_SLICES_GUARD not in prep:
        raise AskComposeRefused("review prep block no longer has the no-op exit shape single-exit composition rewrites")
    prep = prep.replace(_REVIEW_NOOP_EXIT, "_halted = { halted: 'no-op',")
    prep = prep.replace(_REVIEW_SLICES_GUARD, "  if (!_halted && (!_reviewPrep || !(_reviewPrep.slices ?? []).length)) {", 1)
    rest = "\n\n".join(blocks[1:])
    return prep + ("\n\n  if (!_halted) {\n" + rest + "\n  }" if rest else "")


def _row_runner_js() -> str:
    """Declarations `_emit._run_row_helper_js` reads, with no plan-scoped halting (one run, one plan)."""
    return "\n".join(
        [
            "  const _incompleteChunks = [];",
            "  const _blockedChunks = [];",
            "  const _unansweredBriefs = [];",
            "  const _stoppedBy = [];",
            "  const _notStarted = [];",
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
    roadmap_blitz_text: Optional[str] = None,
    agent_type_host: Optional[str] = None,
) -> str:
    """The .mjs text for one ask: a raw `prompt`, or an existing `sizing_rel` (size phase omitted).

    The review roster and stage schemas load from DoE when not injected; `wrap_stage` and
    `plan_blitz_text` default to `ask_plan_blitz.wrap_stage` over the resolved plugin asset, which
    is embedded only when the arm can be M+. `script_path` is the repo-relative path the script
    is written to, carried into `next_action.params` for `dispatch.terminal_commit`.
    `plan_blitz_args` is spread first into the planBlitz call, so `mode`, `repoRoot` and `batons`
    always win; `writes` seeds the emit-time write set the gate and stage ops receive.
    """
    if bool(prompt) == bool(sizing_rel):
        raise AskComposeRefused("compose_ask_script takes exactly one of prompt / sizing_rel")

    if review_roster_fragment is None or review_stage_schemas is None:
        from coordinator_core.ops.dispatch_emit.op import _load_review_inputs

        review_roster_fragment, review_stage_schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
    review = parse_execute_review(review_roster_fragment)

    known_arm = _known_arm(repo_root, sizing_rel)
    blitz_fn: str = ""
    blitz_phases: list[str] = []
    if known_arm in (None, ARM_M_PLUS, ARM_ROADMAP):
        if wrap_stage is None:
            from coordinator_core.ops.dispatch_emit import ask_plan_blitz

            wrap_stage = ask_plan_blitz.wrap_stage
        blitz_fn, blitz_phases = wrap_stage(plan_blitz_text if plan_blitz_text is not None else _read_plan_blitz())
        if f"async function {_PLAN_BLITZ_FN}(" not in blitz_fn:
            raise AskComposeRefused(f"wrapped plan-blitz stage does not define async function {_PLAN_BLITZ_FN}")

    if known_arm == ARM_ROADMAP:
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
    head = _emit._BRIEF_PRECEDENCE_CLAUSE
    session_tail = _emit._dispatching_session_id_paragraph(session_id)
    agent_type = _emit._EXECUTOR_AGENT_TYPE

    def prompt_of(*lines_and_exprs: str) -> str:
        return _cat(f"{head}\n\n{anchor}\n\n", *lines_and_exprs)

    review_head = head + session_tail
    review_blocks = compose_execute_review(
        review,
        stage_schemas=review_stage_schemas,
        plan_path=manifest_rel,
        run_base_sha=head_sha(repo_root) or "",
        declared_paths_js="_manifest.review_declared_paths",
        prompt_head=review_head,
        run_key=run_id,
        prep_suffix_js="' plan: ' + (_planRel ?? _sizingRel)",
        host_degraded=agent_type_host == _emit._AGENT_TYPE_HOST_DEGRADED,
    )
    review_titles = [title for title, _ in review_blocks]

    b: list[str] = []
    b.append(f"  const REPO_ROOT = {_lit(repo_root)};")
    b.append(f"  const _runId = {_lit(run_id)};")
    b.append(f"  const _SESSION_ID = {_lit(session_id or '')};")
    b.append(f"  let _sizingRel = {_lit(sizing_rel) if sizing_rel else 'null'};")
    writes_literal = json.dumps(list(writes))
    b.append(f"  let _writes = {writes_literal};")
    b.append("  let _planRel = null;")
    b.append("  let _gated = [];")
    b.append("  let _manifest = null;")
    if blitz_fn:
        b.append(blitz_fn)
    b.append(_row_runner_js())
    b.append(_usage_limit_helper_js())
    for name in (*_REVIEW_RESULT_NAMES, _emit._TEST_RESULT_VAR, _emit._FALSIFIER_RESULT_VAR):
        b.append(f"  let {name} = null;")

    if not sizing_rel:
        b.append("  phase('size');")
        size_prompt = prompt_of(
            "Size this ask afresh, never reusing or editing an existing sizing routed `shape`, by "
            "following the sizing skill (`coordinator:sizing`) to a sizing the "
            "gate can read, in this order: (1) run `sizing-assemble` for the estimate and route; "
            "(2) scaffold with `coordinator-doc-new --type sizing-object`, passing --tshirt, "
            "--route, --name, --premise executed|read|not-applicable with --premise-evidence, --exit-criterion (one sentence "
            "stating what done means) and --interaction-mode (the `interaction_mode` step (1)'s sizing-assemble returned, verbatim; never choose one); "
            "(3) edit the scaffolded file's `status` from `draft` to `sized`. Leave "
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
    gate_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        f"Run `{_INVOKE} {OP_ASK_GATE} '",
        "js:JSON.stringify({ sizing_path: _sizingRel, writes: _writes })",
        "'` and return its JSON reply verbatim as arm, halt and baton. Also read the sizing at ",
        "js:_sizingRel",
        " and return its estimate.tshirt as tshirt and its route as route.",
    )
    b.append(
        f"  const _gate = await {_agent(gate_prompt, label='gate', phase='gate', agent_type=agent_type, schema=_GATE_SCHEMA)};"
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
        "js:JSON.stringify(_planRel ? { run_id: _runId, plan_path: _planRel, writes: _writes, session_id: _SESSION_ID, ...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) } "
        ": { run_id: _runId, sizing_path: _sizingRel, writes: _writes, gated: _gated, session_id: _SESSION_ID })",
        "'` and return its JSON reply verbatim. If it replies `{\"error\": ...}`, return that "
        "message as `error` with run_dir and marker_path empty and rows and review_declared_paths "
        "empty -- never an empty manifest without the error.",
    )
    plan_author = _cat(
        f"{head}\n\n{anchor}\n\n",
        "Author the plan for the sizing at ",
        "js:_sizingRel",
        ": scaffold `docs/plans/<sizing-stem>.md` with `scope_mode: spec-dispatch` through the plan "
        "skill (never hand-write frontmatter), derive its spine from the sizing, then FILL the "
        "scaffold: no PLACEHOLDER, `path/to/file` or `<REPLACE:` marker may remain anywhere in the "
        "plan -- stage refuses a plan that still carries one. Return its repo-relative path as plan_rel."
        + (
            " Read the sizing's `scout_evidence` entries before deriving the spine."
            if plan_blitz_args
            else ""
        ),
    )
    b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_S)}) {{")
    b.append("    phase('plan');")
    b.append(
        f"    _planRel = (await {_agent(plan_author, label='plan', phase='plan', agent_type=agent_type, schema=_PLAN_SCHEMA)}).plan_rel;"
    )
    b.append("  }")
    if blitz_fn:
        b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_M_PLUS)}) {{")
        b.append("    phase('plan');")
        blitz_spread = f"...{json.dumps(plan_blitz_args, sort_keys=True)}, " if plan_blitz_args else ""
        b.append(
            f"    const _blitz = await {_PLAN_BLITZ_FN}({{ {blitz_spread}mode: 'single', repoRoot: REPO_ROOT, waveIndex: 0, "
            f"trailDir: {_lit(run_dir + '/' + _BLITZ_TRAIL)}, batons: [{{ ...(_gate.baton ?? {{}}), "
            "sized: true, sizingObject: _sizingRel, tshirt: _gate.tshirt, route: _gate.baton?.route ?? _gate.route, "
            "planPath: null, executionOpen: true }] });"
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
    b.append("  let _prev = [];")
    b.append("  for (const w of _waves) {")
    b.append("    const _cur = [];")
    b.append("    for (const r of _manifest.rows.filter((x) => x.wave === w)) {")
    row_prompt = _cat(
        f"{_emit._prompt_head(None)}\n\n{anchor}\n\n",
        "Your brief is the file ",
        "js:r.brief_path",
        f" -- read it completely, then execute it as written.{session_tail}",
    )
    b.append(
        f"      _rows[r.id] = _runRow(r.id, _prev, null, async () => agent({row_prompt}, "
        f"{{ label: {_lit(build_work_label(''))} + r.id, phase: {_lit(_emit._EXECUTE_PHASE_TITLE)}, "
        f"agentType: r.agent_type, model: r.model, stallMs: {_emit._EXECUTOR_STALL_MS} }}));"
    )
    b.append("      _cur.push(_rows[r.id]);")
    b.append("    }")
    b.append("    _prev = _cur;")
    b.append("  }")
    b.append("  await Promise.all(Object.values(_rows));")
    b.append("  await Promise.all(_verifications);")
    b.append("  }")

    review_text = _single_exit_review(
        [_emit._unconst(block, _REVIEW_RESULT_NAMES) for _, block in review_blocks]
    )
    b.append("  phase('review');")
    b.append("  if (!_halted) {\n" + review_text + "\n  }")
    b.append(
        f"  if (!_halted && _gate.arm !== {_lit(ARM_XS)} && (_manifest.review_declared_paths ?? []).length) {{"
    )
    b.append(f"    phase({_lit(_emit._TEST_PHASE_TITLE)});")
    b.append("    try {")
    b.append(f"    {_emit._TEST_RESULT_VAR} = await {_scoped_test_call(agent_type_host)};")
    b.append("    } catch (e) { _haltOnUsageLimit(e); }")
    b.append("  }")
    judge_expr = compose_criterion_judge(
        review,
        stage_schemas=review_stage_schemas,
        plan_path=manifest_rel,
        run_base_sha=head_sha(repo_root) or "",
        falsifier=None,
        prompt_head=review_head,
        host_degraded=agent_type_host == _emit._AGENT_TYPE_HOST_DEGRADED,
        prompt_suffix_js="'\nplan: ' + (_planRel ?? _sizingRel) + ' (a sizing: its exit_criterion is the criterion)'",
    )
    if judge_expr:
        b.append(f"  if (!_halted && _manifest && !_manifest.error) {{")
        b.append(f"    phase({_lit(CRITERION_JUDGE_PHASE_TITLE)});")
        b.append(
            f"    {_emit._FALSIFIER_RESULT_VAR} = await "
            f"{_emit._never_stranding_criterion(judge_expr, judge=True)};"
        )
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
        falsifier_var=_emit._FALSIFIER_RESULT_VAR if judge_expr else None,
        verification_var="_verifications",
        test_absent_status="not_run",
        script_path=script_path,
        session_id=session_id,
    )
    b.append(
        "  return { arm: _gate.arm, sizing: _sizingRel, plan: _planRel, run_id: _runId, "
        f"manifest: {_lit(manifest_rel)}, rows: (_manifest?.rows ?? []).map((r) => r.id), "
        "incomplete: _incompleteChunks, "
        "withheld: ((_manifest && !_manifest.error) ? (_manifest.gated ?? []) : []).map((g) => ({ id: g.id, owner_repo: g.owner_repo ?? '', closure_key: g.closure_key ?? null })), "
        "blocked: _blockedChunks, held_by: _heldBy, unanswered: _unansweredBriefs, "
        "stopped_by: _stoppedBy, not_started: _notStarted, halted_by: _halted, "
        "review: { prep: _reviewPrep, wave: _reviewWave, delivery: _deliveryVerdict, "
        "integration: _reviewIntegration }, "
        f"next_action: {{ kind: {na_kind}, op: {na_op}, params: {na_params} }}, "
        "...((_manifest && !_manifest.error) ? {} : { next_action: { kind: 'none', op: null, params: null } }) };"
    )

    meta = _emit._meta_block(
        "warp-ask",
        "One in-session run from an ask to a reviewed result: size, gate, plan, stage, execute, review.",
        _phase_titles(with_size=not sizing_rel, blitz_phases=blitz_phases, review_titles=[*review_titles, *([CRITERION_JUDGE_PHASE_TITLE] if judge_expr else [])]),
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
