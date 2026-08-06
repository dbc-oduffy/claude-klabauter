"""Tests for the advisory-fire counter's bash-guard leg (C21,
docs/plans/2026-08-06-apply-guard-class-census.md).

Exercises `dispatch.evaluate_payload_json`'s two advisory-return seams
(`return out` and its suppressed-but-rewrite-leg-preserved sibling
`return emitted`) end-to-end, isolating a single fake `GuardEntry` via
`dispatch._build_guard_chain` monkeypatched -- the same isolation technique
`test_advisory_value_host_default.py` established. Covers AC-C21 in both
directions:

  (a) a flipped guard firing produces exactly one appended
      `{"guard", "at"}` record in
      `state/subagent-share/<session_id>/advisory-fire-counts.jsonl`.
  (b) an induced write failure (an unwritable per-session directory) leaves
      the guard's own returned envelope unchanged.

Also checks the `not fail_closed` gate itself: a `fail_closed=True` entry
reaching the same return site (an annotated hard-deny envelope) must never
produce a counter record.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._advisory_value import AdvisoryValue
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry

_ADVISORY_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "allow",
        "permissionDecisionReason": "advisory note",
    }
}

_DENY_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "deny",
        "permissionDecisionReason": "hard deny",
    }
}


def _payload(session_id="sess-c21", cwd="/tmp"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": "echo probe"},
        "session_id": session_id,
        "cwd": cwd,
    }


def _fake_advisory_entry(name="fake-advisory-guard"):
    return GuardEntry(
        name,
        lambda: dict(_ADVISORY_ENVELOPE),
        False,
        GuardBand.ADVISORY_REWRITE,
        AdvisoryValue.HOST_INDEPENDENT,
    )


def _fake_hard_deny_entry(name="fake-hard-deny-guard"):
    return GuardEntry(
        name,
        lambda: dict(_DENY_ENVELOPE),
        True,
        GuardBand.CONFINEMENT_DENY,
        AdvisoryValue.NOT_COST_ARGUED,
    )


def _counts_path(git_root, session_id):
    return git_root / "state" / "subagent-share" / session_id / "advisory-fire-counts.jsonl"


class TestFlippedGuardProducesOneRecord:
    def test_one_record_appended(self, tmp_path, monkeypatch):
        entry = _fake_advisory_entry()
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )

        payload = _payload(session_id="sess-c21", cwd=str(tmp_path))
        out = dispatch.evaluate_payload_json(json.dumps(payload))

        assert out == _ADVISORY_ENVELOPE
        path = _counts_path(tmp_path, "sess-c21")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert set(record.keys()) == {"guard", "at"}
        assert record["guard"] == "fake-advisory-guard"

    def test_no_record_when_session_id_unresolvable(self, tmp_path, monkeypatch):
        entry = _fake_advisory_entry()
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )

        payload = _payload(session_id="", cwd=str(tmp_path))
        out = dispatch.evaluate_payload_json(json.dumps(payload))

        assert out == _ADVISORY_ENVELOPE
        assert not (tmp_path / "state" / "subagent-share").exists()


class TestHardDenyNeverCounted:
    def test_fail_closed_entry_produces_no_record(self, tmp_path, monkeypatch):
        entry = _fake_hard_deny_entry()
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )

        payload = _payload(session_id="sess-c21", cwd=str(tmp_path))
        out = dispatch.evaluate_payload_json(json.dumps(payload))

        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert not (tmp_path / "state" / "subagent-share").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
class TestWriteFailureLeavesEnvelopeUnchanged:
    def test_unwritable_session_dir_does_not_alter_returned_envelope(self, tmp_path, monkeypatch):
        session_dir = tmp_path / "state" / "subagent-share" / "sess-c21"
        session_dir.mkdir(parents=True)
        original_mode = session_dir.stat().st_mode
        entry = _fake_advisory_entry()
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root",
            lambda cwd=None: str(tmp_path),
        )
        try:
            os.chmod(session_dir, 0o000)
            payload = _payload(session_id="sess-c21", cwd=str(tmp_path))
            out = dispatch.evaluate_payload_json(json.dumps(payload))
            # The guard's own envelope must be returned unchanged -- the
            # write failure inside the counter's own try/except must never
            # turn an advisory into a deny or raise into the caller.
            assert out == _ADVISORY_ENVELOPE
        finally:
            os.chmod(session_dir, original_mode)
