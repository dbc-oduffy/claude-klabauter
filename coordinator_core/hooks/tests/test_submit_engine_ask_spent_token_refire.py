"""A {sizing} refire of a run whose token a landing attempt spent mints a fresh run on the first call."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask import _Emit, _hso, _payload, _repo
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_gate import TOKEN_SPENT_FILE, token_retired
from coordinator_core.ops.dispatch_emit.halted_run import halted_run_for, write_halt_marker

pytestmark = pytest.mark.complete

SIZING = "state/sizings/2026-10-01-gate.yaml"
DEAD_RUN = "warp-2026-10-01-gate-20261027T142436-989a2d"
ASK_RUN = "ask-20261109T233008-9b6883"


def _strand_claim(root, run_id):
    """A timed-out land attempt: the token is redeemed and never consumed or released."""
    script = f"scratch/warp/{run_id}.workflow.mjs"
    token = run_token.mint(root, run_id, script, sizing_rel=SIZING)
    assert run_token.redeem(root, run_id, token, script)
    assert not run_token.is_live(root, run_id)


def test_first_refire_of_a_halted_run_with_a_stranded_claim_mints_a_fresh_run(tmp_path):
    root = _repo(tmp_path)
    rd = root / "state" / "warp" / DEAD_RUN
    write_halt_marker(rd, halted="refusal", sizing_rel=SIZING)
    _strand_claim(root, DEAD_RUN)
    assert not (rd / TOKEN_SPENT_FILE).exists()
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert halted_run_for(root, SIZING) is None
    params, _root = emit.calls[0]
    assert "reserved_run_id" not in params
    assert run_token.is_live(root, "ask-20261013T000000-abcdef")


def test_a_halted_run_with_no_token_record_still_resumes(tmp_path):
    root = _repo(tmp_path)
    write_halt_marker(root / "state" / "warp" / DEAD_RUN, halted="refusal", sizing_rel=SIZING)

    mod.evaluate(_payload(root, {"sizing": SIZING}), emit=_Emit())

    assert halted_run_for(root, SIZING) == DEAD_RUN


def test_retired_covers_stranded_claim_and_spent_receipt_but_not_live_or_unfired(tmp_path):
    root = _repo(tmp_path)
    assert token_retired(root, ASK_RUN) is False  # never fired
    run_token.mint(root, ASK_RUN, f"scratch/warp/{ASK_RUN}.workflow.mjs")
    assert token_retired(root, ASK_RUN) is False  # live
    _strand_claim(root, "ask-20261109T233008-000001")
    assert token_retired(root, "ask-20261109T233008-000001") is True


def test_a_fanout_slice_reserved_run_with_a_stranded_claim_is_dropped_but_an_unfired_one_kept(tmp_path):
    root = _repo(tmp_path)
    _strand_claim(root, ASK_RUN)
    assert mod._reserved_token_spent(root, ASK_RUN) is True
    assert mod._reserved_token_spent(root, "ask-20261109T233008-111111") is False


def _claim_with_budget(root, run_id, budget_secs):
    script = f"scratch/warp/{run_id}.workflow.mjs"
    token = run_token.mint(root, run_id, script)
    assert run_token.redeem(root, run_id, token, script, budget_secs=budget_secs)


def test_a_claim_inside_its_deadline_is_a_lander_in_flight_not_retired(tmp_path):
    root = _repo(tmp_path)
    _claim_with_budget(root, ASK_RUN, 600)
    assert token_retired(root, ASK_RUN) is False
    assert mod._reserved_token_spent(root, ASK_RUN) is False


def test_a_claim_past_its_deadline_is_retired(tmp_path):
    root = _repo(tmp_path)
    _claim_with_budget(root, ASK_RUN, 0.001)
    import time

    time.sleep(0.05)
    assert token_retired(root, ASK_RUN) is True
