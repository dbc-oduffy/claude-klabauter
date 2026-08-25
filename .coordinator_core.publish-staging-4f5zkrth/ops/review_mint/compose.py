"""coordinator_core.ops.review_mint.compose — stage list -> Workflow script text.

Spec: ``docs/plans/2026-08-19-review-mints-its-own-gated-workflow.md`` task C2.

ONE composer, TWO caller-supplied policies. ``review.mint_workflow`` (C3,
pre-execution, nothing has landed yet) and ``dispatch.emit`` (C4,
post-execution, commits already landed) each call ``compose()`` with their
own ``prompt``/``phase_title``/``gate_policy`` — this module never forks
into a second copy for either caller (see plan Anti-scope "Do not fork the
roster consumer").

``compose()`` is PURE: it takes an already-parsed ``[Stage, ...]`` list
(``roster.parse_stages``'s output) and returns script TEXT fragments. No
file I/O, no fragment parsing, no cross-repo pointer resolution — those live
at ``roster.py`` (C1) and the op boundary (C3's ``load_fragment()``).

## Per-stage composition (AC2/AC3)

Each ``Stage`` emits one ``phase()`` call, then either one serial
``await agent(...)`` (``len(stage.agents) == 1``) or one
``await parallel([...])`` (``len(stage.agents) >= 2``) — arity alone
decides, never a flag read off the fragment (DR-327; see plan Anti-scope
"Do not read a `parallel` flag from the fragment").

## Gate stages (AC4/AC5/AC6)

A ``gate: true`` stage additionally:

  - stamps a structured-output ``schema`` on EVERY agent call in that stage
    (blocking-capable or not — AC6: an agent with no blocking verdict still
    completes and is still schema'd, it just never satisfies a caller's
    abort condition), naming ``verdict``/``reason``/``sidecar_path`` — the
    exact field names AC5 requires the abort object to carry;
  - captures each call's resolved structured output into a named JS const
    (``const <var> = await agent(...)`` for arity 1, ``const <var> = await
    parallel([...])`` for arity 2+, indexed ``<var>[i]`` per agent in
    parallel);
  - calls ``gate_policy(stage, stage_index, results)`` — ``results`` is the
    ordered ``[(agentType, js_result_expression), ...]`` for that stage —
    and splices whatever JS statement text it returns immediately after the
    capture statement(s).

``compose()`` deliberately does NOT know which verdict token blocks for
which agent: that mapping is ``blocking_verdicts`` from the v3 fragment,
charter data owned by DoE-claude's agents (see ``roster.py``'s module
docstring and plan Anti-scope "Do not define 'blocked' for
coordinator-claude's agents"). A caller builds its ``gate_policy`` closure
over that mapping (and, for ``review.mint_workflow``, over the abort shape
AC5 requires); this module only wires the mechanical parts — schema,
result capture, splice point — that are identical for both callers.

``gate_policy`` returning ``""`` (falsy) disarms the branch entirely for
that stage — no `if` is emitted at all. This is C4's option (a): a caller
whose commits have already landed (``dispatch.emit``) can pass a
``gate_policy`` that always returns ``""``, so stages compose in order for
narration only and the terminal test phase downstream is never skipped.
Option (b) — composing the abort so control falls through to a later phase
rather than ``return``-ing out of the script — is equally legal: nothing
here requires an early ``return``, only that whatever ``gate_policy``
supplies is valid JS spliced at that point.

## Negative spec

  - Reviewer/persona call sites carry ``agentType`` and NO ``model:`` key
    (plan Anti-scope "Do not stamp `model:` on a call site that carries
    `agentType`") — ``opts.model`` overrides the agent definition's own
    charter tier.
  - No commit stage, no test/pytest stage. This module composes review
    stages only; the terminal test phase (``dispatch.emit``) or nothing
    (``review.mint_workflow``) is the caller's business.
  - Does not read a ``parallel`` key off a ``Stage`` — arity is the only
    signal (see above).
  - Does not resolve which verdict blocks which agent (see above) — that is
    ``gate_policy``'s closure, supplied by the caller.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

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

# GatePolicy: called once per `gate: true` Stage with the stage itself, its
# 0-based index within the `stages` list passed to `compose()`, and the
# ordered [(agentType, js_result_expression), ...] for that stage's calls.
# Returns the JS statement text to splice immediately after the capture
# statement(s) -- "" (falsy) disarms the branch for that stage entirely.
GatePolicy = Callable[[Stage, int, List[Tuple[str, str]]], str]


class ComposeError(ValueError):
    """Raised when `compose()` is asked to compose an empty stage list.

    `roster.parse_stages` never returns an empty list (it refuses loudly
    first -- see its own docstring), so this only fires against a caller
    that bypassed that parser. Fail loud rather than emit a phase-less
    fragment that silently narrates nothing.
    """


def _stage_phase_title(base_title: str, index: int, total: int) -> str:
    """A single stage reuses `base_title` verbatim; 2+ stages get a unique,
    order-preserving suffix so each stage's `phase()` call and its
    `meta.phases` entry stay 1:1 (AC2's stage-order requirement)."""
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
    """AC12 leg 2: a GATE stage's agents get `run_nonce: <value>` appended to
    their prompt text so they can echo it back in structured output --
    non-gate stages never call this (see `_compose_stage`; the run-report
    family is exempt by construction, not by a flag read here)."""
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
        # Non-gate stage: identical shape to today's `_review_phase_calls`
        # -- no schema, no captured result, fire-and-forget, no run_nonce
        # (AC12 anti-scope: only a gate stage has a verdict to refuse).
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
    """Compose `stages` (``roster.parse_stages``'s output) into an ordered
    list of ``(phase_title, script_block)`` pairs.

    A caller (``review.mint_workflow``'s op, ``dispatch.emit``'s
    `compose_script`) extends its own `phase_titles`/`body_blocks` lists
    with this return value's entries, in order -- the same shape every
    other section of an emitted script already uses (see
    `dispatch_emit.emit.compose_script`).

    `prompt` is used verbatim for every agent call in every stage -- callers
    pass their own wording (see module docstring; C4's caller passes
    post-execution wording, `review.mint_workflow`'s pre-execution wording,
    never inheriting the other's).

    `phase_title` is the BASE title; a single-stage `stages` list reuses it
    verbatim, a multi-stage list gets a unique, order-preserving suffix per
    stage (`_stage_phase_title`) so every `phase()` call's title stays 1:1
    with its own `meta.phases` entry.

    `run_nonce` (AC12, optional -- defaults to `None`, i.e. no behavior
    change for a caller that never passes one, e.g. `dispatch.emit`'s
    already-landed C4 call site) is appended to a GATE stage's prompt text
    ONLY (`_composed_prompt`); non-gate stages never see it, matching the
    fixed `run_nonce` schema field name coordinator-claude's charter
    contracts key on -- do not rename it.

    Raises `ComposeError` on an empty `stages` list -- see its docstring.
    """
    if not stages:
        raise ComposeError("compose() received an empty stage list")

    total = len(stages)
    return [
        _compose_stage(stage, index, total, prompt, phase_title, gate_policy, run_nonce)
        for index, stage in enumerate(stages)
    ]
