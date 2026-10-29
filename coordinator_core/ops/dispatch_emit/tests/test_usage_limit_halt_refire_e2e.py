"""A run whose every leg died on the usage limit is released by the engine and resumed by a default-args refire."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import ask_compose, halted_run, op, run_token
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_RUNNING, parent_record_rel, status_rel
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

REL = "state/sizings/2026-10-29-usage-limit-e2e.yaml"
SCRIPT = "scratch/warp/run.workflow.mjs"
RUN = "warp-2026-10-29-usage-limit-e2e-20261029T000000-aaaaaa"
SESSION = "11111111-2222-3333-4444-555555555555"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
FIXTURE = json.loads(
    (Path(__file__).parents[2] / ".." / "workflow_watch" / "tests" / "fixtures" / "usage_limit_hit.json")
    .resolve().read_text(encoding="utf-8")
)
PAST_RESET = 1000000000


def _limit_turn() -> str:
    fixture = json.loads(json.dumps(FIXTURE))
    fixture["quotaLimits"]["resetsAt"] = PAST_RESET
    return json.dumps({
        "type": "assistant",
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your session limit"}]},
        "isApiErrorMessage": True,
        **fixture,
    })


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "state" / "sizings").mkdir(parents=True)
    managed, legacy = tmp_path / "managed", tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SESSION)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: BLITZ)
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-usage-e2e-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-29", "mode": "pm"}},
    }
    (root / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(root, RUN)
    rl.bind(root, RUN, REL, ask_text="the ask")
    run_token.mint(root, RUN, SCRIPT, session_id=SESSION, sizing_rel=REL)
    warp_live.record_submission(
        root, RUN, session_id=SESSION, transcript_path=None, script_path=SCRIPT, sizing_rel=REL
    )
    wf = tmp_path / "wf" / "run1"
    wf.mkdir(parents=True)
    monkeypatch.setattr(warp_live, "_run_transcript_dir", lambda *a, **k: str(wf))
    spawned = []
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda r, i: spawned.append(i))
    legs = [("a1", "leg-one"), ("a2", "leg-two")]
    (wf / "journal.jsonl").write_text(
        "".join(json.dumps({"type": "started", "agentId": a, "label": lbl}) + "\n" for a, lbl in legs),
        encoding="utf-8",
    )
    for agent_id, _ in legs:
        (wf / f"agent-{agent_id}.jsonl").write_text(_limit_turn() + "\n", encoding="utf-8")
    return root


def test_default_args_refire_after_the_engine_halt_resumes_the_same_run(repo):
    assert warp_live.live_run_for_sizing(repo, REL, session_id=SESSION) is None
    reply = op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=repo)
    assert reply["run_id"] == RUN
    marker = halted_run.read_strict(record_dir(repo, RUN))
    assert marker["halted"] == "usage_limit" and marker["status"] == "resumed"


def test_the_engine_writes_the_halt_with_no_agent_leg_and_releases_the_claim(repo):
    assert warp_live.live_run_for_sizing(repo, REL, session_id=SESSION) is None
    marker = halted_run.read_strict(record_dir(repo, RUN))
    assert marker["status"] == "halted" and marker["halted"] == "usage_limit"
    assert not (record_dir(repo, RUN) / "halt-commit.json").exists()


def _reserve(repo):
    path = repo / REL
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    parent = "warp-2026-10-29-usage-limit-parent-20261029T000000-cccccc"
    doc["em_analysis"] = {"ask_parent": f"{parent} {RUN}"}
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (repo / parent_record_rel(parent)).parent.mkdir(parents=True)
    (repo / parent_record_rel(parent)).write_text("{}\n", encoding="utf-8")
    (repo / status_rel(RUN)).parent.mkdir(parents=True, exist_ok=True)
    (repo / status_rel(RUN)).write_text(json.dumps({"run_id": RUN, "status": STATUS_RUNNING}), encoding="utf-8")


def test_reserved_run_site_admits_the_refire_once_released(repo):
    _reserve(repo)
    reply = op._dispatch_emit({"ask": True, "sizing_path": REL, "reserved_run_id": RUN}, repo_root=repo)
    assert reply["run_id"] == RUN


def test_reserved_run_site_still_refuses_while_a_leg_is_not_on_the_limit(repo):
    _reserve(repo)
    wf = next(Path(repo).parent.glob("wf/run1"))
    for name in ("agent-a1.jsonl", "agent-a2.jsonl"):
        (wf / name).write_text(json.dumps({"type": "assistant", "message": {"content": []}}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="already working"):
        op._dispatch_emit({"ask": True, "sizing_path": REL, "reserved_run_id": RUN}, repo_root=repo)
    assert halted_run.read_strict(record_dir(repo, RUN)) is None
