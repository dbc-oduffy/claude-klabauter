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


WARP_RUN = "warp-2026-10-01-gate-20261027T142436-989a2d"
NOTE = "keep the exit criterion literal"


def _write_notes(rd, note=NOTE):
    import json

    rd.mkdir(parents=True, exist_ok=True)
    (rd / "notes.jsonl").write_text(json.dumps({"note": note, "at": "2026-11-12T00:00:00Z"}) + "\n", encoding="utf-8")


def _spent_unmarked_run(root, run_id=WARP_RUN):
    """A staged run the gate refused for a spent token: token-spent.json, run.json, no halted.json."""
    import json

    from coordinator_core.ops.dispatch_emit.ask_gate import _mark_token_spent

    rd = root / "state" / "warp" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")
    _mark_token_spent(root, run_id)
    assert (rd / TOKEN_SPENT_FILE).is_file() and not (rd / "halted.json").exists()
    return rd


def test_refire_of_a_spent_run_with_no_halt_marker_mints_a_fresh_run_and_keeps_its_notes(tmp_path):
    from coordinator_core.ops.dispatch_emit.halted_run import notes_predecessor

    root = _repo(tmp_path)
    _write_notes(_spent_unmarked_run(root))
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    params, _root = emit.calls[0]
    assert "reserved_run_id" not in params
    assert run_token.is_live(root, "ask-20261013T000000-abcdef")
    assert notes_predecessor(root, SIZING, exclude="ask-20261013T000000-abcdef") == WARP_RUN


def test_refire_of_a_halted_spent_run_with_notes_mints_one_fresh_run_and_keeps_its_notes(tmp_path):
    from coordinator_core.ops.dispatch_emit.halted_run import notes_predecessor

    root = _repo(tmp_path)
    rd = root / "state" / "warp" / DEAD_RUN
    write_halt_marker(rd, halted="refusal", sizing_rel=SIZING)
    _write_notes(rd)
    _strand_claim(root, DEAD_RUN)
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    assert run_token.is_live(root, "ask-20261013T000000-abcdef")
    assert notes_predecessor(root, SIZING, exclude="ask-20261013T000000-abcdef") == DEAD_RUN


def test_refire_of_a_sizing_bound_to_a_spent_ask_id_mints_one_fresh_run_and_keeps_notes(tmp_path):
    from coordinator_core.ops.dispatch_emit.halted_run import notes_predecessor

    root = _repo(tmp_path)
    import json

    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    parent = "ask-20261112T185000-aaaaaa"
    (root / SIZING).parent.mkdir(parents=True, exist_ok=True)
    record = {"run_id": ASK_RUN, "label": "x", "after": [], "slice_sizing": SIZING, "status": "pending"}
    prec = root / contract.parent_record_rel(parent)
    prec.parent.mkdir(parents=True, exist_ok=True)
    prec.write_text(
        json.dumps({"schema": "ask-parent", "version": 1, "parent_id": parent, "runs": [record]}), encoding="utf-8"
    )
    (root / SIZING).write_text(f"em_analysis:\n  ask_parent: {parent} {ASK_RUN}\n", encoding="utf-8")
    _strand_claim(root, ASK_RUN)
    _write_notes(root / "state" / "warp" / ASK_RUN)
    (root / "state" / "warp" / ASK_RUN / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    params, _root = emit.calls[0]
    assert "reserved_run_id" not in params
    assert run_token.is_live(root, "ask-20261013T000000-abcdef")
    assert notes_predecessor(root, SIZING, exclude="ask-20261013T000000-abcdef") == ASK_RUN


LIVE_ASK_RUN = "ask-20261112T185110-5534c2"


def _bound_slice(root, run_id=LIVE_ASK_RUN):
    """A fan-out slice sizing pinned to `run_id` by `ask_parent`, whose run dir holds run.json and a revise note."""
    import json

    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract

    parent = "ask-parent-20261112T185110-d51157"
    prec = root / contract.parent_record_rel(parent)
    prec.parent.mkdir(parents=True, exist_ok=True)
    record = {"run_id": run_id, "label": "x", "after": [], "slice_sizing": SIZING, "status": "ready"}
    prec.write_text(
        json.dumps({"schema": "ask-parent", "version": 1, "parent_id": parent, "runs": [record]}), encoding="utf-8"
    )
    (root / SIZING).parent.mkdir(parents=True, exist_ok=True)
    (root / SIZING).write_text(f"em_analysis:\n  ask_parent: {parent} {run_id}\n", encoding="utf-8")
    rd = root / "state" / "warp" / run_id
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "minted"}), encoding="utf-8")
    _write_notes(rd)
    return rd


def test_refire_of_an_ask_id_run_whose_token_was_consumed_unlanded_emits_once_fresh_with_notes(tmp_path):
    from coordinator_core.ops.dispatch_emit.halted_run import notes_predecessor

    root = _repo(tmp_path)
    _bound_slice(root)
    run_token.mint(root, LIVE_ASK_RUN, f"scratch/warp/{LIVE_ASK_RUN}.workflow.mjs", sizing_rel=SIZING)
    run_token.consume(root, LIVE_ASK_RUN)
    assert not run_token.is_live(root, LIVE_ASK_RUN)
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    params, _root = emit.calls[0]
    assert "reserved_run_id" not in params
    assert run_token.is_live(root, "ask-20261013T000000-abcdef")
    assert notes_predecessor(root, SIZING, exclude="ask-20261013T000000-abcdef") == LIVE_ASK_RUN


def test_refire_of_an_ask_id_run_whose_token_is_still_live_keeps_its_reserved_id(tmp_path):
    root = _repo(tmp_path)
    _bound_slice(root)
    run_token.mint(root, LIVE_ASK_RUN, f"scratch/warp/{LIVE_ASK_RUN}.workflow.mjs", sizing_rel=SIZING)
    emit = _Emit()

    mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert emit.calls[0][0].get("reserved_run_id") == LIVE_ASK_RUN


def test_an_unmarked_run_with_an_unspent_token_is_still_the_resume_target(tmp_path):
    import json

    root = _repo(tmp_path)
    rd = root / "state" / "warp" / WARP_RUN
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")

    assert mod._unmarked_emitted_run(root, SIZING) == WARP_RUN
    (rd / TOKEN_SPENT_FILE).write_text("{}\n", encoding="utf-8")
    assert mod._unmarked_emitted_run(root, SIZING) is None


def test_a_claim_past_its_deadline_is_retired(tmp_path):
    root = _repo(tmp_path)
    _claim_with_budget(root, ASK_RUN, 0.001)
    import time

    time.sleep(0.05)
    assert token_retired(root, ASK_RUN) is True
