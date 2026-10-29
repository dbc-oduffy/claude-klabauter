"""Pins that the 'call engine-ask' nudges and denies never reach a subagent or workflow agent."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.hooks import guard_em_prefire_survey as gps  # noqa: E402
from coordinator_core.hooks import nudge_em_code_dispatch as ncd  # noqa: E402
from coordinator_core.hooks import nudge_em_turn_budget as ntb  # noqa: E402
from coordinator_core.hooks import _em_write_budget as leaf  # noqa: E402
from coordinator_core.hooks._em_write_budget import shell_write_budget  # noqa: E402
from coordinator_core.session import em_turn_count as etc  # noqa: E402
from coordinator_core.session import engine_route_state as ers  # noqa: E402

AGENT_IDS = ["a1", "wf-agent-7"]


def _base(tmp_path: Path, **extra) -> dict:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True, exist_ok=True)
    transcript = tmp_path / "t.jsonl"
    if not transcript.exists():
        transcript.write_bytes(b"")
    p = {
        "session_id": "sess-em-only",
        "transcript_path": str(transcript),
        "cwd": str(root),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root / "src" / "mod.py"), "old_string": "a", "new_string": "b"},
    }
    p.update(extra)
    return p


@pytest.fixture
def em_env(tmp_path, monkeypatch):
    """Sized, in-repo, unfired-route session so an EM payload reaches each nudge."""
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    for mod in (ncd, leaf):
        monkeypatch.setattr(mod, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
        monkeypatch.setattr(mod, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(etc, "state_path", lambda root, sid: tmp_path / "hub" / sid / "em-turns.json")
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(ers.PHASE_UNFIRED))
    return tmp_path


def _code_payload(tmp_path, **extra):
    return _base(
        tmp_path, hook_event_name="PreToolUse",
        tool_input={
            "file_path": "/repo/pkg/module.py",
            "old_string": "def f():\n    return 1",
            "new_string": "def f():\n    return compute_something(x, y)",
        },
        **extra,
    )


def _turns(tmp_path, k):
    lines = []
    for n in range(k):
        rec = {"type": "assistant", "message": {"role": "assistant", "id": f"m{n}", "model": "x",
                                                "content": [{"type": "text", "text": "x"}]}}
        lines.append(json.dumps(rec, separators=(",", ":")))
    (tmp_path / "t.jsonl").write_bytes(("\n".join(lines) + "\n").encode())


def _prefire_payload(tmp_path, **extra):
    marker = tmp_path / "repo" / ".git" / "coordinator-sessions" / "sess-em-only" / "em-role-asserted"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("1", encoding="utf-8")
    lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}, separators=(",", ":"))]
    for i in range(12):
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"c{i}", "name": "Read", "input": {"file_path": f"f{i}.py"}}]}}, separators=(",", ":")))
    (tmp_path / "t.jsonl").write_bytes(("\n".join(lines) + "\n").encode())
    return _base(
        tmp_path, hook_event_name="PreToolUse", tool_name="Read", tool_use_id="cur",
        tool_input={"file_path": "cur.py"},
        env={"COORDINATOR_SETTINGS_HOME": str(tmp_path / "sh")}, **extra,
    )


def test_code_dispatch_nudge_fires_for_em(em_env):
    assert "engine-ask" in json.dumps(ncd.op(_code_payload(em_env)))


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_code_dispatch_nudge_silent_for_agents(em_env, agent_id):
    assert ncd.op(_code_payload(em_env, agent_id=agent_id)) is None


def test_turn_budget_nudge_fires_for_em(em_env):
    _turns(em_env, 11)
    assert "engine-ask" in ntb.advisory_text(_base(em_env, hook_event_name="PostToolUse"))


def test_prefire_survey_deny_fires_for_em(em_env):
    assert gps.evaluate(_prefire_payload(em_env)) is not None


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_shell_write_budget_silent_for_agents(tmp_path, agent_id):
    p = _base(tmp_path, agent_id=agent_id)
    out = shell_write_budget(p, "sess-em-only", p["tool_input"]["file_path"], "x = 1\n", None)
    assert out is None


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_turn_budget_nudge_silent_for_agents(em_env, agent_id):
    _turns(em_env, 11)
    p = _base(em_env, agent_id=agent_id, hook_event_name="PostToolUse")
    assert ntb.advisory_text(p) == ""
    out = asyncio.run(ntb._handler(p))
    assert "engine-ask" not in json.dumps(out)


@pytest.mark.parametrize("agent_id", AGENT_IDS)
def test_prefire_survey_deny_silent_for_agents(em_env, agent_id):
    p = _prefire_payload(em_env, agent_id=agent_id)
    assert gps.evaluate(p) is None
    out = asyncio.run(gps._handler(p))
    assert "engine-ask" not in json.dumps(out)
