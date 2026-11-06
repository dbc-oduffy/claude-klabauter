"""run_stage_lookup: SubagentStart payload -> {run_id, stage} through the Workflow journal."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import run_stage_lookup

SESSION = "sess-1111"


def _wf(tmp_path, name, events):
    wf = tmp_path / "slug" / SESSION / "subagents" / "workflows" / name
    wf.mkdir(parents=True)
    (wf / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return wf


def _started(agent_id, label):
    return {"type": "started", "key": "k", "agentId": agent_id, "label": label}


def test_journal_started_event_resolves_run_and_stage(tmp_path):
    _wf(tmp_path, "wf_aaa", [{"type": "launched"}, _started("a1", "plan-docs-check")])
    _wf(tmp_path, "wf_bbb", [_started("a2", "other")])
    payload = {"session_id": SESSION, "agent_id": "a1", "agent_type": "coordinator:docs-checker"}
    assert run_stage_lookup.read(payload, projects_root=tmp_path) == {
        "run_id": "wf_aaa",
        "stage": "plan-docs-check",
    }


def test_unknown_agent_and_missing_ids_resolve_to_none(tmp_path):
    _wf(tmp_path, "wf_aaa", [_started("a1", "x")])
    assert run_stage_lookup.read({"session_id": SESSION, "agent_id": "zz"}, projects_root=tmp_path) is None
    assert run_stage_lookup.read({"session_id": SESSION}, projects_root=tmp_path) is None
    assert run_stage_lookup.read({"agent_id": "a1"}, projects_root=tmp_path) is None
    assert run_stage_lookup.read({"session_id": "nope", "agent_id": "a1"}, projects_root=tmp_path) is None
    assert run_stage_lookup.read("not a payload", projects_root=tmp_path) is None


def test_record_round_trips_and_wins_over_journal(tmp_path):
    wf = _wf(tmp_path, "wf_aaa", [_started("a1", "from-journal")])
    run_stage_lookup.record(wf, "a1", "wf_aaa", "recorded-stage")
    run_stage_lookup.record(wf, "a2", "wf_aaa", "second")
    payload = {"session_id": SESSION, "agent_id": "a1"}
    assert run_stage_lookup.read(payload, projects_root=tmp_path) == {
        "run_id": "wf_aaa",
        "stage": "recorded-stage",
    }
    assert run_stage_lookup.read({"session_id": SESSION, "agent_id": "a2"}, projects_root=tmp_path)["stage"] == "second"


def test_record_to_missing_dir_does_not_raise(tmp_path):
    run_stage_lookup.record(tmp_path / "absent", "a1", "wf_x", "s")
