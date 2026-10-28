"""_refuse_foreign_emission: a halted run's script is admitted whichever session emitted it."""

import json

import pytest

from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path
from coordinator_core.ops.dispatch_emit.op import ForeignEmissionError, _refuse_foreign_emission


def _peer_script(tmp_path, session_id):
    target = tmp_path / "run.workflow.mjs"
    target.write_text("// peer", encoding="utf-8", newline="")
    emission_receipt_path(target).write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    return target


@pytest.mark.parametrize("live", [False, True])
def test_halted_run_script_is_admitted_whether_emitter_is_live_or_dead(tmp_path, monkeypatch, live):
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda _sid: live)
    target = _peer_script(tmp_path, "peer")
    _refuse_foreign_emission(target, "// ours", "me", tmp_path, run_owned=True)


def test_live_peer_runs_script_is_still_refused(tmp_path):
    target = _peer_script(tmp_path, "live-peer")
    with pytest.raises(ForeignEmissionError, match="already holds a DIFFERENT emission"):
        _refuse_foreign_emission(target, "// ours", "me", tmp_path, run_owned=False)
