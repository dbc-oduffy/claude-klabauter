"""Tests for hooks.nudge_em_turn_budget: fire-once pre-fire and post-landing advisories."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.hooks import nudge_em_turn_budget as nb  # noqa: E402
from coordinator_core.hooks import postuse_advisory_dispatch as pad  # noqa: E402
from coordinator_core.session import em_turn_count as etc  # noqa: E402
from coordinator_core.session import engine_route_state as ers  # noqa: E402

SID = "sess-turns-1"


class Env:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "repo"
        (self.root / ".git").mkdir(parents=True)
        self.transcript = tmp_path / "t.jsonl"
        self.transcript.write_bytes(b"")
        self.state = tmp_path / "hub" / SID / "em-turns.json"
        self.n = 0
        self.phase = ers.RoutePhase(ers.PHASE_UNFIRED)

    def turns(self, k: int, fire: bool = False) -> None:
        with open(self.transcript, "ab") as fh:
            for _ in range(k):
                self.n += 1
                content = [{"type": "text", "text": "x"}]
                if fire:
                    content = [{
                        "type": "tool_use", "name": "Workflow",
                        "input": {"name": "coordinator:engine-ask"},
                    }]
                    fire = False
                rec = {
                    "type": "assistant",
                    "message": {"role": "assistant", "id": f"m{self.n}", "model": "x", "content": content},
                }
                fh.write(json.dumps(rec, separators=(",", ":")).encode() + b"\n")

    def params(self, **extra) -> dict:
        p = {
            "session_id": SID, "transcript_path": str(self.transcript),
            "cwd": str(self.root), "hook_event_name": "PostToolUse",
        }
        p.update(extra)
        return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setattr(etc, "state_path", lambda root, sid: e.state)
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: e.phase)
    monkeypatch.delenv("COORDINATOR_HOOK_NUDGE_EM_TURN_BUDGET_DISABLED", raising=False)
    return e


def test_eleventh_prefire_turn_signals_once(env):
    env.turns(10)
    assert nb.advisory_text(env.params()) == ""
    env.turns(1)
    text = nb.advisory_text(env.params())
    assert "11 turns" in text and "bar 10" in text
    env.turns(1)
    assert nb.advisory_text(env.params()) == ""


def test_prefire_advisory_arms_the_session_latch(env):
    from coordinator_core.hooks._em_write_budget import advised_deny_envelope

    (env.root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (env.root / ".git" / "refs" / "heads").mkdir(parents=True)
    (env.root / ".git" / "refs" / "heads" / "main").write_text("a" * 40 + "\n")
    env.turns(10)
    assert nb.advisory_text(env.params()) == ""
    assert advised_deny_envelope(str(env.root), SID) is None
    env.turns(1)
    assert nb.advisory_text(env.params()) != ""
    assert advised_deny_envelope(str(env.root), SID) is not None


def test_fired_at_turn_three_never_prefire_signals(env, monkeypatch):
    env.turns(2)
    env.turns(1, fire=True)
    env.phase = ers.RoutePhase(ers.PHASE_IN_FLIGHT, "run-1")
    env.turns(12)
    assert nb.advisory_text(env.params()) == ""


def test_sixth_post_landing_turn_signals_once(env):
    env.turns(1, fire=True)
    env.phase = ers.RoutePhase(ers.PHASE_IN_FLIGHT, "run-1")
    assert nb.advisory_text(env.params()) == ""
    env.phase = ers.RoutePhase(ers.PHASE_LANDED_GREEN, "run-1")
    assert nb.advisory_text(env.params()) == ""
    env.turns(5)
    assert nb.advisory_text(env.params()) == ""
    env.turns(1)
    text = nb.advisory_text(env.params())
    assert "6 turns since the run landed" in text
    env.turns(1)
    assert nb.advisory_text(env.params()) == ""


def test_post_landing_text_routes_gap_through_revise():
    text = nb.post_landing_text(6, 5)
    assert text.startswith("[em-turn-budget]")
    assert "workflow.revise" in text
    assert "Read the digest" not in text


def test_second_landed_run_signals_again(env):
    env.turns(1, fire=True)
    for run in ("run-1", "run-2"):
        env.phase = ers.RoutePhase(ers.PHASE_LANDED_GREEN, run)
        assert nb.advisory_text(env.params()) == ""
        env.turns(6)
        assert "since the run landed" in nb.advisory_text(env.params())
    assert json.loads(env.state.read_text())["landings"]["run-1"]["signalled"] is True


def test_halted_never_signals(env):
    env.turns(1, fire=True)
    env.phase = ers.RoutePhase(ers.PHASE_HALTED, "run-1")
    env.turns(20)
    assert nb.advisory_text(env.params()) == ""


def test_agent_id_never_signals(env):
    env.turns(30)
    assert nb.advisory_text(env.params(agent_id="a1")) == ""
    assert not env.state.exists()


def test_kill_switch_payload_env(env):
    env.turns(30)
    assert nb.advisory_text(env.params(env={nb._KILL_SWITCH: "1"})) == ""


def test_corrupt_state_resets_at_tail_and_stays_silent(env):
    env.turns(30)
    env.state.parent.mkdir(parents=True)
    env.state.write_text("{not json")
    assert nb.advisory_text(env.params()) == ""
    assert json.loads(env.state.read_text())["turns"] == 0


def test_unreadable_transcript_is_silent(env):
    p = env.params(transcript_path=str(env.root / "missing.jsonl"))
    assert nb.advisory_text(p) == ""


def test_under_bar_never_computes_phase(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("phase computed under the bar")

    monkeypatch.setattr(ers, "session_route_phase", boom)
    env.turns(10)
    assert nb.advisory_text(env.params()) == ""


def test_op_spawns_no_process_and_emits_context(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    env.turns(11)
    out = asyncio.run(nb._handler(env.params()))
    assert "em-turn-budget" in out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert asyncio.run(nb._handler(env.params())) == {}


def test_postuse_dispatch_merges_turn_advisory(env, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    env.turns(11)
    out = asyncio.run(pad._handler(env.params(tool_name="Write", agent_id="")))
    assert "[em-turn-budget]" in out["hookSpecificOutput"]["additionalContext"]


def test_task_finished_in_tail_matches_terminal_status_only(tmp_path):
    t = tmp_path / "tail.jsonl"
    note = "<task-notification><task-id>w1</task-id><status>{}</status></task-notification>\n"
    t.write_text(note.format("running"), encoding="utf-8")
    assert pad._task_finished_in_tail(str(t), "w1") is False
    t.write_text(note.format("completed"), encoding="utf-8")
    assert pad._task_finished_in_tail(str(t), "w1") is True
    assert pad._task_finished_in_tail(str(t), "w2") is False


def test_postuse_dispatch_under_bar_has_no_turn_advisory(env, monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    env.turns(3)
    out = asyncio.run(pad._handler(env.params(tool_name="Write")))
    assert "[em-turn-budget]" not in json.dumps(out)
