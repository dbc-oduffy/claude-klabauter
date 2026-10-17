"""run_locate: unique-or-None resolution of a run's wf_ dir on tmp_path trees."""

import json
from pathlib import Path

import pytest

from coordinator_core.usage import run_locate as rl


def _wf(session: Path, wf_id, agents, settled=()):
    """agents: {agent_id: (description, run_id or None)}."""
    d = session / "subagents" / "workflows" / wf_id
    d.mkdir(parents=True)
    events = [{"type": "launched"}]
    for aid, (desc, run_id) in agents.items():
        (d / f"agent-{aid}.meta.json").write_text(json.dumps({"description": desc}), encoding="utf-8")
        prompt = f"x coordinator-invoke dispatch.terminal_commit --params-file repo\\\\scratch/warp/{run_id}/terminal-commit-params.json" if run_id else "nothing"
        (d / f"agent-{aid}.jsonl").write_text(json.dumps({"message": {"content": prompt}}) + "\n", encoding="utf-8")
        events.append({"type": "started", "agentId": aid, "label": desc})
        if aid in settled:
            events.append({"type": "result", "agentId": aid})
    (d / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return d


@pytest.mark.parametrize("sid", ["f3dca0d5-7375-4313-83e8-a173943c2733", "C--weird_Shape-01"])
def test_unique_unsettled_commit_agent_resolves(tmp_path, sid):
    root = tmp_path / "projects"
    session = root / "E--proj" / sid
    target = _wf(session, "wf_a", {"a1": ("commit", "run-1")})
    _wf(session, "wf_b", {"b1": ("commit", "run-2")})
    assert rl.session_dir_for(sid, root) == session
    assert rl.locate_run_dir(session, "run-1") == target


def test_settled_agent_is_none(tmp_path):
    session = tmp_path / "s"
    _wf(session, "wf_a", {"a1": ("commit", "run-1")}, settled={"a1"})
    assert rl.locate_run_dir(session, "run-1") is None


def test_refire_picks_only_the_unsettled_agent(tmp_path):
    session = tmp_path / "s"
    target = _wf(session, "wf_a", {"a1": ("halt-commit", "run-1"), "a2": ("commit", "run-1")}, settled={"a1"})
    assert rl.locate_run_dir(session, "run-1") == target


def test_two_unsettled_matches_is_ambiguous(tmp_path):
    session = tmp_path / "s"
    _wf(session, "wf_a", {"a1": ("commit", "run-1")})
    _wf(session, "wf_b", {"b1": ("commit", "run-1")})
    assert rl.locate_run_dir(session, "run-1") is None


def test_wrong_description_and_absent_dir_are_none(tmp_path):
    session = tmp_path / "s"
    _wf(session, "wf_a", {"a1": ("work:C1", "run-1")})
    assert rl.locate_run_dir(session, "run-1") is None
    assert rl.locate_run_dir(session, "run-1", agent_descriptions=("work:C1",)) is not None
    assert rl.locate_run_dir(tmp_path / "missing", "run-1") is None


def test_path_shaped_ids_are_refused(tmp_path):
    assert rl.locate_run_dir(tmp_path, "../x") is None
    assert rl.session_dir_for("../x", tmp_path) is None


def test_projects_root_honours_env(tmp_path):
    assert rl.projects_root({"CLAUDE_CONFIG_DIR": str(tmp_path)}) == tmp_path / "projects"
