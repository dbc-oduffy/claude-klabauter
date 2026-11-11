"""dispatch.ask_gate stamps the bound run's plan phase, so a revise during M+ plan review is applied, not runner unproven."""

from __future__ import annotations

import json
import time

import pytest
import yaml

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import ask_gate, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

REL = "state/sizings/2026-10-01-gate.yaml"
RUN = "ask-20261111T105517-512525"
SCRIPT = "scratch/warp/x.mjs"
ACCEPTED = {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}
BATON = "state/handoffs/baton-fixture.md"


class _FakeDocNew:
    class SizingMintRefused(Exception):
        pass

    @staticmethod
    def mint_baton_from_sizing(sizing_rel, repo_root, baton=None):
        return {"id": "baton-fixture", "path": BATON}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_gate, "_load_doc_new", lambda: _FakeDocNew)
    monkeypatch.setattr(ask_gate, "main_worktree_root", lambda p: p)
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    doc = {
        "schema": "sizing-object", "name": "gate fixture", "intent": "exercise the gate",
        "estimate": {"tshirt": "M", "provisional": True}, "route": "plan", "detents": [], "fork": None,
        "xl_exit": None, "status": "sized", "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": ACCEPTED},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return tmp_path


def _minted_run(repo):
    """A run as the script leaves it before its first gate call: minted token, minted stage, no runner."""
    rl.start(repo, RUN)
    run_token.mint(repo, RUN, SCRIPT, session_id="s1", sizing_rel=REL)
    warp_live.record_submission(repo, RUN, session_id="s1", transcript_path=None, script_path=SCRIPT, sizing_rel=REL)


def _past_the_grace(monkeypatch):
    late = time.time() + warp_live.MINTED_RUNNER_GRACE_SECONDS + 60
    monkeypatch.setattr(warp_live.time, "time", lambda: late)


def _revise(repo):
    return rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=repo)


def _stage(repo) -> dict:
    return json.loads((record_dir(repo, RUN) / "run-stage.json").read_text(encoding="utf-8"))


def test_a_run_that_has_not_reached_the_gate_is_unproven(repo, monkeypatch):
    _minted_run(repo)
    _past_the_grace(monkeypatch)
    assert "plan_started_at" not in _stage(repo)
    assert warp_live.runner_unproven(repo, RUN) is True


def test_the_gate_stamps_plan_started_at_when_it_binds_the_run(repo):
    _minted_run(repo)
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert out["arm"] == "m_plus" and out["halt"] is None
    stage = _stage(repo)
    assert stage["stage"] == "minted" and stage["plan_started_at"]


def test_a_revise_during_m_plus_plan_review_is_applied_not_runner_unproven(repo, monkeypatch):
    _minted_run(repo)
    ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    _past_the_grace(monkeypatch)
    assert warp_live.runner_unproven(repo, RUN) is False
    out = _revise(repo)
    assert out["run_live"] is True
    assert "runner unproven" not in out.get("note", "") and out.get("next", {}).get("kind") != "recheck"


def test_a_refused_bind_stamps_nothing(repo, monkeypatch):
    _minted_run(repo)
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: "requirements: refused")
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert out["halt"]["kind"] == "refusal"
    assert "plan_started_at" not in _stage(repo)


def test_a_call_without_a_run_id_stamps_nothing(repo):
    _minted_run(repo)
    ask_gate._handler({"sizing_path": REL}, repo_root=repo)
    assert "plan_started_at" not in _stage(repo)
