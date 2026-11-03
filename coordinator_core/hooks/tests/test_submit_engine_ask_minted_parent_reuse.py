"""A multi-ask resubmission after a hook that died post-mint reuses the minted-not-started parent; aged orphans are reaped."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run, minted_parent
from coordinator_core.ops.warp_run_deliverable import ABANDONED_AT_MINTED
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.fan_launcher import launcher_rel
from coordinator_core.session_baton import store

SID = "sess-minted-reuse"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


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
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("fix the parser and also rename the exporter, they are unrelated jobs", session_id=SID)["ok"]
    monkeypatch.setenv("COORDINATOR_AGENT_TYPE_HOST", "coordinator")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)

    def stub_emit(params: dict, repo_root: Path) -> dict:
        run_id = params["reserved_run_id"]
        return {"path": f"scratch/warp/{run_id}.workflow.mjs", "run_id": run_id, "fire_args": {}}

    monkeypatch.setattr(mod, "_default_emit", stub_emit)
    return root


def _asks(tag: str = "job") -> list[dict]:
    return [{"ask": f"{tag} {i}", "source": "pm", "label": f"j{i}", "writes": [f"pkg/m{i}.py"]} for i in range(3)]


def _submit(root: Path, asks: list[dict]) -> dict:
    return mod.evaluate({
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"asks": asks}},
        "session_id": SID,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    })


def _script(env: dict) -> str:
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    return env["hookSpecificOutput"]["updatedInput"]["scriptPath"]


def _parents(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "scratch" / "warp").glob("ask-parent-*") if p.is_dir())


def _runs(root: Path, parent_id: str) -> list[str]:
    record = json.loads((root / c.parent_record_rel(parent_id)).read_text(encoding="utf-8"))
    return [r["run_id"] for r in record["runs"]]


def _stage(root: Path, run_id: str, stage: str) -> None:
    """The run's progress record, as `dispatch.emit` leaves it once the run is minted."""
    run_dir = record_dir(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")


def _age(root: Path, seconds: float) -> None:
    for entry in (root / minted_parent.DIGEST_DIR_REL).glob("*.json"):
        data = json.loads(entry.read_text(encoding="utf-8"))
        data["minted_at"] = time.time() - seconds
        entry.write_text(json.dumps(data), encoding="utf-8")


def test_a_resubmission_after_a_post_mint_death_reuses_the_minted_launcher(repo):
    first = _script(_submit(repo, _asks()))
    second_env = _submit(repo, _asks())

    assert _script(second_env) == first
    assert len(_parents(repo)) == 1
    assert "Re-submission of " + _parents(repo)[0] in json.dumps(second_env)
    assert Path(first).is_file()


def test_a_parent_with_a_started_run_is_not_reused(repo):
    first = _script(_submit(repo, _asks()))
    runs = _runs(repo, _parents(repo)[0])
    _stage(repo, runs[0], "staged")
    _stage(repo, runs[1], "minted")

    second = _script(_submit(repo, _asks()))

    assert second != first
    assert len(_parents(repo)) == 2


def test_different_asks_mint_their_own_parent(repo):
    first = _script(_submit(repo, _asks("job")))
    assert _script(_submit(repo, _asks("other"))) != first
    assert len(_parents(repo)) == 2


def test_an_aged_all_minted_parent_is_reaped_by_the_next_submission(repo):
    _submit(repo, _asks("old"))
    old = _parents(repo)[0]
    old_runs = _runs(repo, old)
    old_launcher = repo / launcher_rel(old)
    assert old_launcher.is_file()
    for run_id in old_runs:
        _stage(repo, run_id, "minted")
    _age(repo, minted_parent.REUSE_TTL_S + 60)
    old_digest = next((repo / minted_parent.DIGEST_DIR_REL).glob("*.json")).stem

    _submit(repo, _asks("new"))

    assert old not in _parents(repo)
    assert not old_launcher.exists()
    for run_id in old_runs:
        marker = json.loads((record_dir(repo, run_id) / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
        assert marker["halted"] == ABANDONED_AT_MINTED
    assert len(_parents(repo)) == 1
    assert not (repo / minted_parent.digest_rel(old_digest)).exists()


def test_an_aged_entry_over_a_started_parent_keeps_the_run_files(repo):
    _submit(repo, _asks("old"))
    old = _parents(repo)[0]
    old_runs = _runs(repo, old)
    _stage(repo, old_runs[0], "staged")
    for run_id in old_runs[1:]:
        _stage(repo, run_id, "minted")
    _age(repo, minted_parent.REUSE_TTL_S + 60)

    _submit(repo, _asks("new"))

    assert old in _parents(repo)
    for run_id in old_runs:
        assert record_dir(repo, run_id).exists()
        assert not (record_dir(repo, run_id) / halted_run.HALTED_FILE).exists()
    named = [json.loads(p.read_text(encoding="utf-8"))["parent_id"] for p in (repo / minted_parent.DIGEST_DIR_REL).glob("*.json")]
    assert old not in named
