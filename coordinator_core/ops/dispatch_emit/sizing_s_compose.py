"""Stage 1 of an accepted S sizing: author the plan, then fire stage 2 from it.

`compose_s_stage` returns one `.mjs` text with two phases (`sizing_fire.S_STAGE1_PHASES`):
`plan-author` writes `docs/plans/<sizing-stem>.md`; `fire-execute` runs `plan-spine-check` and
then `emit-dispatch-workflow --plan <plan> --fire`, which emits and fires stage 2 (executors,
review wave, terminal commit). The terminal-commit marker declares the plan document as this
run's sole write, so the driver's relay commits it once stage 1 ends.

No review wave is composed here: stage 1 lands a plan document, not code. Stage 2 carries it.

Ordering: `fire-execute` fires stage 2 while the plan document is still uncommitted. Read at
head, `--plan` emit and `fire_workflow` do no git state check on the plan (emit records only
`head_sha`, never plan cleanliness), so neither refuses an uncommitted plan or records dirty
provenance. Stage 2's terminal-commit pathspec is the union of its rows' declared `writes:`;
the plan document is swept in only if a spine row lists it, which the plan-author prompt forbids.
So stage 1's relay commits the plan and stage 2's commit lands the code, as separate commits.
"""

from __future__ import annotations

from typing import Mapping, Optional

from coordinator_core.ops.dispatch_emit.emit import (
    _BRIEF_PRECEDENCE_CLAUSE,
    _NODE_CHECK_DOES_NOT_APPLY_COMMENT,
    _dispatching_session_id_paragraph,
    _escape_for_js_template_literal,
    _meta_block,
    _terminal_commit_marker,
)
from coordinator_core.ops.dispatch_emit.sizing_fire import S_STAGE1_PHASES, s_plan_path
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.workflow_scaffold import _js_string_literal

__all__ = ["compose_s_stage"]

_SETTINGS_HOME = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}"'


def _plan_author_prompt(sizing: Mapping, sizing_rel: str, plan_rel: str) -> str:
    ec = sizing.get("exit_criterion")
    statement = ec.get("statement", "") if isinstance(ec, Mapping) else ""
    return (
        f"{_BRIEF_PRECEDENCE_CLAUSE}\n\n"
        f"Author the plan for the accepted S sizing {sizing_rel}.\n\n"
        f"Intent: {sizing.get('intent', '')}\n\n"
        f"Exit criterion (verbatim): {statement}\n\n"
        f"Interaction mode: {sizing.get('interaction_mode', '')}\n\n"
        f"1. Scaffold the plan at {plan_rel}: run `{_SETTINGS_HOME}/bin/coordinator-doc-new "
        f"--type plan --sizing-object {sizing_rel} --out {plan_rel}`.\n"
        f"2. Fill it with `scope_mode: spec-dispatch` and a four-part body: problem, file scope, "
        f"acceptance criteria, test surface.\n"
        f"3. Add a `yaml plan-tasks` spine under `## Tasks` with `writes:` on every row. No row "
        f"lists {plan_rel} itself.\n"
        f"Write only {plan_rel}. Do not commit or stage."
    )


def _fire_execute_prompt(plan_rel: str) -> str:
    launcher = f"{_SETTINGS_HOME}/bin"
    return (
        f"{_BRIEF_PRECEDENCE_CLAUSE}\n\n"
        f"Fire stage 2 from the plan {plan_rel}.\n"
        f"1. Run `{launcher}/plan-spine-check {plan_rel}`. On any STRUCTURAL finding, stop and "
        f"report it; fire nothing.\n"
        f"2. Otherwise run `{launcher}/emit-dispatch-workflow --plan {plan_rel} --fire` from the "
        f"repo root you are standing in.\n"
        f"Report the fire record. Never run emit-dispatch-workflow with `--sizing`. Do not "
        f"commit or stage."
    )


def _agent_call(prompt: str, label: str, phase: str, agent_type: str, model: str) -> str:
    return (
        "  await agent(\n"
        f"    `{_escape_for_js_template_literal(prompt)}`,\n"
        f"    {{ label: {_js_string_literal(label)}, phase: {_js_string_literal(phase)}, "
        f"agentType: {_js_string_literal(agent_type)}, model: {_js_string_literal(model)} }}\n"
        "  )"
    )


def compose_s_stage(
    sizing: Mapping,
    *,
    sizing_rel: str,
    repo_root: str,
    session_id: Optional[str] = None,
) -> str:
    """The stage-1 `.mjs` text for an accepted S `sizing` read from `sizing_rel`."""
    plan_rel = s_plan_path(sizing_rel)
    plan_phase, fire_phase, _commit_phase = S_STAGE1_PHASES
    row = WaveRow(
        id="plan-author",
        title=f"Plan document for {sizing_rel}",
        surface=plan_rel,
        writes=[plan_rel],
        reads=[],
        depends_on=[],
    )
    marker = _terminal_commit_marker(
        [row],
        {row.id: [plan_rel]},
        plan_path=plan_rel,
        deliverable_id=None,
        session_id=session_id,
        repo_root=repo_root,
    )
    author_prompt = _plan_author_prompt(sizing, sizing_rel, plan_rel)
    author_prompt += _dispatching_session_id_paragraph(session_id)
    body = [
        f"  phase({_js_string_literal(plan_phase)});",
        "  const _planAuthored = " + _agent_call(
            author_prompt, plan_phase, plan_phase, "coordinator:plan-author", "opus"
        ).lstrip() + ";",
        f"  phase({_js_string_literal(fire_phase)});",
        "  const _fired = " + _agent_call(
            _fire_execute_prompt(plan_rel), fire_phase, fire_phase,
            "coordinator:executor", "sonnet",
        ).lstrip() + ";",
    ]
    if marker is not None:
        body.append(marker)
    body.append("  return { planAuthored: _planAuthored, fired: _fired };")
    meta = _meta_block(
        f"s-stage1-{plan_rel.rsplit('/', 1)[-1][:-3]}",
        f"Author {plan_rel}, then fire stage 2 from it",
        list(S_STAGE1_PHASES),
    )
    return f"{_NODE_CHECK_DOES_NOT_APPLY_COMMENT}\n{meta}\n" + "\n\n".join(body) + "\n"
