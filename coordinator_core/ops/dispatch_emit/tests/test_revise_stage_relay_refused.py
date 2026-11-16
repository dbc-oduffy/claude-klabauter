"""workflow.revise refuses a pipeline-run note asking a stage to message a peer, and resolves an ask- id to its live pipeline run."""
from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.pipeline_contract import RUN_DIR_ROOT as DIR
from coordinator_core.ops.dispatch_emit.pipeline_run import REVISIONS_MARKER as MARKER

RID = "pipeline-research-20261106T125345-87d688"
ASK = "ask-20261106T125300-04adde"
SIZING = "state/sizings/2026-11-06-research.yaml"


def _emit(root, run_id=RID, sizing=SIZING, finished=False):
    d = root / DIR
    d.mkdir(parents=True, exist_ok=True)
    action = json.dumps({"op": "research.close", "params": {"sizing": sizing}}, sort_keys=True)
    (d / f"{run_id}.workflow.mjs").write_text(f"// {MARKER}\nconst NEXT_ACTION = {action};\n", encoding="utf-8")
    (d / f"{run_id}.workflow.mjs.emitted.json").write_text("{}", encoding="utf-8")
    if finished:
        (d / run_id).mkdir(exist_ok=True)
        (d / run_id / "finished.md").write_text("done", encoding="utf-8")


def _ask_run(root):
    (root / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": "sizing-object", "intent": "research it", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "research it"}],
    }
    (root / SIZING).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(root, ASK)
    rl.bind(root, ASK, SIZING, ask_text="a raw ask")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _call(repo, run_id=RID, **params):
    return rl._handler({"run_id": run_id, **params}, repo_root=repo)


@pytest.mark.parametrize("key", ["note", "verbatim"])
@pytest.mark.parametrize(
    "text",
    [
        "SendMessage each settled answer to example-game-workbench-repo-80 immediately.",
        "relay the finding to the peer session as soon as it settles",
        "Notify peer 80 when B is done",
    ],
)
def test_peer_relay_is_refused_and_nothing_appended(repo, key, text):
    _emit(repo)
    reply = _call(repo, **{key: text})
    assert "stages cannot message peers" in reply["error"] and "delivered" not in reply
    assert not (repo / DIR / RID / "revisions.md").exists()


@pytest.mark.parametrize(
    "text", ["narrow to cell A", "also cover the peer-reviewed sources", "write the findings to scratch/out.md"]
)
def test_ordinary_scope_notes_still_deliver(repo, text):
    _emit(repo)
    reply = _call(repo, note=text)
    assert reply["delivered"] is True
    assert text in (repo / reply["revisions"]).read_text(encoding="utf-8")


def test_ask_id_resolves_to_live_pipeline_run(repo):
    _emit(repo)
    _ask_run(repo)
    reply = _call(repo, ASK, note="narrow to cell A")
    assert reply["delivered"] is True and reply["run_live"] is True
    assert reply["run_id"] == RID and reply["requested_run_id"] == ASK
    assert "narrow to cell A" in (repo / DIR / RID / "revisions.md").read_text(encoding="utf-8")


def test_peer_relay_to_ask_id_is_refused_too(repo):
    _emit(repo)
    _ask_run(repo)
    reply = _call(repo, ASK, note="SendMessage peer 80 each answer")
    assert "stages cannot message peers" in reply["error"]
    assert not (repo / DIR / RID / "revisions.md").exists()


def test_ask_id_with_finished_pipeline_keeps_ledger_path(repo):
    _emit(repo, finished=True)
    _ask_run(repo)
    reply = _call(repo, ASK, verbatim="also cover Windows")
    assert "delivered" not in reply and reply["run_id"] == ASK
    assert not (repo / DIR / RID / "revisions.md").exists()


def test_ask_id_with_other_sizing_pipeline_keeps_ledger_path(repo):
    _emit(repo, sizing="state/sizings/other.yaml")
    _ask_run(repo)
    reply = _call(repo, ASK, verbatim="also cover Windows")
    assert "delivered" not in reply and reply["run_id"] == ASK


def test_ask_id_with_ledger_flag_does_not_redirect(repo):
    _emit(repo)
    _ask_run(repo)
    reply = _call(repo, ASK, all_of_pm_ask=True)
    assert "delivered" not in reply and reply["run_id"] == ASK
    assert not (repo / DIR / RID / "revisions.md").exists()
