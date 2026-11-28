"""A failed fan-out child's printed {sizing} refire never re-reserves its spent run id."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask import _Emit, _hso, _payload, _repo
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_gate import reservation_spent

pytestmark = pytest.mark.complete

SIZING = "state/sizings/2026-10-01-gate.yaml"
CHILD = "ask-20261122T115343-1a1a43"


def _failed_child(root: Path, stage: str = "staged") -> Path:
    """A fan-out slice pinned to CHILD by `ask_parent`, whose run began (`stage`) and holds no token record."""
    parent = "ask-parent-20261122T115343-fee49d"
    prec = root / contract.parent_record_rel(parent)
    prec.parent.mkdir(parents=True, exist_ok=True)
    record = {"run_id": CHILD, "label": "x", "after": [], "slice_sizing": SIZING, "status": "ready"}
    prec.write_text(
        json.dumps({"schema": "ask-parent", "version": 1, "parent_id": parent, "runs": [record]}), encoding="utf-8"
    )
    (root / SIZING).parent.mkdir(parents=True, exist_ok=True)
    (root / SIZING).write_text(f"em_analysis:\n  ask_parent: {parent} {CHILD}\n", encoding="utf-8")
    rd = root / "state" / "warp" / CHILD
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")
    return rd


def _script() -> str:
    return f"scratch/warp/{CHILD}.workflow.mjs"


def test_a_staged_child_with_no_token_record_is_not_re_reserved(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _failed_child(root)
    assert reservation_spent(root, CHILD) is True
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert len(emit.calls) == 1
    assert "reserved_run_id" not in emit.calls[0][0]


def test_a_staged_child_whose_token_is_live_keeps_its_reserved_id(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _failed_child(root)
    run_token.mint(root, CHILD, _script(), sizing_rel=SIZING)
    assert reservation_spent(root, CHILD) is False
    emit = _Emit()

    mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert emit.calls[0][0].get("reserved_run_id") == CHILD


def test_a_staged_child_with_a_lander_in_flight_is_kept(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _failed_child(root)
    token = run_token.mint(root, CHILD, _script())
    assert run_token.redeem(root, CHILD, token, _script(), budget_secs=600)
    assert reservation_spent(root, CHILD) is False


def test_a_child_that_never_fired_keeps_its_reserved_id(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _failed_child(root, stage="minted")
    assert reservation_spent(root, CHILD) is False
    emit = _Emit()

    mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert emit.calls[0][0].get("reserved_run_id") == CHILD


def _real_failed_child(root: Path) -> str:
    """The production lifecycle of a child the chain fails: token minted, run staged, token redeemed."""
    from coordinator_core.ops.dispatch_emit import ask_contract

    _failed_child(root, stage="minted")
    token = run_token.mint(root, CHILD, _script(), sizing_rel=SIZING)
    ask_contract.write_run_stage(root, CHILD, "staged")
    assert run_token.redeem(root, CHILD, token, _script(), budget_secs=600)
    return token


def test_a_real_redeemed_child_with_a_lander_in_flight_is_kept(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _real_failed_child(root)
    assert reservation_spent(root, CHILD) is False


def test_a_real_child_whose_claim_ran_out_its_budget_is_not_re_reserved(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _real_failed_child(root)
    claimed = run_token._record_path(root, CHILD, claimed=True)
    record = json.loads(claimed.read_text(encoding="utf-8"))
    record["deadline"] = 1.0
    claimed.write_text(json.dumps(record), encoding="utf-8")
    assert reservation_spent(root, CHILD) is True
    emit = _Emit()

    mod.evaluate(_payload(root, {"sizing": SIZING}), emit=emit)

    assert "reserved_run_id" not in emit.calls[0][0]


def test_a_real_child_whose_token_was_consumed_without_landing_is_not_re_reserved(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _real_failed_child(root)
    run_token.consume(root, CHILD)
    assert reservation_spent(root, CHILD) is True
