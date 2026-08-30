"""Tests for coordinator_core.bash_guards.chain_arrival_ledger.

Covers:
  1. The module's own unit contract in isolation -- one record appended,
     shape, `has_agent_id` raw-presence semantics, empty-session-id no-op,
     unresolvable-git-root no-op, rotation kicking in once the size cap is
     crossed and bounding the on-disk footprint to a fixed number of
     generations, and never-raises on a write failure.
  2. End-to-end wiring through `dispatch.evaluate_payload_json`: a call that
     reaches the chain leaves exactly one arrival record, BEFORE any guard
     runs (so it fires even when the eventual verdict is a hard deny), and a
     non-command `tool_name` (rejected before the chain even builds) leaves
     no record at all -- this instrument records ARRIVAL at the chain, not
     "a Bash tool call happened somewhere".
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from coordinator_core.bash_guards import chain_arrival_ledger as cal
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._advisory_value import AdvisoryValue
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry

_LEDGER_NAME = "chain-arrival-ledger.jsonl"


def _ledger_path(home, session_id):
    """Mirror of the module's own path construction, kept SETTINGS-HOME-rooted:
    a repo-rooted ledger silently writes nothing wherever a git root does not
    resolve, which is the ambiguity the ledger exists to remove.
    """
    return home / "state" / "chain-arrival-ledger" / session_id / _LEDGER_NAME


class TestRecordChainArrivalUnit:
    def test_one_record_appended_with_expected_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)

        cal.record_chain_arrival("sess-1", True, cwd=str(tmp_path))

        path = _ledger_path(tmp_path, "sess-1")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert set(record.keys()) == {"session_id", "at", "has_agent_id", "cwd"}
        assert record["session_id"] == "sess-1"
        assert record["has_agent_id"] is True

    def test_has_agent_id_reflects_raw_presence_not_truthiness_of_kind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)

        cal.record_chain_arrival("sess-2", False, cwd=str(tmp_path))

        record = json.loads(_ledger_path(tmp_path, "sess-2").read_text(encoding="utf-8").strip())
        assert record["has_agent_id"] is False

    def test_no_record_when_session_id_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)

        cal.record_chain_arrival("", True, cwd=str(tmp_path))

        assert not (tmp_path / "state" / "chain-arrival-ledger").exists()

    def test_write_failure_never_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(cal.Path, "open", _boom)

        cal.record_chain_arrival("sess-4", True, cwd=str(tmp_path))  # must not raise


class TestRotation:
    def test_rotation_bounds_generations_and_caps_live_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)
        monkeypatch.setattr(cal, "_ROTATE_MAX_BYTES", 200)
        monkeypatch.setattr(cal, "_ROTATE_GENERATIONS", 2)

        for _ in range(200):
            cal.record_chain_arrival("sess-rot", True, cwd=str(tmp_path))

        session_dir = tmp_path / "state" / "chain-arrival-ledger" / "sess-rot"
        live = session_dir / _LEDGER_NAME
        assert live.exists()
        assert live.stat().st_size < 200 + 512  # one record's worth of slack past the cap

        siblings = sorted(p.name for p in session_dir.glob(_LEDGER_NAME + ".*"))
        # Bounded: never more than _ROTATE_GENERATIONS rotated files on disk.
        assert len(siblings) <= 2
        for name in siblings:
            gen = int(name.rsplit(".", 1)[-1])
            assert 1 <= gen <= 2


class TestDispatchWiring:
    def _payload(self, session_id="sess-wire", agent_id=None, cwd="/tmp"):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "echo probe"},
            "session_id": session_id,
            "cwd": cwd,
        }
        if agent_id is not None:
            payload["agent_id"] = agent_id
        return payload

    def _fake_allow_entry(self):
        return GuardEntry(
            "fake-allow-guard",
            lambda: None,
            False,
            GuardBand.ADVISORY_REWRITE,
            AdvisoryValue.HOST_INDEPENDENT,
        )

    def _fake_deny_entry(self):
        return GuardEntry(
            "fake-deny-guard",
            lambda: {
                "hookSpecificOutput": {
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "denied",
                }
            },
            True,
            GuardBand.CONFINEMENT_DENY,
            AdvisoryValue.NOT_COST_ARGUED,
        )

    def test_arrival_recorded_regardless_of_eventual_deny(self, tmp_path, monkeypatch):
        entry = self._fake_deny_entry()
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)

        payload = self._payload(session_id="sess-wire-deny", agent_id="agent-123", cwd=str(tmp_path))
        out = dispatch.evaluate_payload_json(json.dumps(payload))

        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        path = _ledger_path(tmp_path, "sess-wire-deny")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["has_agent_id"] is True

    def test_arrival_records_em_caller_as_no_agent_id(self, tmp_path, monkeypatch):
        entry = self._fake_allow_entry()
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)

        payload = self._payload(session_id="sess-wire-em", agent_id=None, cwd=str(tmp_path))
        dispatch.evaluate_payload_json(json.dumps(payload))

        record = json.loads(_ledger_path(tmp_path, "sess-wire-em").read_text(encoding="utf-8").strip())
        assert record["has_agent_id"] is False

    def test_no_record_for_non_command_tool_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cal, "settings_home", lambda: tmp_path)

        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "x.py"},
            "session_id": "sess-wire-noncmd",
            "cwd": str(tmp_path),
        }
        out = dispatch.evaluate_payload_json(json.dumps(payload))

        assert out is None
        assert not (tmp_path / "state" / "chain-arrival-ledger").exists()

    def test_recorder_write_failure_never_alters_dispatch_verdict(self, tmp_path, monkeypatch):
        entry = self._fake_allow_entry()
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])

        def _boom(cwd=None):
            raise RuntimeError("resolver blew up")

        monkeypatch.setattr(cal, "settings_home", _boom)

        payload = self._payload(session_id="sess-wire-crash", cwd=str(tmp_path))
        out = dispatch.evaluate_payload_json(json.dumps(payload))

        assert out is None  # the fake allow guard's own verdict, unaffected by the ledger crash
