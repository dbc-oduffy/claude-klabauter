"""workflow.revise on a run whose fire record shows an exited worker refires at once; the ledger header
and rows name the origin the submission gave."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_fire, warp_live
from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.workflow_fire import fire

pytestmark = pytest.mark.complete

RUN = "ask-20261119T121630-1a80f7"
REL = "state/sizings/2026-11-19-dead-worker.yaml"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "install", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "install"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    registry = tmp_path / "fires"
    registry.mkdir()
    monkeypatch.setattr(fire, "_registry_dir", lambda _cwd=None: registry)
    monkeypatch.setattr(run_token, "is_live", lambda _r, run_id: run_id == RUN)
    monkeypatch.setattr(run_token, "live_records", lambda _r: iter(()))
    monkeypatch.setattr(warp_live, "runner_unproven", lambda _r, run_id: run_id == RUN)
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path


def _fire_record(repo: Path, fire_id: str, state: str, run_id: str = RUN) -> None:
    (repo / "fires" / f"{fire_id}.json").write_text(
        json.dumps({"fire_id": fire_id, "run_id": run_id, "state": state}), encoding="utf-8"
    )


def _revise(repo: Path) -> dict:
    return rl._handler({"run_id": RUN, "verbatim": "recovery note"}, repo_root=repo)


def test_an_exited_worker_refires_at_once_and_offers_no_recheck(repo: Path) -> None:
    _fire_record(repo, "f1", "exited")
    out = _revise(repo)
    assert out["run_live"] is False
    assert out["next"]["kind"] == "refire" and out["next"]["call"]
    assert "recheck" not in json.dumps(out["next"]) and "runner unproven" not in out["note"]


def test_a_running_worker_still_gets_the_recheck(repo: Path) -> None:
    _fire_record(repo, "f1", "running")
    assert _revise(repo)["next"]["kind"] == "recheck"


def test_a_resumed_fire_still_running_beside_an_exited_one_is_not_dead(repo: Path) -> None:
    _fire_record(repo, "f1", "exited")
    _fire_record(repo, "f2", "running")
    assert _revise(repo)["next"]["kind"] == "recheck"


def test_another_runs_exited_fire_does_not_kill_this_one(repo: Path) -> None:
    _fire_record(repo, "f1", "exited", run_id="ask-20261119T000000-ffffff")
    assert _revise(repo)["next"]["kind"] == "recheck"


def test_a_pm_ask_ledger_names_no_relay_origin(repo: Path) -> None:
    ledger = rl.read_ledger(repo, repo / RECORD_DIR_ROOT / RUN)
    assert ledger.items[-1].origin == "ask"
    header = rl.render(ledger, RUN)
    assert "origin `relayed`" not in header and "origin `ask`" in header


def test_a_relayed_ask_ledger_keeps_the_relay_header(repo: Path) -> None:
    run = "ask-20261119T130000-aaaaaa"
    rel = "state/sizings/2026-11-19-relayed.yaml"
    fresh = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    fresh.pop("requirement_register", None)
    (repo / rel).write_text(yaml.safe_dump(fresh, sort_keys=False), encoding="utf-8")
    rl.start(repo, run)
    rl.bind(repo, run, rel, relayed_text="an EM's relay")
    ledger = rl.read_ledger(repo, repo / RECORD_DIR_ROOT / run)
    assert any(i.origin == "relayed" for i in ledger.items)
    assert "origin `relayed`" in rl.render(ledger, run)


def test_a_given_pm_source_reaches_the_emit_and_survives_a_later_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setattr(hook, "_is_first_turn", lambda _p: False)
    seen: list = []

    def emit(params: dict, _root: Path) -> dict:
        seen.append(params)
        raise RuntimeError("stop after capture")

    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"ask": "the PM's words", "source": "pm"}},
        "session_id": "sess-1", "cwd": str(root),
    }
    out = hook.evaluate(payload, emit=emit)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seen and seen[0]["ask_source"] == "pm"
    seen.clear()
    payload["tool_input"]["args"] = {"ask": "unmarked"}
    out = hook.evaluate(payload, emit=emit)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert seen and seen[0]["ask_source"] == "relayed"
