"""review.prep's peer_held split counts the sessions of a refired run's earlier pass as the run's own;
an unrelated live peer still holds, and a run with no earlier pass is unchanged."""

import json

from coordinator_core.ops.dispatch_emit import run_write_set as rws
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_run_write_set import RUN, _claims, _record

PREDECESSOR = "ask-20261109T100000-aaaaaa"
EARLIER_PASS = "earlier-pass-session"
UNRELATED_PEER = "unrelated-live-peer"


def _refire(tmp_path):
    _record(tmp_path)
    base = record_dir(tmp_path, RUN)
    (base / "run.json").write_text(
        json.dumps({"sizing_rel": "state/sizings/s.yaml", "resumed_from": PREDECESSOR}), encoding="utf-8")
    prior = record_dir(tmp_path, PREDECESSOR)
    prior.mkdir(parents=True)
    (prior / "run-stage.json").write_text(
        json.dumps({"session_id": EARLIER_PASS, "stage": "halted"}), encoding="utf-8")


def test_earlier_pass_session_claim_is_the_runs_own_and_an_unrelated_peer_still_holds(tmp_path, monkeypatch):
    _refire(tmp_path)
    _claims(monkeypatch, {"pkg/a.py": [EARLIER_PASS], "pkg/f.py": [UNRELATED_PEER], "pkg/w.py": [EARLIER_PASS, UNRELATED_PEER]})

    kept, held = rws.run_write_set_split(tmp_path, RUN, "refire-driver")

    assert "pkg/a.py" in kept
    assert held == {"pkg/f.py": [UNRELATED_PEER], "pkg/w.py": [UNRELATED_PEER]}


def test_without_a_resumed_from_link_the_same_claim_is_a_peer_hold(tmp_path, monkeypatch):
    _refire(tmp_path)
    (record_dir(tmp_path, RUN) / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    _claims(monkeypatch, {"pkg/a.py": [EARLIER_PASS]})

    kept, held = rws.run_write_set_split(tmp_path, RUN, "refire-driver")

    assert held == {"pkg/a.py": [EARLIER_PASS]}
    assert "pkg/a.py" not in kept
