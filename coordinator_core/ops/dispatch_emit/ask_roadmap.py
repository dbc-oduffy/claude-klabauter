"""warp --ask roadmap arm: roadmap-blitz, plan-blitz waves, one record commit that fans the plans out.

Composed in place of the single-plan arms when the sizing resolves to route `roadmap`. DoE's
workflows/roadmap-blitz.mjs is wrapped like plan-blitz into `async function roadmapBlitz(args)`;
waves come from the stage reply's engine-computed `waves`, never from agent judgment. The run
executes nothing: its one commit lands the planning record and fires each ready plan as a sibling
run. Pure composition: no spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.ops.dispatch_emit import emit as _emit
from coordinator_core.ops.dispatch_emit.compose_size import cap_refusal, component_sizes
from coordinator_core.ops.dispatch_emit.fanout_cost import with_fanout
from coordinator_core.ops.dispatch_emit.halt_refire import refire_next_action_js
from coordinator_core.ops.dispatch_emit.stage_task_hijack import HIJACK_HALT_REASON, hijack_test_js
from coordinator_core.ops.dispatch_emit.ask_contract import (
    FANOUT_RECORD_PARAM,
    HALT_PLAN_FIELD,
    HALT_REFUSAL,
    OP_ASK_GATE,
    RUN_DIR_ROOT,
)
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import (
    INTERACTION_MODE_PARAM,
    NOT_FIRED_KEY,
    PARENT_SIZING_KEY,
    PLAN_BLITZ_UNFIRED_BUCKETS,
)
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME as LAND_RUN_OP
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import (
    STAGE_FN as PLAN_BLITZ_STAGE_FN,
    AskPlanBlitzRefused,
    _meta_span,
    _TITLE,
    strip_comment_lines,
)

STAGE_OP = "roadmap.blitz_stage"
ROADMAP_STAGE_FN = "roadmapBlitz"
ROADMAP_BLITZ_SOURCE = ("workflows", "roadmap-blitz.mjs")

_PM_APPROVER, _APM_APPROVER = "pm", "apm"


def wrap_roadmap_stage(text: str) -> tuple[str, list[str]]:
    """(JS function text, phase titles): `async function roadmapBlitz(args)` over the file's body."""
    start, end = _meta_span(text)
    phases = [a or b for a, b in _TITLE.findall(text[start:end])]
    if not phases:
        raise AskPlanBlitzRefused("roadmap-blitz.mjs meta declares no phase titles")
    fn = f"async function {ROADMAP_STAGE_FN}(args) {{\n{text[:start] + text[end:]}\n}}\n"
    return strip_comment_lines(fn), phases


def load_roadmap_blitz_text(plugin_root: Optional[str] = None) -> str:
    """Read workflows/roadmap-blitz.mjs under the resolved plugin root, or refuse by name."""
    if plugin_root is None:
        from coordinator_core.warm.caller_context import resolve_caller_context

        plugin_root = resolve_caller_context().plugin_root
    if not plugin_root:
        raise AskPlanBlitzRefused("plugin root did not resolve; roadmap-blitz.mjs is unreachable")
    source = Path(plugin_root).joinpath(*ROADMAP_BLITZ_SOURCE)
    if not source.is_file():
        raise AskPlanBlitzRefused(f"no roadmap-blitz.mjs at {source}")
    return source.read_text(encoding="utf-8")


def approver_for(interaction_mode: object) -> str:
    """Who clears the approval gates: hands-on asks the PM; pm, ceo and any delegation the APM."""
    return _PM_APPROVER if interaction_mode == "hands-on" else _APM_APPROVER


ROADMAP_DECLS = ("  let _rb = null;", "  const _ready = [];", "  const _unfired = [];", "  const _pulled = [];")

#: `stage` of a halt raised by the roadmap arm's plan-blitz waves.
PLAN_BLITZ_STAGE = "plan-blitz"

NO_READY_LITERAL ="plan-blitz reported no ready plan"


