"""halt_resume_build: the halt-time producer writes the record the inline resume path would have computed."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, halt_resume_build as hrb, halt_resume_record as hrr, halted_run, op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

REL = "state/sizings/2026-10-16-refire-c2.yaml"
RUN = "warp-2026-10-16-refire-c2-20260101T000000-aaaaaa"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
BASE = "a" * 40


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
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


def _halt(repo, monkeypatch):
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: None)
    halted_run.write_halt_marker(record_dir(repo, RUN), halted="criterion", sizing_rel=REL, run_base_sha=BASE)


def test_write_halt_marker_schedules_the_producer_through_the_spawner_seam(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: calls.append((root, run_id)))
    halted_run.write_halt_marker(record_dir(repo, RUN), halted="criterion", sizing_rel=REL)
    assert calls == [(repo, RUN)]


def test_the_stored_script_is_a_top_level_run_that_keeps_its_stage_reader(repo, monkeypatch):
    """The hook returns the stored script as the emit result (a top-level start), never as a launcher child, so it
    keeps the one-level `workflow()` stage reader a launcher child must not hold."""
    _halt(repo, monkeypatch)
    stored = hrr.read(record_dir(repo, RUN), 1000) or hrb.build(repo, RUN)
    script = (repo / stored["composed_script_rel"]).read_text(encoding="utf-8")
    assert "workflow({ scriptPath: reader })" in script


def test_a_halt_without_a_sizing_schedules_nothing(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: calls.append(run_id))
    halted_run.write_halt_marker(record_dir(repo, RUN), halted="criterion", sizing_rel=None)
    assert calls == []


def test_build_writes_a_record_whose_script_and_verdict_equal_the_inline_path(repo, monkeypatch):
    _halt(repo, monkeypatch)
    rd = record_dir(repo, RUN)
    record = hrb.build(repo, RUN)
    stored = hrr.read(rd, 1000)
    assert stored == json.loads(json.dumps(record))
    script = (repo / stored["composed_script_rel"]).read_text(encoding="utf-8")
    assert script and "phases" in script
    inline = op._gate_sizing_at_emit(repo, REL, [])
    assert stored["gate_verdict"]["ask_sizing"] == json.loads(json.dumps(inline))
    expected = {**hrr.fingerprint_of(repo, None, REL, None), "halt_head": stored["fingerprint"]["halt_head"]}
    assert {k: stored["fingerprint"][k] for k in expected} == expected
    assert stored["fingerprint"]["sizing_sha256"]
    assert stored["fingerprint"]["landed_state_sha"] == stored["landed_state_sha"]
    assert stored["retry_not_before"] is None


def test_build_records_a_gate_refusal_without_a_script(repo, monkeypatch):
    _halt(repo, monkeypatch)
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["exit_criterion"] = {"statement": "done", "accepted": None}
    doc["interaction_mode"] = "pm"
    doc["estimate"] = {"tshirt": "M", "provisional": False}
    (repo / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    record = hrb.build(repo, RUN)
    assert record["composed_script_rel"] is None
    assert record["gate_verdict"].get("refused")
    assert record["gate_verdict"]["halt"]["kind"]
    import time

    assert hrr.in_backoff(record, time.time())
    assert not hrr.in_backoff(record, time.time() + hrb.REFUSAL_BACKOFF_S + 1)
    rd = record_dir(repo, RUN)
    current = hrr.fingerprint_for_run(repo, rd, REL, record["resume_plan_rel"])
    assert hrr.matches(hrr.read(rd, 1000), current)


def test_build_ignores_a_run_that_is_not_halted(repo, monkeypatch):
    _halt(repo, monkeypatch)
    halted_run.mark_resumed(record_dir(repo, RUN))
    assert hrb.build(repo, RUN) is None
    assert hrr.read(record_dir(repo, RUN), 1000) is hrr.MISSING


SID = "7f8efcbc-1111-4222-8333-444455556666"


def _stage(repo, session_id):
    rd = record_dir(repo, RUN)
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "run-stage.json").write_text(json.dumps({"session_id": session_id, "stage": "halted"}), encoding="utf-8")


def test_build_threads_the_run_records_canonical_session_id_into_script_and_receipt(repo, monkeypatch):
    _halt(repo, monkeypatch)
    _stage(repo, SID)
    record = hrb.build(repo, RUN)
    script = repo / record["composed_script_rel"]
    text = script.read_text(encoding="utf-8")
    assert f"const _SESSION_ID = '{SID}';" in text or f'const _SESSION_ID = "{SID}";' in text
    receipt = json.loads(script.with_name(script.name + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["session_id"] == SID


@pytest.mark.parametrize("bad", [None, "", "not-a-uuid"])
def test_build_omits_an_absent_or_non_canonical_session_id(repo, monkeypatch, bad):
    _halt(repo, monkeypatch)
    _stage(repo, bad)
    record = hrb.build(repo, RUN)
    script = repo / record["composed_script_rel"]
    text = script.read_text(encoding="utf-8")
    assert "const _SESSION_ID = '';" in text or 'const _SESSION_ID = "";' in text
    assert "not-a-uuid" not in text
    receipt = json.loads(script.with_name(script.name + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["session_id"] == ""


def _spy_compose(monkeypatch):
    seen = []
    real = ask_compose.compose_ask_script
    monkeypatch.setattr(ask_compose, "compose_ask_script", lambda **kw: (seen.append(kw.get("stop_after")), real(**kw))[1])
    return seen


def test_build_composes_with_the_lineage_stop_after_and_records_it(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_contract import STOP_AFTER_PLAN, write_stop_after_marker

    parent = record_dir(repo, "ask-parent")
    parent.mkdir(parents=True)
    write_stop_after_marker(parent, STOP_AFTER_PLAN)
    rd = record_dir(repo, RUN)
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"chain_parent": "ask-parent"}))
    seen = _spy_compose(monkeypatch)
    _halt(repo, monkeypatch)
    record = hrb.build(repo, RUN)
    assert seen == [STOP_AFTER_PLAN]
    assert record["stop_after"] == STOP_AFTER_PLAN and record["fingerprint"]["stop_after"] == STOP_AFTER_PLAN
    script = (repo / record["composed_script_rel"]).read_text(encoding="utf-8")
    assert "parked after plan: stop_after=plan" in script


def test_build_without_a_parked_lineage_composes_with_no_stop_after(repo, monkeypatch):
    seen = _spy_compose(monkeypatch)
    _halt(repo, monkeypatch)
    record = hrb.build(repo, RUN)
    assert seen == [None] and record["stop_after"] is None
    assert "parked after plan: stop_after=plan" not in (repo / record["composed_script_rel"]).read_text(encoding="utf-8")


def test_a_deliberate_resume_of_a_parked_run_stages(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PARKED, STOP_AFTER_PLAN, write_stop_after_marker

    rd = record_dir(repo, RUN)
    rd.mkdir(parents=True)
    write_stop_after_marker(rd, STOP_AFTER_PLAN)
    seen = _spy_compose(monkeypatch)
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: None)
    halted_run.write_halt_marker(rd, halted=HALT_PARKED, sizing_rel=REL, run_base_sha=BASE)
    record = hrb.build(repo, RUN)
    assert seen == [None] and record["stop_after"] is None


def test_main_rejects_a_wrong_argument_count():
    assert hrb.main(["only-one"]) == 2


def test_build_writes_a_receipt_the_fire_guards_accept_and_a_bare_script_is_still_refused(repo, monkeypatch):
    import hashlib

    from coordinator_core.hooks import allow_emitted_workflow_fire as allow
    from coordinator_core.hooks.block_workflow_foreign_emission import evaluate

    _halt(repo, monkeypatch)
    record = hrb.build(repo, RUN)
    script = repo / record["composed_script_rel"]
    receipt = json.loads(script.with_name(script.name + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["sha256"] == hashlib.sha256(script.read_bytes()).hexdigest()
    payload = {"tool_name": "Workflow", "tool_input": {"scriptPath": str(script)}, "cwd": str(repo)}
    assert allow._decide(payload) is not None
    assert evaluate(payload, env={}) == {}
    hand = repo / "hand.workflow.mjs"
    hand.write_text("await agent({prompt: 'x'});", encoding="utf-8")
    hand_payload = {**payload, "tool_input": {"scriptPath": str(hand)}}
    assert allow._decide(hand_payload) is None
    assert evaluate(hand_payload, env={}) != {}


@pytest.mark.twin_needs_script
def test_recompose_script_runs_the_real_build_and_ensure_twin_stages_from_it(repo, monkeypatch):
    import hashlib

    from coordinator_core.ops.dispatch_emit import launcher_staged_twin as twin

    _halt(repo, monkeypatch)
    text, why = hrb.recompose_script(repo, RUN)
    assert why == "" and text and (record_dir(repo, RUN) / hrb.COMPOSED_SCRIPT_FILE).read_text(encoding="utf-8") == text
    assert not (repo / "scratch" / "warp" / f"{RUN}.workflow.mjs").exists()
    raw = b'{"rows":[]}'
    handle = {"path": f"state/warp/{RUN}/manifest.json", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    rel = twin.ensure_twin(repo, handle["path"], raw, handle, max_bytes=10_000_000, refuse=ValueError)
    assert rel is None  # the composed top-level script has no inline slot: recomposed, read, and nothing refused
    assert (record_dir(repo, RUN) / hrb.COMPOSED_SCRIPT_FILE).is_file()


def test_recompose_script_names_a_run_with_no_halt_marker(repo):
    text, why = hrb.recompose_script(repo, RUN)
    assert text is None and why
