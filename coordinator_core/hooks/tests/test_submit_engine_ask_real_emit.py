"""`hooks.submit_engine_ask.evaluate` through the real `dispatch.emit` on a tmp repo: the rewrite
envelope, the run records, and an exact spawn count (the submission starts no process).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core import warp_fire
from coordinator_core.benchmarks.budget import load_manifest
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as pm_turns
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_TEXT_FILE, RELAYED_ASK_FILE, RUN_DIR_ROOT, record_dir

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REL = "state/sizings/2026-10-13-real-emit.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


def _budget(route: str) -> int:
    return load_manifest()["overrides"]["hooks.submit_engine_ask"]["spawn_count_budget"][route]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


def _put_sizing(root: Path) -> None:
    doc = {
        "schema": "sizing-object", "name": "real emit fixture", "intent": "exercise the real emit",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-real-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-13", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make the real emit verbatim"}],
    }
    (root / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _payload(root: Path, args) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-real-emit",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


def _run_dirs(root: Path) -> list[Path]:
    return sorted(p for p in (root / RUN_DIR_ROOT).iterdir() if p.is_dir())


def _assert_rewrite(env: dict) -> dict:
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    updated = hso["updatedInput"]
    assert Path(updated["scriptPath"]).is_file()
    assert updated.get("args")
    return updated


def _all_git_run(spawns) -> None:
    assert all(s.origin == "run_git" for s in spawns), [(s.origin, s.argv) for s in spawns]


def test_sizing_submission_through_the_real_emit(repo, monkeypatch):
    _put_sizing(repo)
    with _count_spawns_attributed(monkeypatch) as spawns:
        env = mod.evaluate(_payload(repo, {"sizing": REL}))

    _assert_rewrite(env)
    (run_dir,) = _run_dirs(repo)
    assert (run_dir / "submission.json").is_file()
    tokens = [p for p in repo.rglob("*") if p.is_file() and REL in p.read_text(encoding="utf-8", errors="ignore")]
    assert tokens, "no record names the sizing in repo-relative form"
    assert len(spawns) == _budget("sizing_submission"), [s.argv for s in spawns]
    _all_git_run(spawns)


def test_raw_ask_submission_through_the_real_emit(repo, monkeypatch):
    sid = "sess-real-emit"
    pm_turns.log_path(sid, str(repo)).parent.mkdir(parents=True)
    assert pm_turns.append_turn("add the widget", session_id=sid, cwd=str(repo))["ok"]
    with _count_spawns_attributed(monkeypatch) as spawns:
        env = mod.evaluate(_payload(repo, {"ask": "add the widget", "source": "pm"}))

    _assert_rewrite(env)
    (run_dir,) = _run_dirs(repo)
    assert (run_dir / "submission.json").is_file()
    assert (record_dir(repo, run_dir.name) / ASK_TEXT_FILE).read_text(encoding="utf-8") == "add the widget"
    assert len(spawns) == _budget("raw_ask_submission"), [s.argv for s in spawns]
    _all_git_run(spawns)


def test_asks_submission_through_the_real_fanout_and_emit(repo, monkeypatch):
    from coordinator_core.ops import baton_pm_turns as PT
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
    from coordinator_core.session_baton import store

    hub = repo.parent / "hub" / "sess-real-emit"
    hub.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: repo.parent / "hub" / s)
    assert PT.append_turn("do these budget-many separable jobs", session_id="sess-real-emit")["ok"]
    count = 4
    asks = [{"ask": f"job {i}", "label": f"job-{i}", "writes": [f"pkg/m{i}.py"]} for i in range(count)]

    with _count_spawns_attributed(monkeypatch) as spawns:
        env = mod.evaluate(_payload(repo, {"asks": asks}))

    _assert_rewrite(env)
    (parent_dir,) = (repo / "scratch" / "warp").glob("ask-parent-*")
    record = contract.ParentRecord.from_json(
        json.loads((parent_dir / "parent.json").read_text(encoding="utf-8"))
    )
    assert len(record.runs) == count
    assert len(spawns) == _budget("asks_submission"), [s.argv for s in spawns]
    _all_git_run(spawns)


def test_relayed_ask_submission_writes_relayed_ask_beside_no_ask_md(repo, monkeypatch):
    with _count_spawns_attributed(monkeypatch) as spawns:
        env = mod.evaluate(_payload(repo, {"ask": "RELAYED-MARK add the widget", "source": "relayed"}))

    _assert_rewrite(env)
    (run_dir,) = _run_dirs(repo)
    assert (run_dir / "submission.json").is_file()
    records = record_dir(repo, run_dir.name)
    assert (records / RELAYED_ASK_FILE).read_text(encoding="utf-8") == "RELAYED-MARK add the widget"
    assert not (records / ASK_TEXT_FILE).exists()
    assert not (run_dir / ASK_TEXT_FILE).exists()
    assert len(spawns) == _budget("raw_ask_submission"), [s.argv for s in spawns]


def test_ask_with_inline_brief_reaches_the_real_emit(repo):
    env = mod.evaluate(_payload(repo, {"ask": "add the widget", "source": "pm", "brief": "BRIEF-MARK prefer small"}))
    _assert_rewrite(env)
    (run_dir,) = _run_dirs(repo)
    assert any(
        "BRIEF-MARK" in p.read_text(encoding="utf-8", errors="ignore")
        for p in repo.rglob("*")
        if p.is_file() and run_dir.name in str(p)
    )


def test_ask_with_a_missing_brief_file_is_denied_by_the_real_emit(repo):
    env = mod.evaluate(_payload(repo, {"ask": "add the widget", "source": "pm", "brief_file": "nope.md"}))
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "nope.md" in hso["permissionDecisionReason"]
