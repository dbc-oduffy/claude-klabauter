"""Tests for the hand-research read-count nudge, fired from the Stop dispatcher."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.hooks import nudge_em_research_scoping as nr  # noqa: E402
from coordinator_core.hooks import postuse_advisory_dispatch as pad  # noqa: E402
from coordinator_core.hooks import stop_dispatch as sd  # noqa: E402

SID = "sess-research-1"
PM_TEXT = "scope the corpus census for me"
MARK = "scoping research by hand"


class Env:
    def __init__(self, tmp_path: Path):
        self.transcript = tmp_path / "t.jsonl"
        self.transcript.write_bytes(b"")
        self.n = 0

    def _write(self, rec: dict) -> None:
        with open(self.transcript, "ab") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")).encode() + b"\n")

    def user(self, text: str = PM_TEXT) -> None:
        self._write({"type": "user", "message": {"role": "user", "content": text}})

    def tool(self, tool_name: str, **tool_input) -> None:
        self.n += 1
        self._write({
            "type": "assistant",
            "message": {
                "role": "assistant", "id": f"m{self.n}",
                "content": [{"type": "tool_use", "id": f"t{self.n}", "name": tool_name, "input": tool_input}],
            },
        })
        self._write({
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"t{self.n}", "content": "ok"},
            ]},
        })

    def reads(self, k: int, path: str = "docs/research/a.md") -> None:
        for _ in range(k):
            self.tool("Read", file_path=path)

    def payload(self, **extra) -> dict:
        p = {"cwd": "", "session_id": SID, "transcript_path": str(self.transcript)}
        p.update(extra)
        return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    sentinels = tmp_path / "tmp"
    sentinels.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sentinels))
    monkeypatch.delenv("COORDINATOR_HOOK_NUDGE_EM_RESEARCH_SCOPING_DISABLED", raising=False)
    monkeypatch.setattr(sd, "_FOLDED_LEGS", ())
    monkeypatch.setattr(sd, "_group_em_park_spool_handler", lambda _p: None)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(sd, "_receiver_state_sensor_handler", _noop)
    e = Env(tmp_path)
    e.user()
    return e


def _stop(env: Env, **extra) -> str:
    return json.dumps(asyncio.run(sd._handler({"payload": env.payload(**extra)})))


def test_below_threshold_silent_at_threshold_one_nudge_then_repeat_silent(env):
    env.reads(nr.READ_THRESHOLD - 1)
    assert MARK not in _stop(env)
    env.reads(1)
    out = _stop(env)
    assert MARK in out
    assert PM_TEXT in out
    assert "/coordinator:research" in out and "engine-ask" in out
    env.reads(1)
    assert MARK not in _stop(env)


def test_nudge_is_context_only_never_a_block(env):
    env.reads(nr.READ_THRESHOLD)
    out = asyncio.run(sd._handler({"payload": env.payload()}))
    assert out.get("decision") != "block"
    assert "permissionDecision" not in json.dumps(out)


def test_engine_ask_in_flight_this_turn_is_silent(env):
    env.tool("Workflow", name="coordinator:engine-ask")
    env.reads(nr.READ_THRESHOLD + 3)
    assert MARK not in _stop(env)


def test_research_skill_and_bash_engine_ask_are_in_flight(env):
    env.tool("Skill", skill="coordinator:research")
    env.reads(nr.READ_THRESHOLD)
    assert MARK not in _stop(env)
    env.user("next")
    env.tool("Bash", command="coordinator-invoke engine-ask --ask x")
    env.reads(nr.READ_THRESHOLD)
    assert MARK not in _stop(env)


def test_a_new_user_turn_resets_count_and_fire_flag(env):
    env.tool("Workflow", name="coordinator:engine-ask")
    env.reads(nr.READ_THRESHOLD)
    assert MARK not in _stop(env)
    env.user("a later ask")
    env.reads(nr.READ_THRESHOLD)
    assert "a later ask" in _stop(env)


def test_new_turn_can_nudge_again_after_the_first_turn_nudged(env):
    env.reads(nr.READ_THRESHOLD)
    assert MARK in _stop(env)
    env.user("second ask")
    env.reads(nr.READ_THRESHOLD)
    assert "second ask" in _stop(env)


@pytest.mark.parametrize("tool,inp", [
    ("Read", {"file_path": "docs\\research\\x.md"}),
    ("Read", {"file_path": "E:\\repo\\state\\handoffs\\2026-q4-roadmap.md"}),  # abs-path-ok: Windows path-form fixture
    ("Read", {"file_path": "/home/u/repo/state/improvement-queue/row.yaml"}),
    ("Grep", {"pattern": "x", "path": "corpus/"}),
    ("Grep", {"pattern": "x", "glob": "**/census*.json"}),
    ("Bash", {"command": "cat docs/research/a.md"}),
    ("Bash", {"command": "ls state/improvement-queue/"}),
    ("Bash", {"command": "grep -rn foo corpus/"}),
])
def test_research_paths_count(env, tool, inp):
    for _ in range(nr.READ_THRESHOLD):
        env.tool(tool, **inp)
    assert MARK in _stop(env)


@pytest.mark.parametrize("tool,inp", [
    ("Read", {"file_path": "coordinator_core/ipc.py"}),
    ("Read", {"file_path": "state/handoffs/2026-10-30-plain.md"}),
    ("Grep", {"pattern": "docs/research/", "path": "coordinator_core"}),
    ("Bash", {"command": "python -m pytest docs/research/x.py"}),
    ("Edit", {"file_path": "docs/research/a.md"}),
])
def test_non_research_calls_do_not_count(env, tool, inp):
    for _ in range(nr.READ_THRESHOLD + 2):
        env.tool(tool, **inp)
    assert MARK not in _stop(env)


def test_subagent_chained_stop_and_kill_switch_are_silent(env, monkeypatch):
    env.reads(nr.READ_THRESHOLD)
    assert MARK not in _stop(env, agent_id="sub-1")
    assert MARK not in _stop(env, stop_hook_active=True)
    monkeypatch.setenv("COORDINATOR_HOOK_NUDGE_EM_RESEARCH_SCOPING_DISABLED", "1")
    assert MARK not in _stop(env)


def test_missing_ask_falls_back_to_placeholder_and_unreadable_transcript_is_silent(tmp_path, env):
    (tmp_path / "other").mkdir()
    e = Env(tmp_path / "other")
    e.reads(nr.READ_THRESHOLD)
    assert "<latest PM verbatim>" in _stop(e)
    assert MARK not in _stop(env, transcript_path=str(tmp_path / "absent.jsonl"))


def test_only_the_tail_is_read(env, monkeypatch):
    monkeypatch.setattr(nr, "TAIL_CAP", 4096)
    env.reads(60, path="src/filler.py")
    env.reads(nr.READ_THRESHOLD)
    assert env.transcript.stat().st_size > nr.TAIL_CAP
    out = _stop(env)
    assert MARK in out and "<latest PM verbatim>" in out


def test_posttooluse_dispatcher_no_longer_carries_the_research_leg():
    src = Path(pad.__file__).read_text(encoding="utf-8")
    for name in ("_em_research_scoping_sync", "_RESEARCH_READ_TOOLS", "research_text"):
        assert name not in src
    assert not hasattr(nr, "advisory_text")
