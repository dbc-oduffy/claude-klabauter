"""A {sizing, writes} refire of a run whose token a landing attempt spent mints a fresh run, as the {sizing}-only refire does."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask import _Emit, _hso, _payload, _repo
from coordinator_core.hooks.tests.test_submit_engine_ask_spent_token_refire import (
    DEAD_RUN,
    SIZING,
    _strand_claim,
)
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.halted_run import halted_run_for, write_halt_marker

pytestmark = pytest.mark.complete

WRITES = ["a/one.py", "a/two.py"]


def test_sizing_plus_writes_refire_of_a_spent_halt_mints_fresh_and_forwards_writes(tmp_path):
    root = _repo(tmp_path)
    write_halt_marker(root / "state" / "warp" / DEAD_RUN, halted="refusal", sizing_rel=SIZING)
    _strand_claim(root, DEAD_RUN)
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING, "writes": WRITES}), emit=emit)

    assert _hso(out)["permissionDecision"] == "allow"
    assert halted_run_for(root, SIZING) is None
    params, _root = emit.calls[0]
    assert "reserved_run_id" not in params
    assert params["writes"] == WRITES
    assert run_token.is_live(root, "ask-20261013T000000-abcdef")


def _launch(root, parent_id, children, session_id):
    script = root / "launcher.mjs"
    script.write_text("export const meta = {}\n", encoding="utf-8")
    return script


def test_writes_refire_whose_halted_run_cannot_hold_a_live_token_mints_a_fresh_run(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    write_halt_marker(root / "state" / "warp" / DEAD_RUN, halted="refusal", sizing_rel=SIZING)
    monkeypatch.setattr(mod, "_ensure_launcher_run_token", lambda *a, **k: None)
    emit = _Emit()

    out = mod.evaluate(_payload(root, {"sizing": SIZING, "writes": WRITES}), emit=emit, launch=_launch)

    assert _hso(out)["permissionDecision"] == "allow"
    assert halted_run_for(root, SIZING) is None
    params, _root = emit.calls[0]
    assert "reserved_run_id" not in params
    assert params["writes"] == WRITES


def test_writes_refire_whose_halted_run_holds_a_live_token_resumes_it(tmp_path):
    root = _repo(tmp_path)
    write_halt_marker(root / "state" / "warp" / DEAD_RUN, halted="refusal", sizing_rel=SIZING)
    emit = _Emit()

    mod.evaluate(_payload(root, {"sizing": SIZING, "writes": WRITES}), emit=emit, launch=_launch)

    assert emit.calls == []
    assert halted_run_for(root, SIZING) == DEAD_RUN
    assert run_token.is_live(root, DEAD_RUN)


def test_sizing_plus_writes_refire_of_an_unspent_halt_still_resumes(tmp_path):
    root = _repo(tmp_path)
    write_halt_marker(root / "state" / "warp" / DEAD_RUN, halted="refusal", sizing_rel=SIZING)

    mod.evaluate(_payload(root, {"sizing": SIZING, "writes": WRITES}), emit=_Emit())

    assert halted_run_for(root, SIZING) == DEAD_RUN
