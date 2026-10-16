"""workflow.revise from a peer session: the sender is stamped on the row and rendered, a held
(not yet emitted) run queues the revision until emit, and both stage ledger clauses name it."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import ParentRecord, SiblingRun, parent_record_rel

RUN = "ask-20261016T000000-abc123"
REL = "state/sizings/2026-10-16-peer.yaml"
PEER = "example-game-workbench-repo-25"


def _sizing():
    return {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    return tmp_path


def _rows(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8")).rows


def _view(repo):
    return (repo / RECORD_DIR_ROOT / RUN / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")


def test_from_is_an_accepted_param():
    assert "from" in {f.name for f in rl._PARAMS}


def test_bound_run_stamps_sender_on_row_and_renders_it(repo):
    rl.start(repo, RUN)
    rl.bind(repo, RUN, REL, ask_text="a raw ask")
    reply = rl._handler({"run_id": RUN, "verbatim": "also cover X", "from": PEER}, repo_root=repo)
    assert reply["ok"] is True
    peer_row = _rows(repo)[-1]
    assert peer_row["source_text"] == "also cover X"
    assert peer_row["anchor"].startswith("revision@") and peer_row["anchor"].endswith(f" from {PEER}")
    assert f"from {PEER}" in _view(repo)
    rl.revise(repo, RUN, verbatim="em change")
    em_row = _rows(repo)[-1]
    assert " from " not in em_row["anchor"]
    ledger = rl.read_ledger(repo, repo / RECORD_DIR_ROOT / RUN)
    senders = [i.sender for i in ledger.items if i.origin == rl.ORIGIN_REVISION]
    assert senders == [PEER, ""]
    assert all(i.at and " " not in i.at for i in ledger.items if i.origin == rl.ORIGIN_REVISION)


def _seed_parent(repo, run_id=RUN):
    parent = ParentRecord(parent_id="ask-parent", runs=(SiblingRun(run_id=run_id, label="a"),))
    path = repo / parent_record_rel("ask-parent")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(parent.to_json()), encoding="utf-8")


def test_unknown_run_id_is_refused_and_creates_no_directory(repo):
    _seed_parent(repo)
    typo = "ask-20261016T000000-abc124"
    reply = rl._handler({"run_id": typo, "verbatim": "lost?", "from": PEER}, repo_root=repo)
    assert "no engine-minted parent record" in reply["error"] and "held" not in reply
    assert not (repo / RECORD_DIR_ROOT / typo).exists()


def test_held_run_accepts_and_folds_at_emit(repo):
    _seed_parent(repo)
    reply = rl._handler({"run_id": RUN, "verbatim": "early steer", "from": PEER}, repo_root=repo)
    assert reply["ok"] is True and reply["held"] is True and reply["item"] == "Q1"
    assert not (repo / RECORD_DIR_ROOT / RUN / rl.RUN_FILE).exists()
    rl._handler({"run_id": RUN, "verbatim": "second steer"}, repo_root=repo)

    rl.start(repo, RUN)
    assert f"from {PEER}" in _view(repo)
    rl.bind(repo, RUN, REL, ask_text="a raw ask")
    rows = _rows(repo)
    steer = [r for r in rows if r["source_text"] in ("early steer", "second steer")]
    assert [r["source_text"] for r in steer] == ["early steer", "second steer"]
    assert steer[0]["anchor"].endswith(f" from {PEER}") and " from " not in steer[1]["anchor"]
    assert not (repo / RECORD_DIR_ROOT / RUN / rl.INBOX_FILE).exists()


def test_held_run_still_refuses_flag_and_out_of_scope(repo):
    for params in ({"all_of_pm_ask": True}, {"verbatim": "x", "out_of_scope": ["R1"]}):
        reply = rl._handler({"run_id": RUN, **params}, repo_root=repo)
        assert "emit it first" in reply["error"]


def test_both_ledger_clauses_name_peer_revisions():
    for clause in (ask_compose._LEDGER_CLAUSE, ask_compose._RELAYED_LEDGER_CLAUSE):
        assert "peer session" in clause and "workflow.revise" in clause and "same authority" in clause
    assert "relayed ask" in ask_compose._RELAYED_LEDGER_CLAUSE