def no_ready_reason_js(pulled_js: str) -> str:
    """JS expression: the no-ready literal, then `: ` and the string `reason` of each entry of
    `pulled_js` joined by `; `; the bare literal when none carries one. When a pulled reason is a
    stage-task hijack (`hijack_test_js`), the expression is `stage-task-hijacked: ...: <that reason>`."""
    reasons = f"({pulled_js} ?? []).map((p) => p && p.reason).filter((r) => typeof r === 'string' && r)"
    hijacked = f"(r) => {hijack_test_js('r')}"
    plain = f"('{NO_READY_LITERAL}' + {reasons}.reduce((a, r) => a ? a + '; ' + r : ': ' + r, ''))"
    return (
        f"({reasons}.some({hijacked}) ? '{HIJACK_HALT_REASON}: plan-blitz agent declined its task for a relayed/user line: '"
        f" + {reasons}.find({hijacked}) : {plain})"
    )

#: The run's one commit: lands the planning record, then fans the ready plans out as sibling runs.
def roadmap_next_action_js(script_path: Optional[str] = None, interaction_mode: object = None) -> str:
    """The terminal-commit `_nextAction` literal. `params` is the replayable block: `run_id`, the
    emitted script's repo-relative `script_path` when known (omitted, never null), and the fan-out."""
    from coordinator_core.ops.dispatch_emit import ask_compose as ac

    path = f"script_path: {ac._lit(script_path)}, " if script_path else ""
    mode = f"{INTERACTION_MODE_PARAM}: {ac._lit(interaction_mode)}, " if isinstance(interaction_mode, str) and interaction_mode else ""
    return (
        f"{{ kind: 'terminal_commit', op: '{LAND_RUN_OP}', "
        f"params: {{ run_id: _runId, {path}{FANOUT_RECORD_PARAM}: {{ plans: _ready.map((r) => r.planPath), "
        "waves: Object.fromEntries(_ready.map((r) => [r.planPath, r.wave])), "
        f"{mode}{PARENT_SIZING_KEY}: _sizingRel, {NOT_FIRED_KEY}: _unfired }} }} }}"
    )


SEAM_FIX_PHASE = "seam-fix"
SEAM_FIX_OP = "plan.seam_fix"

_SEAM_FIX_SCHEMA = {
    "type": "object",
    "required": ["converged"],
    "properties": {
        "edges_added": {"type": "array"},
        "skipped_cycles": {"type": "array"},
        "skipped_plans": {"type": "array"},
        "converged": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
    },
}


def _seam_fix_lines(repo_root: str, run_id: str, halt: Callable[[str], str]) -> list[str]:
    """One `plan.seam_fix` relay over the distinct ready plan paths (wave order, then plan-blitz order,
    which is the blocked_by order the waves were computed from); skipped when fewer than two plans
    could collide. A refusal, a cycle, a skipped plan or non-convergence halts with the reason."""
    from coordinator_core.ops.dispatch_emit import ask_compose as ac

    head = _emit._BRIEF_PRECEDENCE_CLAUSE
    anchor = ac._ANCHOR_CLAUSE.format(root=repo_root)
    prompt = ac._cat(
        f"{head}\n\n{anchor}\n\n",
        f"Run `{ac._INVOKE} {SEAM_FIX_OP} '",
        "js:JSON.stringify({ plans: _sfPlans, order: _sfPlans, dry_run: false })",
        f"'{ac._repo_flag(repo_root)}` once and return its JSON reply verbatim "
        '(or {"error": ...} when it refuses). Commit nothing.',
    )
    call = ac._agent(
        prompt, label="seam-fix", phase=SEAM_FIX_PHASE, agent_type=ac._OP_RELAY_AGENT_TYPE,
        agent_model=ac._MECHANICAL_MODEL, schema=_SEAM_FIX_SCHEMA, effort=ac._MECHANICAL_EFFORT,
        run_id=run_id,
    )
    why = (
        "'plan.seam_fix over the ready plans: ' + (_sf?.error ? 'refused: ' + _sf.error"
        " : (_sf?.skipped_cycles ?? []).length ? 'cycle ' + JSON.stringify(_sf.skipped_cycles)"
        " : (_sf?.skipped_plans ?? []).length ? 'plan not written ' + JSON.stringify(_sf.skipped_plans)"
        " : 'collisions remain after the fix')"
    )
    return [
        "  const _sfPlans = [...new Set(_ready.map((r) => r.planPath).filter((p) => typeof p === 'string' && p))];",
        "  if (!_halted && _sfPlans.length > 1) {",
        f"    phase({ac._lit(SEAM_FIX_PHASE)});",
        f"    const _sf = await {call};",
        "    if (!_sf || _sf.error || (_sf.skipped_cycles ?? []).length || (_sf.skipped_plans ?? []).length || _sf.converged !== true) { "
        + halt(why) + " }",
        "  }",
    ]


