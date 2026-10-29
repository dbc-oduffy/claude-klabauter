"""Shared adjudication block for emitted grind workflows.

Every hand-back a grind would put to the PM is ruled by one batched Sonnet call per run. Only an
entry the adjudicator marks ``pm-only`` stays PM-bound; a plan-weight one returns ``spin-out``.
A ruling never clears a named irreversible or external gate, and a missing verdict fails closed.
"""

from __future__ import annotations

from typing import Mapping, Optional

from coordinator_core.ops.dispatch_emit import grind_stages as stages
from coordinator_core.ops.workflow_scaffold import _js_string_literal

#: Hand-back types that put a question to the PM. park / wont-do / yagni are settled
#: dispositions, not questions, and stay outside.
PM_BOUND_HANDBACK_TYPES: tuple[str, ...] = ("needs-judgment", "unclear-direction")

#: The opus APM persona: ask_compose's accept and xl-exit legs only; the grind never uses it.
APM_RULING_AGENT_TYPE = "coordinator:apm"

#: The grind's one batched adjudicate call: a plain Sonnet agent, no persona (model: ``STAGE_MODELS["adjudicate"]``).
ADJUDICATOR_AGENT_TYPE = stages.GENERAL_PURPOSE_AGENT_TYPE

#: Must appear in the grind's meta.phases, or the contract linter warns on every emit.
ADJUDICATE_PHASE_TITLE = "Adjudicate"

#: Same gate as DoE plan-blitz.mjs: a ruling clears a decision, never a merge, publish or push to main.
IRREVERSIBLE_GATE_SOURCE = (
    r"\b(merge[ds]? (to|into) main|push(ed|ing)? to main|force-push|publish(ed|ing)?|release"
    r"|cross-repo commit|branch deletion|history rewrite|rewrite history)\b"
)

_RULING_SCHEMA = {
    "type": "object",
    "required": ["row", "verdict", "ruling", "externalAction", "actionable"],
    "properties": {
        "row": {"type": "string"},
        "verdict": {"type": "string", "enum": ["ruled", "spin-out", "pm-only"]},
        "pmOnlyGround": {"type": "string"},
        "ruling": {"type": "string"},
        "externalAction": {"type": "string"},
        "actionable": {"type": "boolean"},
    },
}

#: One reply rules every PM-bound hand-back of the run; a row missing from it fails closed to pmOnly.
_ADJUDICATION_SCHEMA = {
    "type": "object",
    "required": ["rulings"],
    "properties": {"rulings": {"type": "array", "items": _RULING_SCHEMA}},
}

_PROMPT_HEAD = (
    "phase: adjudicate\n\n"
    "Rule every hand-back below in this one reply. Nobody escalates to the human here: decide each "
    "from what is on disk, do not re-ask it.\n\n"
)
_PROMPT_TAIL = (
    "\n\nReturn one entry per row in `rulings`, keyed by `row`:\n"
    "  1. Read the row and what it cites. Decide the question with what is on disk.\n"
    "  2. verdict 'ruled' for a matter you can decide; ruling is one line. actionable true only when "
    "the ruling names a concrete change the fix stage can apply now.\n"
    "  3. verdict 'spin-out' for a plan-weight matter (it needs a plan, not a fix); actionable false.\n"
    "  4. externalAction: the external or irreversible act the ruling needs, or '' for none.\n\n"
    "Return verdict 'pm-only' (with pmOnlyGround) ONLY when the matter is important AND urgent AND "
    "has no clear right answer, or needs an external or irreversible action (merge, publish, push to "
    "main, cross-repo commit assent). Being unsure is not a ground. "
    "You do not stage or commit anything."
)


