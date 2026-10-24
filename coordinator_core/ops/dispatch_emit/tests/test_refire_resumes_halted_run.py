"""A sizing fire with no reserved run id rebinds the sizing's halted run instead of minting a new one."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import ask_compose, op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, write_halt_marker

REL = "state/sizings/2026-10-16-refire-c3.yaml"
OLDER = "warp-2026-10-16-refire-c3-20260101T000000-aaaaaa"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
HEAD = "c" * 40
HALTED_BASE = "a" * 40


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: BLITZ)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-refire-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-16", "mode": "pm"}},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return tmp_path


def _halt(repo, run_id=OLDER, run_base_sha=None):
    rl.start(repo, run_id)
    rl.bind(repo, run_id, REL, ask_text="the ask")
    write_halt_marker(record_dir(repo, run_id), halted="criterion", sizing_rel=REL, run_base_sha=run_base_sha)


def _marker(repo, run_id=OLDER):
    return json.loads((record_dir(repo, run_id) / HALTED_FILE).read_text(encoding="utf-8"))


def _fire(repo, **extra):
    return op._dispatch_emit({"ask": True, "sizing_path": REL, **extra}, repo_root=repo)


def test_a_halted_marker_makes_the_fire_answer_that_run_and_reads_resumed(repo):
    _halt(repo)
    assert _fire(repo)["run_id"] == OLDER
    assert _marker(repo)["status"] == "resumed"


def test_no_marker_mints_a_fresh_id(repo):
    reply = _fire(repo)
    assert reply["run_id"].startswith("warp-2026-10-16-refire-c3-")
    assert reply["run_id"] != OLDER


def test_a_resumed_marker_is_not_rebound_twice(repo):
    _halt(repo)
    first = _fire(repo)["run_id"]
    assert _fire(repo)["run_id"] != first


def test_start_keeps_the_sizing_and_ors_all_of_pm_ask(repo):
    _halt(repo)
    rl._handler({"run_id": OLDER, "all_of_pm_ask": True}, repo_root=repo)
    rl.start(repo, OLDER, all_of_pm_ask=False)
    run = rl.read_run(record_dir(repo, OLDER))
    assert run["all_of_pm_ask"] is True
    assert run["sizing_rel"] == REL


def test_a_rebound_refire_with_the_same_ask_adds_no_register_row(repo):
    _halt(repo)
    before = rr.read_register((repo / REL).read_text(encoding="utf-8")).rows
    _fire(repo)
    after = rr.read_register((repo / REL).read_text(encoding="utf-8")).rows
    assert len(after) == len(before)


def test_the_rebound_compose_receives_the_recorded_run_base_not_head(repo, monkeypatch):
    import coordinator_core.git.git_state as git_state

    _halt(repo)
    recorded = repo / op._run_base_rel(OLDER)
    recorded.parent.mkdir(parents=True, exist_ok=True)
    recorded.write_text("a" * 40 + "\n", encoding="utf-8")
    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    seen = {}
    real = ask_compose.compose_ask_script

    def spy(*a, **k):
        seen["base"] = k.get("run_base_sha")
        return real(*a, **k)

    monkeypatch.setattr(ask_compose, "compose_ask_script", spy)
    _fire(repo)
    assert seen["base"] == "a" * 40


def test_a_fresh_sizing_fire_records_head_as_its_run_base(repo, monkeypatch):
    import coordinator_core.git.git_state as git_state

    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    run_id = _fire(repo)["run_id"]
    assert op._recorded_run_base(repo, run_id) == HEAD


def _emit_by(repo, session, live, monkeypatch, run_id=OLDER):
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path

    script = repo / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("// peer emission\n", encoding="utf-8", newline="")
    receipt = emission_receipt_path(script)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"session_id": session}), encoding="utf-8")
    import coordinator_core.session.liveness as liveness

    monkeypatch.setattr(liveness, "session_live", lambda s: live)
    return script, receipt


def test_a_dead_owner_halted_run_diverts_to_a_fresh_path_named_in_the_result(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_contract import (
        OUTPUT_PATH_REDIRECT_KEY,
        OUTPUT_PATH_REDIRECT_REASONS,
    )

    import coordinator_core.git.git_state as git_state

    monkeypatch.setattr(git_state, "head_sha", lambda *_a, **_k: HEAD)
    _halt(repo, run_base_sha=HALTED_BASE)
    script, receipt = _emit_by(repo, "dead-peer", False, monkeypatch)
    before = (script.read_bytes(), receipt.read_bytes())
    reply = _fire(repo)
    assert reply["run_id"] != OLDER
    redirect = reply[OUTPUT_PATH_REDIRECT_KEY]
    assert redirect["halted_run"] == OLDER and redirect["prior_session"] == "dead-peer"
    assert redirect["reason"] in OUTPUT_PATH_REDIRECT_REASONS
    assert redirect["from"].endswith(f"{OLDER}.workflow.mjs")
    assert redirect["to"].endswith(f"{reply['run_id']}.workflow.mjs")
    assert (repo / redirect["to"]).is_file()
    assert (script.read_bytes(), receipt.read_bytes()) == before
    assert op._recorded_run_base(repo, reply["run_id"]) == HALTED_BASE != HEAD


def test_a_live_peer_halted_run_still_refuses(repo, monkeypatch):
    _halt(repo)
    _emit_by(repo, "live-peer", True, monkeypatch)
    with pytest.raises(op.ForeignEmissionError):
        _fire(repo)


def test_a_live_peer_refusal_names_only_remedies_the_ask_route_accepts(repo, monkeypatch):
    _halt(repo)
    _emit_by(repo, "live-peer", True, monkeypatch)
    with pytest.raises(op.ForeignEmissionError) as excinfo:
        _fire(repo)
    msg = str(excinfo.value)
    assert "force" not in msg and "output_path" not in msg
    assert "re-fire" in msg


def test_a_halted_run_refire_lands_over_its_own_receiptless_stale_emission(repo, monkeypatch):
    _halt(repo)
    script, receipt = _emit_by(repo, "gone", False, monkeypatch)
    receipt.unlink()
    reply = _fire(repo)
    assert reply["run_id"] == OLDER
    assert script.read_text(encoding="utf-8") != "// peer emission\n"


def test_a_dead_owner_halted_run_with_explicit_output_path_does_not_redirect(repo, monkeypatch):
    _halt(repo)
    _emit_by(repo, "dead-peer", False, monkeypatch)
    reply = _fire(repo, output_path=str(repo / "mine.mjs"))
    assert reply["run_id"] == OLDER
    assert "output_path_redirect" not in reply


def test_a_reserved_run_id_fire_does_not_rebind(repo):
    _halt(repo)
    with pytest.raises(ValueError):
        _fire(repo, reserved_run_id="nope")
    assert _marker(repo)["status"] == "halted"
