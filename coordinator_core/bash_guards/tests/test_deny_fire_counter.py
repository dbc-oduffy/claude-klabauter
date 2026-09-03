"""Tests for the hard-deny-path counter's bash-guard leg (`docs/plans/
2026-08-13-em-exercisable-in-band-grant-route.md` chunk C6, AC-8).

`record_advisory_fire`'s two call sites both sit on the advisory path
(module docstring of `coordinator_core.guard_advisory_counter`);
`record_deny_fire` mirrors it for the deny path at
`dispatch.evaluate_payload_json`'s in-session-unlock seam -- the single
place a hard-deny envelope (`permissionDecision == "deny"`) is either
returned or cleared, regardless of which registered guard fired.

A fake `GuardEntry` is injected via `_build_guard_chain` monkeypatched, so
firing is deterministic rather than depending on a real guard's trigger
shape (mirrors `write_guards/tests/test_deny_fire_counter.py`'s harness).
"""

from __future__ import annotations

import json

from pathlib import Path

from coordinator_core.bash_guards import dispatch
from coordinator_core.session.machinery_paths import share_dir

_DENY_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "deny",
        "permissionDecisionReason": "hard-deny for testing",
    }
}


def _payload(session_id="sess-c6-bash-deny", cwd=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "session_id": session_id,
    }
    if cwd is not None:
        p["cwd"] = cwd
    return p


def _fake_chain(name="fake-hard-deny-guard"):
    return [
        dispatch.GuardEntry(
            name,
            lambda: dict(_DENY_ENVELOPE),
            True,
            dispatch.GuardBand.CONFINEMENT_DENY,
        )
    ]


def _deny_counts_path(git_root, session_id):
    return Path(share_dir(str(git_root), session_id)) / "deny-fire-counts.jsonl"


class TestUnclearedHardDenyProducesOneRecord:
    def test_one_uncleared_record_appended_and_deny_still_returned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: _fake_chain())
        monkeypatch.setattr(dispatch, "_consume_unlock", lambda session_id, guard_name: False)
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root_cheap",
            lambda cwd=None: str(tmp_path),
        )

        payload_text = json.dumps(_payload(session_id="sess-c6-bash-deny", cwd=str(tmp_path)))
        out = dispatch.evaluate_payload_json(payload_text)

        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        path = _deny_counts_path(tmp_path, "sess-c6-bash-deny")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert set(record.keys()) == {"guard", "session", "at", "cleared"}
        assert record["guard"] == "fake-hard-deny-guard"
        assert record["session"] == "sess-c6-bash-deny"
        assert record["cleared"] is False

    def test_no_record_when_session_id_unresolvable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: _fake_chain())
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root_cheap",
            lambda cwd=None: str(tmp_path),
        )

        payload_text = json.dumps(_payload(session_id="", cwd=str(tmp_path)))
        out = dispatch.evaluate_payload_json(payload_text)

        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert not Path(share_dir(str(tmp_path), "unused")).parent.exists()


class TestClearedHardDenyProducesOneRecordAndChainContinues:
    def test_cleared_record_appended_and_chain_continues(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: _fake_chain())
        monkeypatch.setattr(dispatch, "_consume_unlock", lambda session_id, guard_name: True)
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root_cheap",
            lambda cwd=None: str(tmp_path),
        )

        payload_text = json.dumps(_payload(session_id="sess-c6-bash-cleared", cwd=str(tmp_path)))
        out = dispatch.evaluate_payload_json(payload_text)

        # The cleared hard-deny must not be returned -- chain continues
        # past it, no other guard registered here, so ALLOW (None).
        assert out is None
        path = _deny_counts_path(tmp_path, "sess-c6-bash-cleared")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["cleared"] is True
        assert record["guard"] == "fake-hard-deny-guard"


class TestDenyCounterNeverReadBack:
    def test_module_source_has_no_read_of_deny_counts_file(self):
        import inspect

        src = inspect.getsource(dispatch)
        assert "deny-fire-counts" not in src, (
            "dispatch.py must reference the deny-counts filename only through "
            "guard_advisory_counter.record_deny_fire, never a literal read"
        )