def compose_adjudicate_block(
    *,
    agent_type_host: Optional[str],
    run_key: str = stages.DEFAULT_RUN_KEY,
) -> str:
    """JS text defining ``_adjudicatePmBound()``; the caller awaits it after the grind loop.

    Every PM-bound entry in ``_handedBack`` gains an ``adjudication`` record
    (adjudicator, verdict, pmOnly, ruling, actionable); a missing verdict yields ``pmOnly: true``.
    The gate scans only the adjudicator's declared ``externalAction``, never hand-back text.
    """
    call = stages._agent_call(
        _adjudicate_prompt_expr(),
        stage="adjudicate",
        models=None,
        label="adjudicate",
        phase_title=ADJUDICATE_PHASE_TITLE,
        agent_type=ADJUDICATOR_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="medium",
        schema=_ADJUDICATION_SCHEMA,
        is_expr=True,
        run_key=run_key,
    )
    prefix, suffix = "  await agent(", ");"
    assert call.startswith(prefix) and call.endswith(suffix)
    agent_call = "  const _result = (await agent(" + call[len(prefix) : -len(suffix)] + ")) || {};"
    types = ", ".join(_js_string_literal(t) for t in PM_BOUND_HANDBACK_TYPES)
    return (
        f"const PM_BOUND_TYPES = [{types}];\n"
        f"const IRREVERSIBLE_GATE = /{IRREVERSIBLE_GATE_SOURCE}/i;\n"
        "async function _adjudicateBatch(pending) {\n"
        + agent_call
        + "\n  _recordCall('adjudicate');\n"
        "  return Array.isArray(_result.rulings) ? _result.rulings : [];\n"
        "}\n"
        "function _settleAdjudication(h, v) {\n"
        "  const known = !!v && ['ruled', 'spin-out', 'pm-only'].includes(v.verdict);\n"
        "  const gated = known && IRREVERSIBLE_GATE.test(v.externalAction || '');\n"
        "  const stated = known && v.verdict === 'pm-only';\n"
        "  const pmOnly = !known || stated || gated;\n"
        "  h.adjudication = {\n"
        f"    adjudicator: {_js_string_literal(ADJUDICATOR_AGENT_TYPE)},\n"
        "    verdict: known ? v.verdict : 'unavailable',\n"
        "    pmOnly,\n"
        "    ...(known && v.pmOnlyGround ? { pmOnlyGround: v.pmOnlyGround } : {}),\n"
        "    ...(gated && !stated ? { pmOnlyGround: 'external-or-irreversible' } : {}),\n"
        "    ruling: known ? v.ruling : null,\n"
        "    actionable: known && v.verdict === 'ruled' && !!v.actionable && !pmOnly,\n"
        "  };\n"
        "}\n"
        "async function _adjudicateEntries(pending) {\n"
        "  let rulings = [];\n"
        "  try { rulings = await _adjudicateBatch(pending); } catch (_e) { rulings = []; }\n"
        "  for (const h of pending) {\n"
        "    _settleAdjudication(h, rulings.find((r) => r && r.row === h.row) || null);\n"
        "  }\n"
        "}\n"
        "async function _adjudicatePmBound() {\n"
        "  const pending = _handedBack.filter((h) => PM_BOUND_TYPES.includes(h.type) && !h.adjudication);\n"
        "  if (pending.length === 0) return;\n"
        "  await _adjudicateEntries(pending);\n"
        "}"
    )


def _adjudicate_prompt_expr() -> str:
    return stages._join_prompt_parts(
        [
            ("lit", _PROMPT_HEAD),
            ("lit", "Profile: "),
            ("expr", "PROFILE_NAME"),
            ("lit", "\nHand-backs:\n"),
            (
                "expr",
                "pending.map((h) => '- row ' + h.row + ' [' + h.type + ']: ' + (h.reason || '(no question stated)')"
                " + ' (manifest: ' + ((QUEUE_GRIND_MANIFEST.entries.find((x) => x.row_id === h.row) || {}).path"
                " || '(unknown)') + ')').join('\\n')",
            ),
            ("lit", "\nYour repo is `"),
            ("expr", "REPO_ROOT"),
            ("lit", "`: `cd` there before any command."),
            ("lit", _PROMPT_TAIL),
        ]
    )
