"""The one-refire halt envelope: builder, validator and the emitted JS finaliser body.

Leaf module: no I/O, no git, and it never imports ask_compose (ask_compose imports it).
A halt's `next_action` names exactly one engine call and never a hand edit; a decision the
refire reads rides under `input`.
"""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit.ask_contract import HALT_DESIGN_REVIEW

REFIRE_KIND = "refire"
OP_ENGINE_ASK = "engine-ask"
OP_WORKFLOW_RESUME = "workflow-resume"
OPS = (OP_ENGINE_ASK, OP_WORKFLOW_RESUME)

# `stage` is the resume handle from halt-keeps-work (hnd for 05b3e6-1-halt-keeps-work). This
# module does not know its shape: it is copied as-is from the `stage` argument and never read.
RESUME_KEYS = ("run_id", "sizing", "halted", "stage")

INPUT_KINDS = ("rule", "repin", "touchpoint", "em-discharge", "wait")
LEGACY_INPUT_KIND = {
    "rule_and_refire": "rule",
    "rule_on_blocking_rows": "rule",
    "repin_register": "repin",
}

# ask_compose's HALT_USAGE_LIMIT, spelled out because this module must not import ask_compose.
HALT_USAGE_LIMIT = "usage_limit"
INPUT_BY_HALT = {HALT_DESIGN_REVIEW: "touchpoint", HALT_USAGE_LIMIT: "wait"}

ENVELOPE_EXEMPT_KINDS = ("parked", "no-op")
FANNED_KINDS = ("split", "research")

HAND_REPAIR_MARKERS = (
    "fix the",
    "hand-finish",
    "hand-repair",
    "edit the",
    "apply the unapplied",
)

ALL_OWED_PREFIX = "Everything left is EM or operator work:"


def refire_next_action(
    *,
    call: str,
    run_id: str,
    sizing: str,
    halted: str,
    op: str = OP_ENGINE_ASK,
    input: dict | None = None,
    detail: str | None = None,
    stage=None,
) -> dict:
    """The envelope `{kind, op, call, resume, input, detail}`; `stage` is copied unchanged."""
    return {
        "kind": REFIRE_KIND,
        "op": op,
        "call": call,
        "resume": {"run_id": run_id, "sizing": sizing, "halted": halted, "stage": stage},
        "input": input,
        "detail": detail,
    }


def legacy_input(legacy: dict) -> dict:
    """Nest a legacy judgment next_action under `input`, fields intact, legacy kind kept."""
    out = dict(legacy)
    legacy_kind = legacy.get("kind")
    out["kind"] = LEGACY_INPUT_KIND.get(legacy_kind, legacy_kind)
    out["legacy_kind"] = legacy_kind
    return out


def _carries_hand_repair(text: object) -> bool:
    if not isinstance(text, str):
        return False
    low = text.lower()
    return any(marker in low for marker in HAND_REPAIR_MARKERS)


def _call_is_one_engine_call(op: str, call: str) -> bool:
    if op == OP_ENGINE_ASK:
        return call.count("Workflow(") == 1
    return "resumeFromRunId" in call and call.count("Workflow") <= 1


def is_refire_next_action(obj: object) -> bool:
    """True only for an envelope naming one engine call and no hand-repair step.

    `input` is not scanned: it carries reviewer and ruling text verbatim.
    """
    if not isinstance(obj, dict) or obj.get("kind") != REFIRE_KIND:
        return False
    op = obj.get("op")
    if op not in OPS:
        return False
    call = obj.get("call")
    if not isinstance(call, str) or not call.strip() or not _call_is_one_engine_call(op, call):
        return False
    resume = obj.get("resume")
    if not isinstance(resume, dict) or set(resume) != set(RESUME_KEYS):
        return False
    if not isinstance(resume.get("run_id"), str) or not resume["run_id"]:
        return False
    inp = obj.get("input")
    if inp is not None and (not isinstance(inp, dict) or inp.get("kind") not in INPUT_KINDS):
        return False
    return not (_carries_hand_repair(call) or _carries_hand_repair(obj.get("detail")))


def refire_next_action_js(
    call_js: str,
    legacy_js: str,
    kind_js: str,
    detail_js: str,
    stage_js: str = "null",
    run_id_js: str = "_runId",
    sizing_js: str = "_sizingRel",
) -> str:
    """JS source of `function _refireNextAction()`, which finalises `_halted.next_action` in place.

    Every argument is a JS expression: `call_js` the one engine call string, `legacy_js` the
    halt's current next_action, `kind_js` its halted kind, `detail_js` the default detail,
    `stage_js` the resume handle rendered into `resume.stage`. Idempotent; leaves exempt and
    already-fanned halts alone.
    """
    j = json.dumps
    return "\n".join(
        [
            "function _refireNextAction() {",
            "  if (!_halted || typeof _halted !== 'object') return;",
            f"  const _hk = {kind_js};",
            f"  if ({j(list(ENVELOPE_EXEMPT_KINDS))}.includes(_hk)) return;",
            f"  if ({j(list(FANNED_KINDS))}.includes(_hk) && Array.isArray(_halted.fire) && _halted.fire.length) return;",
            f"  const _legacy = {legacy_js};",
            f"  if (_legacy && typeof _legacy === 'object' && _legacy.kind === {j(REFIRE_KIND)}) return;",
            f"  const _legacyKind = {j(LEGACY_INPUT_KIND)};",
            f"  const _byHalt = {j(INPUT_BY_HALT)};",
            "  let _input = null;",
            "  let _detail = null;",
            "  if (typeof _legacy === 'string') {",
            "    _detail = _legacy;",
            f"    if (_legacy.startsWith({j(ALL_OWED_PREFIX)})) _input = {{ kind: 'em-discharge' }};",
            "  } else if (_legacy && typeof _legacy === 'object') {",
            "    _input = Object.assign({}, _legacy, { kind: _legacyKind[_legacy.kind] || _legacy.kind, legacy_kind: _legacy.kind });",
            f"    _detail = {detail_js};",
            "  }",
            "  if (!_input && _byHalt[_hk]) _input = { kind: _byHalt[_hk] };",
            "  _halted.next_action = {",
            f"    kind: {j(REFIRE_KIND)},",
            f"    op: _hk === {j(HALT_USAGE_LIMIT)} ? {j(OP_WORKFLOW_RESUME)} : {j(OP_ENGINE_ASK)},",
            f"    call: {call_js},",
            f"    resume: {{ run_id: {run_id_js}, sizing: {sizing_js}, halted: _hk, stage: {stage_js} }},",
            "    input: _input,",
            "    detail: _detail === undefined ? null : _detail,",
            "  };",
            "}",
        ]
    )