READ_GATE_REPORT_PARAM = "read_gate_report"

_FROZEN_WAVES_SCHEMA = {
    "type": "object",
    "properties": {"waves": {"type": "array"}, "error": {"type": ["string", "null"]}},
}


def _frozen_waves_lines(repo_root: str, run_id: str) -> list[str]:
    """When the stage reply (an agent's relay of the op) lost its waves, one `roadmap.blitz_stage`
    read of the frozen gate report at `_rb.gate_report_path` refills `_waves` from the engine's own
    computation; skipped when the reply already carried them."""
    from coordinator_core.ops.dispatch_emit import ask_compose as ac

    prompt = ac._cat(
        f"{_emit._BRIEF_PRECEDENCE_CLAUSE}\n\n{ac._ANCHOR_CLAUSE.format(root=repo_root)}\n\n",
        f"Run `{ac._INVOKE} {STAGE_OP} '",
        f"js:JSON.stringify({{ {READ_GATE_REPORT_PARAM}: _rb.gate_report_path }})",
        f"'{ac._repo_flag(repo_root)}` once and return its JSON reply verbatim "
        '(or {"error": ...} when it refuses). Commit nothing.',
    )
    call = ac._agent(
        prompt, label="frozen-waves", phase="roadmap-blitz", agent_type=ac._OP_RELAY_AGENT_TYPE,
        agent_model=ac._MECHANICAL_MODEL, schema=_FROZEN_WAVES_SCHEMA, effort=ac._MECHANICAL_EFFORT,
        run_id=run_id,
    )
    return [
        "  if (!_waves.length && _rb?.gate_report_path) {",
        f"    const _fw = await {call};",
        "    _waves = [...(_fw?.waves ?? [])].sort((a, c) => a.index - c.index);",
        "  }",
    ]


READ_STAGED_PARAM = "read_staged"

_STAGED_SCHEMA = {
    "type": "object",
    "properties": {
        "gate_report_path": {"type": ["string", "null"]},
        "approval_record": {"type": ["object", "null"]},
        "waves": {"type": "array"},
        "error": {"type": ["string", "null"]},
    },
}


def _staged_from_disk_lines(repo_root: str, run_id: str, trail_dir: str) -> list[str]:
    """When the stage reply lost its `gate_report_path` (the stage agent's relay of the op was
    truncated or refused), one read-only `roadmap.blitz_stage` call re-reads the frozen gate report
    and `approval.json` from disk. `_rb` is rebuilt only when BOTH exist; otherwise it is left
    untouched and the halt that follows stands."""
    from coordinator_core.ops.dispatch_emit import ask_compose as ac

    prompt = ac._cat(
        f"{_emit._BRIEF_PRECEDENCE_CLAUSE}\n\n{ac._ANCHOR_CLAUSE.format(root=repo_root)}\n\n",
        f"Run `{ac._INVOKE} {STAGE_OP} '",
        f"js:JSON.stringify({{ {READ_STAGED_PARAM}: {{ roadmap_id: _rb.roadmapId, trail_dir: {ac._lit(trail_dir)} }} }})",
        f"'{ac._repo_flag(repo_root)}` once and return its JSON reply verbatim "
        '(or {"error": ...} when it refuses). Commit nothing.',
    )
    call = ac._agent(
        prompt, label="staged-from-disk", phase="roadmap-blitz", agent_type=ac._OP_RELAY_AGENT_TYPE,
        agent_model=ac._MECHANICAL_MODEL, schema=_STAGED_SCHEMA, effort=ac._MECHANICAL_EFFORT,
        run_id=run_id,
    )
    return [
        "  if (!_rb?.gate_report_path && typeof _rb?.roadmapId === 'string' && _rb.roadmapId) {",
        f"    const _disk = await {call};",
        "    if (_disk?.gate_report_path && _disk?.approval_record) {",
        "      _rb = { ..._rb, gate_report_path: _disk.gate_report_path, stage: { ...(_rb.stage ?? {}), "
        "gate_report_path: _disk.gate_report_path, approval_record: _disk.approval_record, waves: _disk.waves ?? [] } };",
        "    }",
        "  }",
    ]


