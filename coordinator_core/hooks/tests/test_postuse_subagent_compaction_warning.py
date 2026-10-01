"""Behaviour and HOOK_PORT budget tests for hooks.postuse_subagent_compaction_warning."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.benchmarks.budget import resolve_budget  # noqa: E402
from coordinator_core.benchmarks.process_time import in_process_time_ms  # noqa: E402
from coordinator_core.hooks import postuse_subagent_compaction_warning as mod  # noqa: E402

OP = "hooks.postuse_subagent_compaction_warning"
AGENT = "agent-abc123"


@pytest.fixture
def sh(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


def _transcript(tmp_path, tokens, filler=0):
    path = tmp_path / "t.jsonl"
    row = {"message": {"model": "claude-x", "usage": {"input_tokens": tokens}}}
    pad = json.dumps({"type": "filler", "text": "x" * 200}) + "\n"
    path.write_text(pad * filler + json.dumps(row) + "\n", encoding="utf-8")
    return path


def _payload(transcript, env=None, agent_id=AGENT):
    p = {"agent_transcript_path": str(transcript), "env": {"CLAUDE_HOME": "/nonexistent-home"}}
    if agent_id is not None:
        p["agent_id"] = agent_id
    if env:
        p["env"].update(env)
    return p


def _text(result):
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


# 200k window, no pct: threshold 187000. 75% = 140250, 90% = 168300.
def test_no_agent_id_is_silent(sh, tmp_path):
    t = _transcript(tmp_path, 180000)
    assert not _text(mod._handler(_payload(t, agent_id=None)))


def test_below_band_is_silent(sh, tmp_path):
    t = _transcript(tmp_path, 100000)
    assert not _text(mod._handler(_payload(t)))


def test_75_band_fires_once(sh, tmp_path):
    t = _transcript(tmp_path, 150000)
    assert "Context compaction is near" in _text(mod._handler(_payload(t)))
    assert not _text(mod._handler(_payload(t)))


def test_90_band_fires_after_75(sh, tmp_path):
    assert _text(mod._handler(_payload(_transcript(tmp_path, 150000))))
    assert _text(mod._handler(_payload(_transcript(tmp_path, 175000))))
    assert (sh / "state" / "compaction-warned" / f"{AGENT}.2").exists()


def test_pct_in_payload_env_lowers_threshold(sh, tmp_path):
    t = _transcript(tmp_path, 100000)
    assert not _text(mod._handler(_payload(t)))
    assert _text(mod._handler(_payload(t, env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40"})))


def test_pct_in_settings_json_env_lowers_threshold(sh, tmp_path):
    claude_home = tmp_path / "ch"
    (claude_home / ".claude").mkdir(parents=True)
    (claude_home / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "40"}}), encoding="utf-8"
    )
    t = _transcript(tmp_path, 100000)
    assert _text(mod._handler(_payload(t, env={"CLAUDE_HOME": str(claude_home)})))


def test_unreadable_transcript_is_silent(sh, tmp_path):
    assert not _text(mod._handler(_payload(tmp_path / "missing.jsonl")))


def test_process_environ_is_ignored(sh, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "10")
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "1000")
    assert not _text(mod._handler(_payload(_transcript(tmp_path, 100000))))


def test_hook_port_budget(sh, tmp_path):
    t = _transcript(tmp_path, 150000, filler=1500)
    assert t.stat().st_size > mod._TAIL_BYTES
    payload = _payload(t)
    assert _text(mod._handler(payload))  # writes the marker; later calls are the steady state
    assert not _text(mod._handler(payload))
    budget = resolve_budget(OP, "HOOK_PORT")
    limit = budget["target_ms"] * (1 + budget["tolerance"]["value"])
    measured = in_process_time_ms(lambda: mod._handler(payload))["process_time_ms"]
    assert measured <= limit, (measured, limit)
