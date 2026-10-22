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
from coordinator_core.ops.dispatch_emit.fanout_cost import with_fanout
from coordinator_core.ops.dispatch_emit.ask_contract import (
    FANOUT_RECORD_PARAM,
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


ROADMAP_DECLS = ("  let _rb = null;", "  const _ready = [];", "  const _unfired = [];", "  const _pulled = [];")

NO_READY_LITERAL = "plan-blitz reported no ready plan"


def no_ready_reason_js(pulled_js: str) -> str:
    """JS expression: the no-ready literal, then `: ` and the string `reason` of each entry of
    `pulled_js` joined by `; `; the bare literal when none carries one."""
    return (
        f"('{NO_READY_LITERAL}' + (({pulled_js} ?? []).map((p) => p && p.reason).filter((r) => typeof r === 'string' && r)"
        ".reduce((a, r) => a ? a + '; ' + r : ': ' + r, '')))"
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

    return [
        "  if (!_halted) {",
        "  phase('roadmap-blitz');",
        f"  _rb = await {ROADMAP_STAGE_FN}({{ repoRoot: REPO_ROOT, sizing: _sizingRel, "
        f"interactionMode: {lit(str(interaction_mode))}, approver: {lit(approver)}, "
        f"trailDir: {lit(run_dir + '/roadmap-blitz')} }});",
        f"  if (!_rb || !_rb.gate_report_path) {{ {halt(chr(39) + 'roadmap-blitz returned no gate_report_path' + chr(39))} }}",
        "  }",
        "  if (!_halted) {",
        "  const _waves = [...(_rb?.stage?.waves ?? [])].sort((a, c) => a.index - c.index);",
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
        + halt("'plan-blitz surfaced items to the PM'") + " _halted.surfaced = _pb.surfacedToPm; break; }",
        "    for (const r of (_pb?.ready ?? [])) _ready.push({ wave: w.index, planPath: (r && typeof r === 'object') ? r.planPath : r });",
        "    for (const p of (_pb?.pulled ?? [])) _pulled.push(p);",
        "  }",
        "  for (const w of _waves.slice(_ran)) {",
        "    for (const b of (w.batons ?? [])) {",
        "      const _b = _bid(b), _p = _pth(b);",
        "      _unfired.push({ wave: w.index, ...(_b ? { baton: String(_b) } : {}), ...(_p ? { plan: _p } : {}), reason: 'wave-not-run' });",
        "    }",
        "  }",
        "  if (!_halted && !_ready.length) { " + halt(no_ready_reason_js("_pulled")) + " }",
        "  }",
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
        "'` and return its JSON reply verbatim as arm, halt and baton. Also read the sizing at ",
        "js:_sizingRel",
        " and return its estimate.tshirt as tshirt and its route as route.",
    )

    b: list[str] = [
        f"  const REPO_ROOT = {lit(repo_root)};",
        f"  const _runId = {lit(run_id)};",
        f"  let _sizingRel = {lit(sizing_rel)};",
        "  let _halted = null;",
        *ROADMAP_DECLS,
        *ac.roadmap_commit_decls(head=head, anchor=anchor, repo_root=repo_root, run_id=run_id),
        rb_fn,
        plan_blitz_fn,
        "  phase('gate');",
        f"  const _gate = await {agent(gate_prompt, label='gate', phase='gate', agent_type=ac._MECHANICAL_AGENT_TYPE, agent_model=ac._MECHANICAL_MODEL, effort=ac._MECHANICAL_EFFORT, schema=ac._GATE_SCHEMA, run_id=run_id)};",
        "  if (_gate.halt || !_gate.arm) { _halted = { halted: (_gate.halt && _gate.halt.kind) || "
        f"{lit(HALT_REFUSAL)}, ..._gate.halt, reason: String((_gate.halt && _gate.halt.reason) || "
        "(_gate.halt ? 'the gate halted (' + _gate.halt.kind + ') without a reason' : "
        "'the gate returned no arm and no halt')), sizing: _sizingRel, run_id: _runId }; }",
        f"  if (!_halted && _gate.arm !== {lit('roadmap')}) {{ {halt(lit('sizing resolved to a non-roadmap arm in the roadmap script'))} }}",
        *roadmap_stage_lines(
            repo_root=repo_root, run_id=run_id, interaction_mode=interaction_mode,
            plan_blitz_args=plan_blitz_args, script_path=script_path,
        ),
        *ac.roadmap_commit_lines(
            head=head, anchor=anchor, repo_root=repo_root, run_id=run_id, script_path=script_path
        ),
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
    if len(script.encode("utf-8")) > _emit._WORKFLOW_SCRIPT_BYTE_CAP:
        raise ac.AskComposeRefused(
            f"composed ask script is over the Workflow runner's {_emit._WORKFLOW_SCRIPT_BYTE_CAP}-byte cap"
        )
    return script


def roadmap_phase_titles(rb_phases: list[str], plan_blitz_phases: list[str]) -> list[str]:
    titles = ["gate", "roadmap-blitz", *rb_phases, *plan_blitz_phases, "commit"]
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
