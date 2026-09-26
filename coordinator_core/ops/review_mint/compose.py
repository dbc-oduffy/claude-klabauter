
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from coordinator_core.ops.review_mint.roster import Stage
from coordinator_core.ops.workflow_scaffold import _js_string_literal

# prompt -- see `_GATE_SCHEMA_LITERAL`'s pairing with `compose()`'s
_GATE_SCHEMA_LITERAL = (
    "{ type: 'object', properties: { "
    "verdict: { type: 'string' }, "
    "reason: { type: 'string' }, "
    "sidecar_path: { type: 'string' }, "
    "run_nonce: { type: 'string' } "
    "}, required: ['verdict', 'run_nonce'] }"
)

GatePolicy = Callable[[Stage, int, List[Tuple[str, str]]], str]


class ComposeError(ValueError):
    pass


def _stage_phase_title(base_title: str, index: int, total: int) -> str:
    if total == 1:
        return base_title
    return f"{base_title} {index + 1}/{total}"


def _result_var(index: int) -> str:
    return f"reviewStage{index}Result"


def _agent_call_literal(
    agent_type: str,
    prompt: str,
    phase_title: str,
    *,
    schema: bool,
    as_arrow: bool,
) -> str:
    """Render one `agent(...)` call. `as_arrow` wraps it as `() => agent(...)`
    for use inside `parallel([...])`; NEGATIVE SPEC: never a `model:` key
    (see module docstring)."""
    opts = (
        "{ "
        f"label: {_js_string_literal(f'review:{agent_type}')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(agent_type)}"
    )
    if schema:
        opts += f", schema: {_GATE_SCHEMA_LITERAL}"
    opts += " }"

    call = f"agent({_js_string_literal(prompt)}, {opts})"
    return f"() => {call}" if as_arrow else call


def _composed_prompt(prompt: str, run_nonce: Optional[str]) -> str:
    return f"{prompt}\n\nrun_nonce: {run_nonce}"


def _compose_stage(
    stage: Stage,
    index: int,
    total: int,
    prompt: str,
    base_phase_title: str,
    gate_policy: GatePolicy,
    run_nonce: Optional[str] = None,
) -> Tuple[str, str]:
    phase_title = _stage_phase_title(base_phase_title, index, total)
    lines = [f"  phase({_js_string_literal(phase_title)});"]

    if not stage.gate:
        if len(stage.agents) == 1:
            call = _agent_call_literal(
                stage.agents[0], prompt, phase_title, schema=False, as_arrow=False
            )
            lines.append(f"  await {call};")
        else:
            item_calls = ",\n".join(
                "    "
                + _agent_call_literal(agent, prompt, phase_title, schema=False, as_arrow=True)
                for agent in stage.agents
            )
            lines.append(f"  await parallel([\n{item_calls}\n  ]);")
        return phase_title, "\n".join(lines)

    gate_prompt = _composed_prompt(prompt, run_nonce) if run_nonce is not None else prompt

    var = _result_var(index)
    if len(stage.agents) == 1:
        agent = stage.agents[0]
        call = _agent_call_literal(agent, gate_prompt, phase_title, schema=True, as_arrow=False)
        lines.append(f"  const {var} = await {call};")
        results = [(agent, var)]
    else:
        item_calls = ",\n".join(
            "    "
            + _agent_call_literal(agent, gate_prompt, phase_title, schema=True, as_arrow=True)
            for agent in stage.agents
        )
        lines.append(f"  const {var} = await parallel([\n{item_calls}\n  ]);")
        results = [(agent, f"{var}[{i}]") for i, agent in enumerate(stage.agents)]

    branch = gate_policy(stage, index, results)
    if branch:
        lines.append(branch)

    return phase_title, "\n".join(lines)


def compose(
    stages: List[Stage],
    prompt: str,
    phase_title: str,
    gate_policy: GatePolicy,
    run_nonce: Optional[str] = None,
) -> List[Tuple[str, str]]:
    if not stages:
        raise ComposeError("compose() received an empty stage list")

    total = len(stages)
    return [
        _compose_stage(stage, index, total, prompt, phase_title, gate_policy, run_nonce)
        for index, stage in enumerate(stages)
    ]
