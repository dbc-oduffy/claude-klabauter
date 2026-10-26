"""P-C refire: the reply names what it re-armed; an identical re-halt is a refusal, never ok:true."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    LEG_SKIP,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
)
from coordinator_core.ops.dispatch_emit.halt_refire import (
    is_refire_next_action,
    refire_next_action,
    refire_next_action_js,
    refire_reply,
)
from coordinator_core.ops.dispatch_emit.phase_ledger import HALT_PHASE
from coordinator_core.ops.dispatch_emit.refire_noop import identical_rehalt_refusal

SHA = "b" * 40
CALL = 'Workflow({script: "x"})'


def _ledger(**over):
    base = {"blocking": ["R9"], RESUME_LEDGER_HALT_SHA_FIELD: SHA, RESUME_LEDGER_NOTES_FIELD: []}
    base.update(over)
    return base


def test_refire_envelope_reports_what_it_rearmed():
    halted = next(iter(HALT_PHASE))
    env = refire_next_action(call=CALL, run_id="r1", sizing="s.yaml", halted=halted)
    assert is_refire_next_action(env)
    reply = refire_reply(env)
    assert reply.ok and reply.target == "r1"
    assert "r1" in reply.applied[0] and halted in reply.applied[0] and HALT_PHASE[halted] in reply.applied[0]
    assert reply.refused == ()


def test_unknown_halt_kind_reports_a_full_pass():
    env = refire_next_action(call=CALL, run_id="r2", sizing="s.yaml", halted="no-such-kind")
    assert "full pass" in refire_reply(env).applied[0]


def test_envelope_without_applied_is_not_ok():
    reply = refire_reply({"resume": {"run_id": "r3"}})
    assert not reply.ok and reply.target == "r3"


def test_js_envelope_carries_applied_and_refused():
    js = refire_next_action_js("'c'", "null", "'ledger-check'", "null")
    assert "applied:" in js and "refused: []" in js and "re-arm " in js


def test_identical_rehalt_is_a_refusal_with_nothing_applied():
    reply = identical_rehalt_refusal(_ledger(), [], [], [], [], {"action": LEG_SKIP}, target="r4")
    assert reply is not None
    assert not reply.ok and reply.applied == () and reply.target == "r4"
    assert "R9" in reply.refused[0] and SHA[:10] in reply.refused[0]


def test_changed_input_is_not_refused():
    assert identical_rehalt_refusal(_ledger(), [{"id": "C1"}], [], [], [], None) is None
    assert identical_rehalt_refusal(_ledger(), [], [], [], [], {"action": LEG_DISPATCH}) is None
    assert identical_rehalt_refusal(_ledger(**{RESUME_LEDGER_NOTES_FIELD: ["rule: x"]}), [], [], [], [], None) is None
