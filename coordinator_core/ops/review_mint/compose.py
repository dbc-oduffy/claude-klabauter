
"""Renders one `agent(...)` call literal, validating per-agent model/effort opts."""

from __future__ import annotations

import re
from typing import Dict, Optional

from coordinator_core.ops.workflow_scaffold import _js_string_literal

#: Delimits a script-scope JS expression spliced into prompt text (the run base, resolved at
#: fire). Restated from `dispatch_emit/emit.py :: _SHARED_PATH_MARKER_DELIM` (cycle).
PROMPT_MARKER_DELIM = "\x01"


def prompt_literal(prompt: str) -> str:
    """``prompt`` as one JS expression: text between marker pairs is spliced as bare JS,
    the rest is a string literal. Plain ``_js_string_literal`` when no marker is present."""
    if PROMPT_MARKER_DELIM not in prompt:
        return _js_string_literal(prompt)
    pieces = [
        part if i % 2 else _js_string_literal(part)
        for i, part in enumerate(prompt.split(PROMPT_MARKER_DELIM))
        if i % 2 or part
    ]
    return " + ".join(pieces) if pieces else "''"


# AC5's exact abort-object field names -- also the structured-output schema
# field names stamped on every gated agent call, so a `gate_policy` closure
# can read them straight off the captured result (`result.verdict`,
# `result.reason`, `result.sidecar_path`) with no renaming step.
#
# `run_nonce` is fixed by the cross-repo charter contract
# (`sidecar-emission-contract.md` / `sidecar-frontmatter-contract.md`) -- do not rename it.
_GATE_SCHEMA_LITERAL = (
    "{ type: 'object', properties: { "
    "verdict: { type: 'string' }, "
    "reason: { type: 'string' }, "
    "sidecar_path: { type: 'string' }, "
    "run_nonce: { type: 'string' } "
    "}, required: ['verdict', 'run_nonce'] }"
)

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
    gate schema."""
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

    call = f"agent({prompt_literal(prompt)}, {opts})"
    return f"() => {call}" if as_arrow else call
