"""workflow.revise to a refired ask- id lands on the warp- run its run-stage record names."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir, write_run_stage_at

ASK = "ask-20261106T091921-aaaaaa"
WARP = "warp-2026-11-06-x-20261106T091921-593b1a"
REL = "state/sizings/2026-11-06-refired.yaml"


def _repo(tmp_path, *, link: bool):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    for rid in (ASK, WARP):
        rl.start(tmp_path, rid)
        rl.bind(tmp_path, rid, REL, ask_text="a raw ask")
    if link:
        write_run_stage_at(record_dir(tmp_path, ASK), "staged", warp_run=WARP)
    return tmp_path


def _rows(root, run_id):
    ledger = rl.read_ledger(root, record_dir(root, run_id))
    return [i.text for i in ledger.items] if ledger else []


def test_ask_id_with_warp_link_revises_the_warp_run(tmp_path):
    root = _repo(tmp_path, link=True)
    outcome: dict = {}
    rl.revise(root, ASK, verbatim="also cover Windows", outcome=outcome)
    assert outcome["executing_run"] == WARP
    assert any("also cover Windows" in r for r in _rows(root, WARP))


def test_ask_id_without_warp_link_revises_itself(tmp_path):
    root = _repo(tmp_path, link=False)
    outcome: dict = {}
    rl.revise(root, ASK, verbatim="also cover Windows", outcome=outcome)
    assert "executing_run" not in outcome
    assert any("also cover Windows" in r for r in _rows(root, ASK))
