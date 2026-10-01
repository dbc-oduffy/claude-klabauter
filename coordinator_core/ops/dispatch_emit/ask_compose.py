"""warp --ask script composer: the one .mjs from an ask (or an existing sizing) to a reviewed run.

The script sizes (raw ask only), asks the engine for a gate verdict, halts or runs the routed arm
(XS stage; S plan-author then stage; M+ plan-blitz then stage), executes the staged manifest rows
and composes the review wave once. Rows, briefs and pathspecs are Python-composed by
dispatch.ask_stage; the script only interprets the manifest. Pure composition: no spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

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
    ARM_S,
    ARM_XS,
    SizingFireRefused,
    load_sizing,
    resolve_arm,
)
from coordinator_core.ops.dispatch_emit.work_label import build_work_label
from coordinator_core.ops.review_mint.execute_review import compose_execute_review
from coordinator_core.ops.review_mint.roster import parse_execute_review
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
    ["sizing_rel"], {"sizing_rel": _STR, "writes": {"type": "array", "items": _STR}}
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
    },
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
    for extra in [_emit._EXECUTE_PHASE_TITLE, *review_titles]:
        if extra not in titles:
            titles.append(extra)
    return titles


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
) -> str:
    """The .mjs text for one ask: a raw `prompt`, or an existing `sizing_rel` (size phase omitted).

    The review roster and stage schemas load from DoE when not injected; `wrap_stage` and
    `plan_blitz_text` default to `ask_plan_blitz.wrap_stage` over the resolved plugin asset, which
    is embedded only when the arm can be M+.
    """
    if bool(prompt) == bool(sizing_rel):
        raise AskComposeRefused("compose_ask_script takes exactly one of prompt / sizing_rel")

    if review_roster_fragment is None or review_stage_schemas is None:
        from coordinator_core.ops.dispatch_emit.op import _load_review_inputs

        review_roster_fragment, review_stage_schemas = _load_review_inputs(_emit.EMIT_ROUTE_PLAN)
    review = parse_execute_review(review_roster_fragment)

    known_arm = _known_arm(repo_root, sizing_rel)
    blitz_fn: str = ""
    blitz_phases: list[str] = []
    if known_arm in (None, ARM_M_PLUS):
        if wrap_stage is None:
            from coordinator_core.ops.dispatch_emit import ask_plan_blitz

            wrap_stage = ask_plan_blitz.wrap_stage
        blitz_fn, blitz_phases = wrap_stage(plan_blitz_text if plan_blitz_text is not None else _read_plan_blitz())
        if f"async function {_PLAN_BLITZ_FN}(" not in blitz_fn:
            raise AskComposeRefused(f"wrapped plan-blitz stage does not define async function {_PLAN_BLITZ_FN}")

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
        prep_suffix_js="' plan: ' + (_planRel ?? _sizingRel)",
    )
    review_titles = [title for title, _ in review_blocks]

    b: list[str] = []
    b.append(f"  const REPO_ROOT = {_lit(repo_root)};")
    b.append(f"  const _runId = {_lit(run_id)};")
    b.append(f"  let _sizingRel = {_lit(sizing_rel) if sizing_rel else 'null'};")
    b.append("  let _writes = [];")
    b.append("  let _planRel = null;")
    if blitz_fn:
        b.append(blitz_fn)
    b.append(_row_runner_js())
    for name in _REVIEW_RESULT_NAMES:
        b.append(f"  let {name} = null;")

    if not sizing_rel:
        b.append("  phase('size');")
        size_prompt = prompt_of(
            "Size this ask by following the sizing skill (`coordinator:sizing`) to a sizing the "
            "gate can read, in this order: (1) run `sizing-assemble` for the estimate and route; "
            "(2) scaffold with `coordinator-doc-new --type sizing-object`, passing --tshirt, "
            "--route, --name, --premise with --premise-evidence, --exit-criterion (one sentence "
            "stating what done means) and --interaction-mode (the mode this session runs under); "
            "(3) edit the scaffolded file's `status` from `draft` to `sized`. Leave "
            "`exit_criterion.accepted` null: never accept it yourself; the gate halts at the "
            "touchpoint when the mode asks the PM. Return the sizing's repo-relative path as "
            "sizing_rel. When the estimate is XS, also return the file footprint the ask will "
            "write as repo-relative `writes`.\n\nAsk:\n"
            + (prompt or "")
        )
        b.append(
            f"  const _sized = await {_agent(size_prompt, label='size', phase='size', agent_type=agent_type, schema=_SIZE_SCHEMA)};"
        )
        b.append("  _sizingRel = _sized.sizing_rel;")
        b.append("  _writes = _sized.writes ?? [];")

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
        "  if (_gate.halt || !_gate.arm) { return { halted: (_gate.halt && _gate.halt.kind) || "
        f"{_lit(HALT_REFUSAL)}, ..._gate.halt, sizing: _sizingRel, run_id: _runId }}; }}"
    )

    stage_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        f"Run `{_INVOKE} {OP_ASK_STAGE} '",
        "js:JSON.stringify({ run_id: _runId, plan_path: _planRel, sizing_path: _sizingRel, writes: _writes })",
        "'` and return its JSON reply verbatim.",
    )
    plan_author = _cat(
        f"{head}\n\n{anchor}\n\n",
        "Author the plan for the sizing at ",
        "js:_sizingRel",
        ": scaffold `docs/plans/<sizing-stem>.md` with `scope_mode: spec-dispatch` through the plan "
        "skill (never hand-write frontmatter), derive its spine from the sizing, and return its "
        "repo-relative path as plan_rel.",
    )
    b.append(f"  if (_gate.arm === {_lit(ARM_S)}) {{")
    b.append("    phase('plan');")
    b.append(
        f"    _planRel = (await {_agent(plan_author, label='plan', phase='plan', agent_type=agent_type, schema=_PLAN_SCHEMA)}).plan_rel;"
    )
    b.append("  }")
    if blitz_fn:
        b.append(f"  if (_gate.arm === {_lit(ARM_M_PLUS)}) {{")
        b.append("    phase('plan');")
        b.append(
            f"    const _blitz = await {_PLAN_BLITZ_FN}({{ mode: 'single', repoRoot: REPO_ROOT, waveIndex: 0, "
            f"trailDir: {_lit(run_dir + '/' + _BLITZ_TRAIL)}, batons: [{{ ...(_gate.baton ?? {{}}), "
            "sized: true, sizingObject: _sizingRel, tshirt: _gate.tshirt, route: _gate.route, "
            "planPath: null, executionOpen: true }] });"
        )
        b.append("    const _ready = (_blitz?.ready ?? [])[0];")
        b.append("    _planRel = (_ready && typeof _ready === 'object') ? _ready.planPath : _ready;")
        b.append(
            f"    if (!_planRel) {{ return {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'plan-blitz reported no ready plan', blitz: _blitz, sizing: _sizingRel, run_id: _runId }; }"
        )
        b.append("  }")
    else:
        b.append(
            f"  if (_gate.arm === {_lit(ARM_M_PLUS)}) {{ return {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'sizing resolved to M+ at emit time but the plan-blitz stage was not embedded', "
            "sizing: _sizingRel, run_id: _runId }; }"
        )

    b.append("  phase('stage');")
    b.append(
        f"  const _manifest = await {_agent(stage_prompt, label='stage', phase='stage', agent_type=agent_type, schema=_MANIFEST_SCHEMA)};"
    )

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

    review_text = "\n\n".join(_emit._unconst(block, _REVIEW_RESULT_NAMES) for _, block in review_blocks)
    b.append("  phase('review');")
    b.append("  if (!_halted) {\n" + review_text + "\n  }")
    b.append(
        "  return { arm: _gate.arm, sizing: _sizingRel, plan: _planRel, run_id: _runId, "
        f"manifest: {_lit(manifest_rel)}, rows: _manifest.rows.map((r) => r.id), "
        "incomplete: _incompleteChunks, blocked: _blockedChunks, unanswered: _unansweredBriefs, "
        "stopped_by: _stoppedBy, not_started: _notStarted, halted_by: _halted, "
        "review: { prep: _reviewPrep, wave: _reviewWave, delivery: _deliveryVerdict, "
        "integration: _reviewIntegration } };"
    )

    meta = _emit._meta_block(
        "warp-ask",
        "One in-session run from an ask to a reviewed result: size, gate, plan, stage, execute, review.",
        _phase_titles(with_size=not sizing_rel, blitz_phases=blitz_phases, review_titles=review_titles),
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