def roadmap_stage_lines(
    *,
    repo_root: str,
    run_id: str,
    interaction_mode: object,
    plan_blitz_args: Optional[dict],
    script_path: Optional[str] = None,
) -> list[str]:
    """roadmap-blitz, then plan-blitz per wave, each block under `if (!_halted)`; the last sets
    `_nextAction` to the fan-out commit. Waves come from the stage reply, never an agent. Reads
    `REPO_ROOT`, `_sizingRel`, `_runId`, `_halted`, `_nextAction` and the `ROADMAP_DECLS` bindings;
    the caller embeds both stage functions and fires the commit."""
    from coordinator_core.ops.dispatch_emit import ask_compose as ac

    lit = ac._lit
    run_dir = f"{RUN_DIR_ROOT}/{run_id}"
    approver = approver_for(interaction_mode)
    spread = f"...{json.dumps(plan_blitz_args, sort_keys=True)}, " if plan_blitz_args else ""
    halt = lambda reason: (  # noqa: E731
        f"_halted = {{ halted: {lit(HALT_REFUSAL)}, reason: {reason}, sizing: _sizingRel, run_id: _runId }};"
    )
    plan_halt = lambda reason: (  # noqa: E731
        f"_halted = {{ halted: {lit(HALT_REFUSAL)}, reason: {reason}, sizing: _sizingRel, run_id: _runId, "
        f"{HALT_PLAN_FIELD}: (_pulled.map(_pth).find(Boolean) ?? null), stage: {lit(PLAN_BLITZ_STAGE)} }};"
    )
    chatty_keys = ""
    if plan_blitz_args and plan_blitz_args.get("chatty") is True and plan_blitz_args.get("chattyReview"):
        chatty_keys = (
            f"chatty: true, chattyReview: {json.dumps(plan_blitz_args['chattyReview'], sort_keys=True)}, "
        )

    return [
        "  if (!_halted) {",
        "  phase('roadmap-blitz');",
        f"  _rb = await {ROADMAP_STAGE_FN}({{ repoRoot: REPO_ROOT, sizing: _sizingRel, "
        f"interactionMode: {lit(str(interaction_mode))}, approver: {lit(approver)}, {chatty_keys}"
        f"trailDir: {lit(run_dir + '/roadmap-blitz')} }});",
        *_staged_from_disk_lines(repo_root, run_id, run_dir + '/roadmap-blitz'),
        f"  if (!_rb || !_rb.gate_report_path) {{ {halt(chr(39) + 'roadmap-blitz returned no gate_report_path' + chr(39))} }}",
        "  }",
        "  if (!_halted) {",
        "  let _waves = [...(_rb?.stage?.waves ?? [])].sort((a, c) => a.index - c.index);",
        *_frozen_waves_lines(repo_root, run_id),
        "  if (!_waves.length) { " + halt("'roadmap-blitz stage reply carries no waves'") + " }",
        "  let _ran = 0;",
        "  const _bid = (x) => (x && typeof x === 'object') ? (x.batonId ?? x.baton ?? x.id ?? null) : x;",
        "  const _pth = (x) => (x && typeof x === 'object') ? (x.planPath ?? x.path ?? null) : null;",
        "  for (const w of _waves) {",
        "    if (_halted) break;",
        f"    const _pb = await {_plan_blitz_call(spread, run_dir)};",
        "    _ran++;",
        "    const _seen = new Set();",
        "    for (const r of (_pb?.ready ?? [])) { _seen.add(_bid(r)); _seen.add(_pth(r)); _seen.add(r); }",
        f"    for (const [_k, _why] of {json.dumps([list(p) for p in PLAN_BLITZ_UNFIRED_BUCKETS])}) {{",
        "      for (const e of (_pb?.[_k] ?? [])) {",
        "        const _b = _bid(e), _p = _pth(e) ?? ((e && typeof e === 'object') ? null : e);",
        "        _seen.add(_b); _seen.add(_p);",
        "        _unfired.push({ wave: w.index, ...(_b ? { baton: _b } : {}), ...(_p ? { plan: _p } : {}), reason: _why, "
        "...((e && e.reason) ? { detail: String(e.reason) } : {}) });",
        "      }",
        "    }",
        "    for (const b of (w.batons ?? [])) {",
        "      const _b = _bid(b), _p = _pth(b);",
        "      if (_seen.has(_b) || (_p && _seen.has(_p))) continue;",
        "      _unfired.push({ wave: w.index, ...(_b ? { baton: String(_b) } : {}), ...(_p ? { plan: _p } : {}), reason: 'absent-from-wave-reply' });",
        "    }",
        "    if ((_pb?.surfacedToPm ?? []).length) { "
        + plan_halt("'plan-blitz surfaced items to the PM'") + " _halted.surfaced = _pb.surfacedToPm; break; }",
        "    for (const r of (_pb?.ready ?? [])) _ready.push({ wave: w.index, planPath: (r && typeof r === 'object') ? r.planPath : r });",
        "    for (const p of (_pb?.pulled ?? [])) _pulled.push(p);",
        "  }",
        "  for (const w of _waves.slice(_ran)) {",
        "    for (const b of (w.batons ?? [])) {",
        "      const _b = _bid(b), _p = _pth(b);",
        "      _unfired.push({ wave: w.index, ...(_b ? { baton: String(_b) } : {}), ...(_p ? { plan: _p } : {}), reason: 'wave-not-run' });",
        "    }",
        "  }",
        "  if (!_halted && !_ready.length) { " + plan_halt(no_ready_reason_js("_pulled")) + " }",
        "  }",
        *_seam_fix_lines(repo_root, run_id, halt),
        f"  if (!_halted) {{ _nextAction = {roadmap_next_action_js(script_path, interaction_mode)}; }}",
    ]


