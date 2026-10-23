"""land_run: a refire whose met row landed in the prior fire's own checkpoints closes through the land body, never ledger-unmet."""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261017T000000-abc123"
REL = "state/sizings/2026-10-17-resume.yaml"
PLAN = "docs/plans/2026-10-17-incident.md"
PROSE = "Incident test and write_back refusal pass; TestScaffoldedStatus tests pass"


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


@pytest.fixture
def incident(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "deliver the owed work"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "twin.py").write_text("# twin\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "prod.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "prod.py")
    _git(tmp_path, "commit", "-q", "-m", "checkpoint(wave 0): 1 rows\n\nCheckpoint-Plan: " + PLAN)
    checkpoint = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "note.txt").write_text("halt\n", encoding="utf-8")
    _git(tmp_path, "add", "note.txt")
    _git(tmp_path, "commit", "-q", "-m", "halt record")
    halt = _git(tmp_path, "rev-parse", "HEAD")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    run_dir = tmp_path / RECORD_DIR_ROOT / RUN
    manifest = run_dir / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    (tmp_path / "twin.py").write_text("# twin\n# reviewer comment\n", encoding="utf-8")
    return tmp_path, run_dir, manifest, checkpoint, halt


def _gate(incident, met_by):
    root, run_dir, manifest, _, halt = incident
    verdict = {"items": [{"id": "R1", "status": "met", "wired": True, "met_by": [met_by]}]}
    resume = {"run_id": "old", "halt_sha": halt, "blocking": ["R1"], "plan": PLAN}
    return land_run._ledger_gate(root, run_dir, verdict, manifest, [], {}, None, {}, resume, PLAN)[0]


def test_prose_met_by_closes_on_the_prior_checkpoint(incident):
    reply = _gate(incident, PROSE)
    assert reply["committed"] is True and reply["already_landed"] is True
    assert reply["sha"] == incident[3] and not reply["refused"]


def test_met_by_naming_an_unrelated_sha_still_refuses(incident):
    reply = _gate(incident, "deadbeef1234")
    assert reply["refused"] == "ledger-unmet"
    assert reply["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


CREDIT = {
    "committed": True, "sha": "a" * 40, "already_landed": True, "paths": ["prod.py"],
    "since_halt": {"landed_sha": "a" * 40}, "error": None, "refused": None, "close": None,
}


def _land(monkeypatch, inner_reply):
    def inner(root, repo_root, params, stranded, token_state):
        token_state["since_halt_close"] = CREDIT
        return inner_reply

    monkeypatch.setattr(land_run, "_land_inner", inner)
    return land_run._land_product(None, None, {}, {})


def test_nothing_left_to_stage_reports_the_credit_as_already_landed(monkeypatch):
    reply = _land(monkeypatch, {"committed": False, "nothing_to_commit": True})
    assert reply["committed"] is True and reply["already_landed"] is True and reply["sha"] == "a" * 40
    assert not reply["nothing_to_commit"] and not reply["refused"]


def test_a_fresh_commit_carries_the_reviewer_edit_and_the_credit(monkeypatch):
    reply = _land(monkeypatch, {"committed": True, "sha": "b" * 40, "paths": ["twin.py"]})
    assert reply["sha"] == "b" * 40 and "already_landed" not in reply
    assert reply["since_halt"] == {"landed_sha": "a" * 40}


def test_a_commit_refusal_is_not_masked_by_the_credit(monkeypatch):
    refused = {"committed": False, "error": "boom", "refused": "blob-drift"}
    assert _land(monkeypatch, refused)["refused"] == "blob-drift"
