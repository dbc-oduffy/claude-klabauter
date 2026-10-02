"""warp --ask roadmap arm: roadmap-blitz, plan-blitz waves, execute per plan, workstream-complete.

Composed in place of the single-plan arms when the sizing resolves to route `roadmap`. DoE's
workflows/roadmap-blitz.mjs is wrapped like plan-blitz into `async function roadmapBlitz(args)`;
waves come from the frozen gate report's engine-computed `waves`, never from agent judgment.
Pure composition: no spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.ops.dispatch_emit import emit as _emit
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL, OP_ASK_GATE, RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import (
    AskPlanBlitzRefused,
    _meta_span,
    _TITLE,
)

ROADMAP_STAGE_FN = "roadmapBlitz"
ROADMAP_BLITZ_SOURCE = ("workflows", "roadmap-blitz.mjs")

_PM_APPROVER, _APM_APPROVER = "pm", "apm"


def wrap_roadmap_stage(text: str) -> tuple[str, list[str]]:
    """(JS function text, phase titles): `async function roadmapBlitz(args)` over the file's body."""
    start, end = _meta_span(text)
    phases = [a or b for a, b in _TITLE.findall(text[start:end])]
    if not phases:
        raise AskPlanBlitzRefused("roadmap-blitz.mjs meta declares no phase titles")
    return f"async function {ROADMAP_STAGE_FN}(args) {{\n{text[:start] + text[end:]}\n}}\n", phases


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
) -> str:
    from coordinator_core.ops.dispatch_emit import ask_compose as ac

    wrap = wrap_roadmap or wrap_roadmap_stage
    rb_fn, rb_phases = wrap(roadmap_text if roadmap_text is not None else load_roadmap_blitz_text())
    if f"async function {ROADMAP_STAGE_FN}(" not in rb_fn:
        raise ac.AskComposeRefused(f"wrapped roadmap-blitz stage does not define async function {ROADMAP_STAGE_FN}")

    lit, agent, cat = ac._lit, ac._agent, ac._cat
    run_dir = f"{RUN_DIR_ROOT}/{run_id}"
    anchor = ac._ANCHOR_CLAUSE.format(root=repo_root)
    head = _emit._BRIEF_PRECEDENCE_CLAUSE
    agent_type = _emit._EXECUTOR_AGENT_TYPE
    approver = approver_for(interaction_mode)
    spread = f"...{json.dumps(plan_blitz_args, sort_keys=True)}, " if plan_blitz_args else ""
    halt = lambda reason: (  # noqa: E731
        f"_halted = {{ halted: {lit(HALT_REFUSAL)}, reason: {reason}, sizing: _sizingRel, run_id: _runId }};"
    )

    def prompt_of(*parts: str) -> str:
        return cat(f"{head}\n\n{anchor}\n\n", *parts)

    gate_prompt = prompt_of(
        f"Run `{ac._INVOKE} {OP_ASK_GATE} '",
        "js:JSON.stringify({ sizing_path: _sizingRel, writes: [] })",
        "'` and return its JSON reply verbatim as arm, halt and baton. Also read the sizing at ",
        "js:_sizingRel",
        " and return its estimate.tshirt as tshirt and its route as route.",
    )
    waves_prompt = prompt_of(
        "Read the frozen gate report at ",
        "js:_rb.gate_report_path",
        " and return its `waves` field verbatim: each wave's `index` and `batons` (id, path, title, "
        "sized, planPath, executionOpen, route exactly as recorded). The waves are engine-computed from file "
        "overlap: copy, never regroup, reorder or merge.",
    )
    exec_prompt = prompt_of(
        "Execute the approved plan at ",
        "js:planPath",
        " by following the execute-plan skill (`coordinator:execute-plan`) to completion, then return "
        "its terminal status as status.",
    )
    done_prompt = prompt_of(
        "Every approved plan of this roadmap has executed. Follow the workstream-complete skill "
        "(`coordinator:workstream-complete`) for the sizing at ",
        "js:_sizingRel",
        " and return its terminal status as status.",
    )
    exec_schema = ac._obj(["status"], {"status": ac._STR})
    waves_schema = ac._obj(
        ["waves"],
        {
            "waves": {
                "type": "array",
                "items": ac._obj(["index", "batons"], {"index": {"type": "integer"}, "batons": {"type": "array", "items": {"type": "object"}}}),
            }
        },
    )

    b: list[str] = [
        f"  const REPO_ROOT = {lit(repo_root)};",
        f"  const _runId = {lit(run_id)};",
        f"  let _sizingRel = {lit(sizing_rel)};",
        "  let _halted = null;",
        "  let _rb = null;",
        "  const _ready = [];",
        "  const _executed = [];",
        "  let _complete = null;",
        rb_fn,
        plan_blitz_fn,
        "  phase('gate');",
        f"  const _gate = await {agent(gate_prompt, label='gate', phase='gate', agent_type=agent_type, schema=ac._GATE_SCHEMA)};",
        "  if (_gate.halt || !_gate.arm) { _halted = { halted: (_gate.halt && _gate.halt.kind) || "
        f"{lit(HALT_REFUSAL)}, ..._gate.halt, sizing: _sizingRel, run_id: _runId }}; }}",
        f"  if (!_halted && _gate.arm !== {lit('roadmap')}) {{ {halt(lit('sizing resolved to a non-roadmap arm in the roadmap script'))} }}",
        "  if (!_halted) {",
        "  phase('roadmap-blitz');",
        f"  _rb = await {ROADMAP_STAGE_FN}({{ repoRoot: REPO_ROOT, sizing: _sizingRel, "
        f"interactionMode: {lit(str(interaction_mode))}, approver: {lit(approver)}, "
        f"trailDir: {lit(run_dir + '/roadmap-blitz')} }});",
        f"  if (!_rb || !_rb.gate_report_path) {{ {halt(chr(39) + 'roadmap-blitz returned no gate_report_path' + chr(39))} }}",
        "  }",
        "  if (!_halted) {",
        "  phase('plan');",
        f"  const _wm = await {agent(waves_prompt, label='waves', phase='plan', agent_type=agent_type, schema=waves_schema)};",
        "  const _waves = [...(_wm.waves ?? [])].sort((a, c) => a.index - c.index);",
        "  if (!_waves.length) { " + halt("'gate report carries no waves'") + " }",
        "  for (const w of _waves) {",
        "    if (_halted) break;",
        f"    const _pb = await {_plan_blitz_call(spread, run_dir)};",
        "    if ((_pb?.surfacedToPm ?? []).length) { "
        + halt("'plan-blitz surfaced items to the PM'") + " _halted.surfaced = _pb.surfacedToPm; break; }",
        "    for (const r of (_pb?.ready ?? [])) _ready.push({ wave: w.index, planPath: (r && typeof r === 'object') ? r.planPath : r });",
        "  }",
        "  if (!_halted && !_ready.length) { " + halt("'plan-blitz reported no ready plan'") + " }",
        "  }",
        "  if (!_halted) {",
        "  phase('execute');",
        "  const _byWave = [...new Set(_ready.map((x) => x.wave))].sort((a, c) => a - c);",
        "  for (const wi of _byWave) {",
        "    const _res = await Promise.all(_ready.filter((x) => x.wave === wi).map(async ({ planPath }) => ({ planPath, "
        f"...(await {agent(exec_prompt, label='execute', phase='execute', agent_type=agent_type, schema=exec_schema)}) }})));",
        "    _executed.push(..._res);",
        "  }",
        "  }",
        "  if (!_halted) {",
        "  phase('workstream-complete');",
        f"  _complete = await {agent(done_prompt, label='workstream-complete', phase='workstream-complete', agent_type=agent_type, schema=exec_schema)};",
        "  }",
        "  return { arm: 'roadmap', sizing: _sizingRel, run_id: _runId, roadmap: _rb, ready: _ready, "
        "executed: _executed, workstream_complete: _complete, halted_by: _halted };",
    ]
    titles = ["gate", "roadmap-blitz", *rb_phases, "plan", *plan_blitz_phases, "execute", "workstream-complete"]
    seen: list[str] = []
    titles = [t for t in titles if not (t in seen or seen.append(t))]
    meta = _emit._meta_block(
        "warp-ask-roadmap",
        "One in-session run from a roadmap sizing: roadmap-blitz, plan-blitz waves, execute, workstream-complete.",
        titles,
    )
    script = f"{_emit._NODE_CHECK_DOES_NOT_APPLY_COMMENT}\n{meta}\n" + "\n".join(b) + "\n"
    if len(script.encode("utf-8")) > _emit._WORKFLOW_SCRIPT_BYTE_CAP:
        raise ac.AskComposeRefused(
            f"composed ask script is over the Workflow runner's {_emit._WORKFLOW_SCRIPT_BYTE_CAP}-byte cap"
        )
    return script


def _plan_blitz_call(spread: str, run_dir: str) -> str:
    from coordinator_core.ops.dispatch_emit.ask_compose import _lit
    from coordinator_core.ops.dispatch_emit.ask_plan_blitz import STAGE_FN

    return (
        f"{STAGE_FN}({{ {spread}repoRoot: REPO_ROOT, waveIndex: w.index, "
        f"trailDir: {_lit(run_dir + '/blitz')} + '/wave-' + w.index, "
        "gateReportPath: _rb.gate_report_path, batons: w.batons })"
    )
