
"""Composes a review-stage roster into the JS call sequence (`agent()`/`parallel()` literals) a generated workflow script executes, threading the run_nonce/gate schema through gated stages only."""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from coordinator_core.ops.review_mint.roster import Stage
from coordinator_core.ops.workflow_scaffold import _js_string_literal

# AC5's exact abort-object field names -- also the structured-output schema
# field names stamped on every gated agent call, so a `gate_policy` closure
# can read them straight off the captured result (`result.verdict`,
# `result.reason`, `result.sidecar_path`) with no renaming step.
#
# AC12: `run_nonce` joins the schema (and `required`, alongside `verdict`) so
# a gated agent's structured output must echo the value injected into its own
# prompt -- see `_GATE_SCHEMA_LITERAL`'s pairing with `compose()`'s
# `run_nonce` param below. The field name is fixed by the cross-repo charter
# contract (coordinator-claude's `sidecar-emission-contract.md` /
# `sidecar-frontmatter-contract.md`) -- do not rename it.
#
# NOT caller-symmetric, deliberately: `required` applies to every `schema:
# true` gate-stage call this schema literal is stamped on, including
# `dispatch.emit`'s (C4) -- which never injects a `run_nonce` into the
# prompt (`compose()` is called there with `run_nonce=None`) and whose own
# `gate_policy` never reads `.run_nonce` off the captured result (disarmed,
# see `op.py`'s `_make_gate_policy` docstring). An uninstructed agent still
# has to supply SOME string for the required field, but nothing ever
# compares it against anything, so this is inert by construction for that
# caller -- not a bug, and not a claim that the schema and the injection are
# symmetric across both `compose()` callers.
_GATE_SCHEMA_LITERAL = (
    "{ type: 'object', properties: { "
    "verdict: { type: 'string' }, "
    "reason: { type: 'string' }, "
    "sidecar_path: { type: 'string' }, "
    "run_nonce: { type: 'string' } "
    "}, required: ['verdict', 'run_nonce'] }"
)

GatePolicy = Callable[[Stage, int, List[Tuple[str, str]]], str]

# Restated (not imported) from `dispatch_emit/emit.py :: _AGENT_MODEL_GRAMMAR`
# -- importing `emit` would cycle, since `emit` imports `compose`. A test
# pins the two grammars equal (Design D6).
_AGENT_MODEL_GRAMMAR = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
_AGENT_MODEL_GRAMMAR_RE = re.compile(_AGENT_MODEL_GRAMMAR)

_VALID_EFFORTS = frozenset({"low", "medium", "high"})

#: Per-agent-type opts (Design D6): ``{model?, effort?}``. Any other key, or
#: a value failing its own grammar, raises `ComposeError`.
AgentOpts = Dict[str, Dict[str, str]]


class ComposeError(ValueError):
    pass


def _stage_phase_title(base_title: str, index: int, total: int) -> str:
    if total == 1:
        return base_title
    return f"{base_title} {index + 1}/{total}"


def _result_var(index: int) -> str:
    return f"reviewStage{index}Result"


def _validate_agent_opts_entry(agent_type: str, entry: Dict[str, str]) -> None:
    allowed = {"model", "effort"}
    unknown = set(entry) - allowed
    if unknown:
        raise ComposeError(
            f"agent_opts[{agent_type!r}] carries unknown key(s) {sorted(unknown)!r} -- "
            "only 'model' and 'effort' are allowed"
        )
    model = entry.get("model")
    if model is not None and not _AGENT_MODEL_GRAMMAR_RE.match(model):
        raise ComposeError(
            f"agent_opts[{agent_type!r}]['model'] {model!r} does not match the "
            f"required grammar {_AGENT_MODEL_GRAMMAR}"
        )
    effort = entry.get("effort")
    if effort is not None and effort not in _VALID_EFFORTS:
        raise ComposeError(
            f"agent_opts[{agent_type!r}]['effort'] {effort!r} is not one of "
            f"{sorted(_VALID_EFFORTS)!r}"
        )


def _agent_call_literal(
    agent_type: str,
    prompt: str,
    phase_title: str,
    *,
    schema: bool,
    as_arrow: bool,
    agent_opts: Optional[AgentOpts] = None,
    schema_literal: str = _GATE_SCHEMA_LITERAL,
) -> str:
    """Render one `agent(...)` call. `as_arrow` wraps it as `() => agent(...)`
    for use inside `parallel([...])`. A `model`/`effort` key appears only
    when the caller supplies one via `agent_opts` (Design D6) -- absent
    `agent_opts`, or an entry missing for this `agent_type`, output stays
    byte-identical to before D6. `schema_literal` is the raw JS object
    literal emitted as `schema:` when `schema` is true; defaults to the
    gate schema `compose()` has always used."""
    entry = (agent_opts or {}).get(agent_type)
    if entry is not None:
        _validate_agent_opts_entry(agent_type, entry)

    opts = (
        "{ "
        f"label: {_js_string_literal(f'review:{agent_type}')}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(agent_type)}"
    )
    if entry:
        model = entry.get("model")
        effort = entry.get("effort")
        if model is not None:
            opts += f", model: {_js_string_literal(model)}"
        if effort is not None:
            opts += f", effort: {_js_string_literal(effort)}"
    if schema:
        opts += f", schema: {schema_literal}"
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
    agent_opts: Optional[AgentOpts] = None,
) -> Tuple[str, str]:
    phase_title = _stage_phase_title(base_phase_title, index, total)
    lines = [f"  phase({_js_string_literal(phase_title)});"]

    if not stage.gate:
        if len(stage.agents) == 1:
            call = _agent_call_literal(
                stage.agents[0],
                prompt,
                phase_title,
                schema=False,
                as_arrow=False,
                agent_opts=agent_opts,
            )
            lines.append(f"  await {call};")
        else:
            item_calls = ",\n".join(
                "    "
                + _agent_call_literal(
                    agent,
                    prompt,
                    phase_title,
                    schema=False,
                    as_arrow=True,
                    agent_opts=agent_opts,
                )
                for agent in stage.agents
            )
            lines.append(f"  await parallel([\n{item_calls}\n  ]);")
        return phase_title, "\n".join(lines)

    gate_prompt = _composed_prompt(prompt, run_nonce) if run_nonce is not None else prompt

    var = _result_var(index)
    if len(stage.agents) == 1:
        agent = stage.agents[0]
        call = _agent_call_literal(
            agent, gate_prompt, phase_title, schema=True, as_arrow=False, agent_opts=agent_opts
        )
        lines.append(f"  const {var} = await {call};")
        results = [(agent, var)]
    else:
        item_calls = ",\n".join(
            "    "
            + _agent_call_literal(
                agent,
                gate_prompt,
                phase_title,
                schema=True,
                as_arrow=True,
                agent_opts=agent_opts,
            )
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
    *,
    agent_opts: Optional[AgentOpts] = None,
) -> List[Tuple[str, str]]:
    if not stages:
        raise ComposeError("compose() received an empty stage list")

    total = len(stages)
    return [
        _compose_stage(
            stage, index, total, prompt, phase_title, gate_policy, run_nonce, agent_opts
        )
        for index, stage in enumerate(stages)
    ]