def compose_roadmap_script(
    *,
    repo_root: str,
    sizing_rel: str,
    run_id: str,
    interaction_mode: object,
    roadmap_text: Optional[str],
    plan_blitz_fn: str,
    plan_blitz_phases: list[str],
    plan_blitz_args: Optional[dict],
    wrap_roadmap: Optional[Callable[[str], tuple[str, list[str]]]] = None,
    script_path: Optional[str] = None,
) -> str:
    from coordinator_core.ops.dispatch_emit import ask_compose as ac

    wrap = wrap_roadmap or wrap_roadmap_stage
    rb_fn, rb_phases = wrap(roadmap_text if roadmap_text is not None else load_roadmap_blitz_text())
    if f"async function {ROADMAP_STAGE_FN}(" not in rb_fn:
        raise ac.AskComposeRefused(f"wrapped roadmap-blitz stage does not define async function {ROADMAP_STAGE_FN}")

    lit, agent, cat = ac._lit, ac._agent, ac._cat
    anchor = ac._ANCHOR_CLAUSE.format(root=repo_root)
    head = _emit._BRIEF_PRECEDENCE_CLAUSE
    halt = lambda reason: (  # noqa: E731
        f"_halted = {{ halted: {lit(HALT_REFUSAL)}, reason: {reason}, sizing: _sizingRel, run_id: _runId }};"
    )
    gate_prompt = cat(
        f"{head}\n\n{anchor}\n\n",
        f"Run `{ac._INVOKE} {OP_ASK_GATE} '",
        "js:JSON.stringify({ sizing_path: _sizingRel, writes: [] })",
        f"'{ac._repo_flag(repo_root)}` and return its JSON reply verbatim as arm, halt and baton. Also read the sizing at ",
        "js:_sizingRel",
        " and return its estimate.tshirt as tshirt and its route as route.",
    )

    b: list[str] = [
        f"  const REPO_ROOT = {lit(repo_root)};",
        f"  const _runId = {lit(run_id)};",
        f"  let _sizingRel = {lit(sizing_rel)};",
        "  let _halted = null;",
        refire_next_action_js(
            call_js=ac._refire_js(None),
            legacy_js="_halted.next_action",
            kind_js="_halted.halted",
            detail_js="(_legacy.detail ?? null)",
        ),
        *ROADMAP_DECLS,
        *ac.roadmap_commit_decls(head=head, anchor=anchor, repo_root=repo_root, run_id=run_id),
        "  let _haltCommit = null;",
        "  let _planRel = null;",
        "  let _testResult = null;",
        "  let _docs = null;",
        "  let _consumedReturns = {};",
        ac._halt_record_commit_fn(
            head=head, anchor=anchor, script_path=script_path, repo_root=repo_root, run_id=run_id,
        ),
        ac.brief_clause_decl_js(),
        *_shadowed_stage(ROADMAP_STAGE_FN, rb_fn),
        *(_shadowed_stage(PLAN_BLITZ_STAGE_FN, plan_blitz_fn) if plan_blitz_fn else []),
        "  phase('gate');",
        f"  const _gate = await {agent(gate_prompt, label='gate', phase='gate', agent_type=ac._MECHANICAL_AGENT_TYPE, agent_model=ac._MECHANICAL_MODEL, effort=ac._MECHANICAL_EFFORT, schema=ac._GATE_SCHEMA, run_id=run_id)};",
        "  if (_gate.halt || !_gate.arm) { _halted = { halted: (_gate.halt && _gate.halt.kind) || "
        f"{lit(HALT_REFUSAL)}, ..._gate.halt, reason: String((_gate.halt && _gate.halt.reason) || "
        "(_gate.halt ? 'the gate halted (' + _gate.halt.kind + ') without a reason' : "
        "'the gate returned no arm and no halt; refire to re-run the gate: ' + "
        f"{ac._refire_js(None)})), sizing: _sizingRel, run_id: _runId }}; }}",
        f"  if (!_halted && _gate.arm !== {lit('roadmap')}) {{ {halt(lit('sizing resolved to a non-roadmap arm in the roadmap script'))} }}",
        *roadmap_stage_lines(
            repo_root=repo_root, run_id=run_id, interaction_mode=interaction_mode,
            plan_blitz_args=plan_blitz_args, script_path=script_path,
        ),
        *ac.roadmap_commit_lines(
            head=head, anchor=anchor, repo_root=repo_root, run_id=run_id, script_path=script_path
        ),
    ]
    ac._append_halt_record_commit(b)
    b += [
        "  _refireNextAction();",
        "  return { arm: 'roadmap', sizing: _sizingRel, run_id: _runId, roadmap: _rb, ready: _ready, "
        f"{NOT_FIRED_KEY}: _commit?.fanout?.{NOT_FIRED_KEY} ?? _unfired, "
        "commit: _commit, fanout: _commit?.fanout ?? null, next_action: _nextAction, halted_by: _halted };",
    ]
    titles = roadmap_phase_titles(rb_phases, plan_blitz_phases)
    meta = with_fanout(
        _emit._meta_block(
            "warp-ask-roadmap",
            "One in-session run from a roadmap sizing: roadmap-blitz, plan-blitz waves, one record commit that fans the plans out.",
            titles,
        ),
        ac.composed_fanout(b),
    )
    script = f"{_emit._NODE_CHECK_DOES_NOT_APPLY_COMMENT}\n{meta}\n" + "\n".join(b) + "\n"
    total = len(script.encode("utf-8"))
    if total > _emit._WORKFLOW_SCRIPT_BYTE_CAP:
        sizes = component_sizes({"roadmap_blitz": rb_fn, "plan_blitz": plan_blitz_fn, "meta": meta})
        sizes.append(("remainder", max(0, total - sum(n for _, n in sizes))))
        raise ac.AskComposeRefused(cap_refusal(total, _emit._WORKFLOW_SCRIPT_BYTE_CAP, sizes))
    return script


def _shadowed_stage(name: str, fn_text: str) -> list[str]:
    from coordinator_core.ops.dispatch_emit.ask_compose import clause_shadow_parts

    opened, closed = clause_shadow_parts(name)
    return [opened, fn_text, closed]


def roadmap_phase_titles(rb_phases: list[str], plan_blitz_phases: list[str]) -> list[str]:
    titles = ["gate", "roadmap-blitz", *rb_phases, *plan_blitz_phases, SEAM_FIX_PHASE, "commit"]
    seen: list[str] = []
    return [t for t in titles if not (t in seen or seen.append(t))]


def _plan_blitz_call(spread: str, run_dir: str) -> str:
    from coordinator_core.ops.dispatch_emit.ask_compose import _lit
    from coordinator_core.ops.dispatch_emit.ask_plan_blitz import STAGE_FN

    return (
        f"{STAGE_FN}({{ {spread}repoRoot: REPO_ROOT, waveIndex: w.index, "
        f"trailDir: {_lit(run_dir + '/blitz')} + '/wave-' + w.index, "
        "gateReportPath: _rb.gate_report_path, batons: w.batons })"
    )
