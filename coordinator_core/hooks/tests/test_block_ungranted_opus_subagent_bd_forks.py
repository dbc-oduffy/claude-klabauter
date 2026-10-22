"""Pins the two verbatim "bd" fork briefs as denied on an Opus parent, at the engine leg
(`block_ungranted_opus_subagent.check`) and the composed seam
(`block_unenumerated_agent_type.check`), with a Sonnet-parent control."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.hooks.block_unenumerated_agent_type as seam_mod
import coordinator_core.hooks.block_ungranted_opus_subagent as opus_mod
from coordinator_core.hooks.tests._bd_fork_briefs import BD_FORK_BRIEFS

_IDS = ["fork1", "fork2"]


def _transcript(tmp_path: Path, model: str) -> str:
    path = tmp_path / "transcript.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "message": {"role": "assistant", "model": model}}) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _fork_payload(brief: dict, transcript: str) -> dict:
    return {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "fork",
            "description": brief["description"],
            "prompt": brief["prompt"],
        },
        "transcript_path": transcript,
    }


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(opus_mod._OVERRIDE_ENV, raising=False)
    monkeypatch.setattr(seam_mod, "resolve_roster", lambda *a, **k: ({"fork", "general-purpose"}, None))
    monkeypatch.setattr("coordinator_core.machine_profile.guard_level", lambda name: "block")


def _assert_denied_naming_phrase(envelope: dict | None) -> None:
    assert envelope is not None
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert "brief says" in reason
    assert "brief says ''" not in reason


@pytest.mark.parametrize("brief", BD_FORK_BRIEFS, ids=_IDS)
def test_engine_leg_denies_bd_fork_on_opus_parent(brief: dict, tmp_path: Path) -> None:
    payload = _fork_payload(brief, _transcript(tmp_path, "claude-opus-4-1-20250805"))
    _assert_denied_naming_phrase(opus_mod.check(payload))


@pytest.mark.parametrize("brief", BD_FORK_BRIEFS, ids=_IDS)
def test_composed_seam_denies_bd_fork_on_opus_parent(brief: dict, tmp_path: Path) -> None:
    payload = _fork_payload(brief, _transcript(tmp_path, "claude-opus-4-1-20250805"))
    _assert_denied_naming_phrase(seam_mod.check(payload))


@pytest.mark.parametrize("brief", BD_FORK_BRIEFS, ids=_IDS)
def test_sonnet_parent_control_passes_at_both_layers(brief: dict, tmp_path: Path) -> None:
    payload = _fork_payload(brief, _transcript(tmp_path, "claude-sonnet-4-5"))
    assert opus_mod.check(payload) is None
    assert seam_mod.check(payload) is None
