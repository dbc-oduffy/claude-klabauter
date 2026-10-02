"""Shared adjudication block for emitted grind workflows.

A hand-back a grind would put to the PM goes first to an adjudicator agent (coordinator:apm for
scope, direction and priority). Only an entry the adjudicator marks ``pm_only`` stays PM-bound.
A ruling never clears a named irreversible or external gate, and a missing verdict fails closed.
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.ops.dispatch_emit import grind_stages as stages
from coordinator_core.ops.workflow_scaffold import _js_string_literal

#: Hand-back types that put a question to the PM. park / wont-do / yagni are settled
#: dispositions, not questions, and stay outside.
PM_BOUND_HANDBACK_TYPES: tuple[str, ...] = ("needs-judgment", "unclear-direction")

ADJUDICATOR_AGENT_TYPE = "coordinator:apm"

#: Must appear in the grind's meta.phases, or the contract linter warns on every emit.
ADJUDICATE_PHASE_TITLE = "Adjudicate"

#: Same gate as DoE plan-blitz.mjs: a ruling clears a decision, never a merge, publish or push to main.
IRREVERSIBLE_GATE_SOURCE = (
    r"\b(merge[ds]? (to|into) main|push(ed|ing)? to main|force-push|publish(ed|ing)?|release"
    r"|cross-repo commit|branch deletion|history rewrite|rewrite history)\b"
)

_ADJUDICATION_SCHEMA = {
    "type": "object",
    "required": ["verdict", "pmOnly", "ruling"],
    "properties": {
        "verdict": {"type": "string", "enum": ["ruled", "pm-only"]},
        "pmOnly": {"type": "boolean"},
        "pmOnlyGround": {"type": "string"},
        "ruling": {"type": "string"},
    },
}

_PROMPT_HEAD = (
    "phase: adjudicate\n\n"
    "You are the PM's delegate for one hand-back of a grind run. Nobody escalates to the human here: "
    "a scope, direction or priority matter is yours as the APM. Rule it, do not re-ask it.\n\n"
)
_PROMPT_TAIL = (
    "\n\nDo this:\n"
    "  1. Read the row and what it cites. Decide the question with what is on disk.\n"
    "  2. Write one line on the row record: `pm_ruling: \"apm (PM-delegated) <your ruling>\"`.\n"
    "  3. Return verdict 'ruled' and pmOnly false.\n\n"
    "Return pmOnly true (verdict 'pm-only', with pmOnlyGround) ONLY when the matter is important AND "
    "urgent AND has no clear right answer, or needs an external or irreversible action (merge, "
    "publish, push to main, cross-repo commit assent). Being unsure is not a ground. "
    "You do not stage or commit anything."
)


def compose_adjudicate_block(*, agent_type_host: Optional[str]) -> str:
    """JS text defining ``_adjudicatePmBound()``; the caller awaits it after the grind loop.

    Every PM-bound entry in ``_handedBack`` gains an ``adjudication`` record
    (adjudicator, verdict, pmOnly, ruling); a missing verdict yields ``pmOnly: true``.
    """
    call = stages._agent_call(
        _adjudicate_prompt_expr(),
        label="adjudicate",
        phase_title=ADJUDICATE_PHASE_TITLE,
        agent_type=ADJUDICATOR_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="high",
        schema=_ADJUDICATION_SCHEMA,
        is_expr=True,
    )
    prefix, suffix = "  await agent(", ");"
    assert call.startswith(prefix) and call.endswith(suffix)
    agent_call = "  const _result = (await agent(" + call[len(prefix) : -len(suffix)] + ")) || {};"
    types = ", ".join(_js_string_literal(t) for t in PM_BOUND_HANDBACK_TYPES)
    return (
        f"const PM_BOUND_TYPES = [{types}];\n"
        f"const IRREVERSIBLE_GATE = /{IRREVERSIBLE_GATE_SOURCE}/i;\n"
        "async function _adjudicateOne(h) {\n"
        + agent_call
        + "\n  _recordCall('adjudicate');\n"
        "  return _result.verdict ? _result : null;\n"
        "}\n"
        "function _settleAdjudication(h, v) {\n"
        "  const gated = IRREVERSIBLE_GATE.test([h.reason, v && v.ruling].join(' '));\n"
        "  const pmOnly = !v || v.pmOnly === true || gated;\n"
        "  h.adjudication = {\n"
        f"    adjudicator: {_js_string_literal(ADJUDICATOR_AGENT_TYPE)},\n"
        "    verdict: v ? v.verdict : 'unavailable',\n"
        "    pmOnly,\n"
        "    ...(v && v.pmOnlyGround ? { pmOnlyGround: v.pmOnlyGround } : {}),\n"
        "    ...(gated && !(v && v.pmOnly === true) ? { pmOnlyGround: 'external-or-irreversible' } : {}),\n"
        "    ruling: v ? v.ruling : null,\n"
        "  };\n"
        "}\n"
        "async function _adjudicatePmBound() {\n"
        "  const bound = _handedBack.filter((h) => PM_BOUND_TYPES.includes(h.type));\n"
        "  await Promise.all(bound.map(async (h) => {\n"
        "    let v = null;\n"
        "    try { v = await _adjudicateOne(h); } catch (_e) { v = null; }\n"
        "    _settleAdjudication(h, v);\n"
        "  }));\n"
        "}"
    )


def _adjudicate_prompt_expr() -> str:
    return stages._join_prompt_parts(
        [
            ("lit", _PROMPT_HEAD),
            ("lit", "Row: "),
            ("expr", "h.row"),
            ("lit", "\nProfile: "),
            ("expr", "PROFILE_NAME"),
            ("lit", "   Hand-back type: "),
            ("expr", "h.type"),
            ("lit", "\nQuestion: "),
            ("expr", "h.reason || '(no question stated)'"),
            ("lit", "\nManifest path: "),
            ("expr", "(QUEUE_GRIND_MANIFEST.entries.find((x) => x.row_id === h.row) || {}).path || '(unknown)'"),
            ("lit", "\nYour repo is `"),
            ("expr", "REPO_ROOT"),
            ("lit", "`: `cd` there before any command."),
            ("lit", _PROMPT_TAIL),
        ]
    )
