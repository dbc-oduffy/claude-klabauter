"""Pins the Stop-valid advisory envelope for the three Stop-path advisory hooks.

Stop/SubagentStop drop hookSpecificOutput, so each hook's advisory must carry the
top-level reason/systemMessage shape; non-Stop post_advisory output stays PostToolUse.
"""

from __future__ import annotations

import asyncio
import importlib

from coordinator_core import _hook_envelope
from coordinator_core.hooks import guard_manufactured_blocker as gmb
from coordinator_core.hooks import guard_turn_intent as gti

_STOP_DISPATCH = "coordinator_core.hooks.stop_dispatch"


def _assert_stop_valid(envelope: dict, needle: str) -> None:
    assert "hookSpecificOutput" not in envelope, envelope
    assert "decision" not in envelope, envelope
    assert needle in envelope.get("systemMessage", ""), envelope


def test_manufactured_blocker_declarative_stall_advisory_is_stop_valid():
    envelope = gmb._emit_declarative_stall_verdict("Now proceeding with the next batch.")
    _assert_stop_valid(envelope, gmb._DECLARATIVE_STALL_CORRECTION_TEXT[:20])


def test_manufactured_blocker_decidability_advisory_is_stop_valid(monkeypatch):
    monkeypatch.setattr(gmb, "_fails_decidability", lambda _c, _t: True)
    envelope = gmb._emit_decidability_verdict("cand", "text")
    _assert_stop_valid(envelope, gmb._DECIDABILITY_CORRECTION_TEXT[:20])


def test_turn_intent_go_ask_advisory_is_stop_valid(monkeypatch):
    monkeypatch.setattr(gti, "_go_ask_tail", lambda _t: True)
    monkeypatch.setattr(gti, "_group_em_live", lambda _p: True)
    monkeypatch.setattr(gti, "_has_fired_this_session", lambda *_a, **_k: False)
    monkeypatch.setattr(gti, "_mark_fired_this_session", lambda *_a, **_k: None)
    envelope = gti._go_ask_advisory({}, "Should I go ahead?")
    _assert_stop_valid(envelope, gti._GO_ASK_ADVISORY_TEXT[:20])


def test_stop_dispatch_aggregate_advisory_is_stop_valid(monkeypatch):
    mod = importlib.import_module(_STOP_DISPATCH)
    for name in (
        "_runtime_tripwire_em_check_handler",
        "_guard_manufactured_blocker_handler",
        "_guard_terminal_review_handler",
        "_guard_drive_root_new_dir_handler",
        "_stop_em_report_altitude_handler",
        "_nudge_harness_directive_dispatch_handler",
        "_nudge_unrouted_sizing_handler",
        "_workflow_stall_stop_handler",
    ):
        monkeypatch.setattr(mod, name, lambda _p: mod.no_advisory(), raising=True)
    monkeypatch.setattr(
        mod,
        "_watchdog_undischarged_next_move_handler",
        lambda _p: mod.post_advisory("ALPHA-LEG-TEXT"),
        raising=True,
    )
    result = asyncio.run(
        mod._handler({"payload": {"cwd": "", "session_id": "", "transcript_path": ""}})
    )
    _assert_stop_valid(result, "ALPHA-LEG-TEXT")


def test_event_advisory_subagent_stop_has_no_hook_specific_output():
    _assert_stop_valid(_hook_envelope.event_advisory("SubagentStop", "SUB-TEXT"), "SUB-TEXT")


def test_non_stop_post_advisory_shape_unchanged():
    envelope = _hook_envelope.post_advisory("PLAIN")
    hso = envelope["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUse"
    assert "PLAIN" in hso["additionalContext"]
    assert "decision" not in envelope
    assert _hook_envelope.event_advisory("PostToolUse", "PLAIN") == envelope
