"""Behavioral tests for coordinator_core.write_guards.block_priority_ledger_edit
-- the priority-ledger directory advisory guard (see the module's own
docstring for the design decision this is the discharge of).

Mirrors the structure of a `block_tracker_edit.py`-shaped guard: no repo
fixture is needed since the match is a pure path-tail regex, independent of
cwd/git-root resolution.

Spec backlink: docs/plans/2026-07-26-priority-ledger.md (chunk C9a)
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import block_priority_ledger_edit as guard


_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_PRIORITY_LEDGER_EDIT"


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(_OVERRIDE_ENV, raising=False)


class TestAdvisesLedgerEdit:
    def test_write_to_ledger_entry_advised(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/priority-ledger/hnd-abc123.yaml",
                "content": "priority: urgent\n",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert "priority-set" in reason
        assert "priority.set" in reason

    def test_advisory_reason_leads_with_route_before_the_downside(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/priority-ledger/hnd-abc123.yaml",
                "content": "priority: urgent\n",
            },
        }

        result = guard.check(payload)
        reason = result["hookSpecificOutput"]["additionalContext"]

        assert reason.index("priority-set") < reason.index("hand-editing")
        assert "override" in reason.lower()

    def test_edit_to_ledger_entry_advised(self):
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "state/priority-ledger/hnd-abc123.yaml",
                "old_string": "priority: high",
                "new_string": "priority: urgent",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_multiedit_to_ledger_entry_advised(self):
        payload = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "state/priority-ledger/hnd-abc123.yaml",
                "edits": [
                    {"old_string": "priority: high", "new_string": "priority: urgent"}
                ],
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_absolute_path_under_resolved_root_advised(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/Users/someone/X/claude-klabauter/state/priority-ledger/hnd-abc123.yaml",
                "content": "priority: urgent\n",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_backslash_path_advised(self):
        """Windows-style separators normalize before the tail match, parity
        with block_tracker_edit.py's F5 normalizer fix."""
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": r"C:\Users\someone\X\claude-klabauter\state\priority-ledger\hnd-abc123.yaml",
                "content": "priority: urgent\n",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]


class TestPassThrough:
    def test_intent_inbox_sibling_dir_passes_through(self):
        """Scope is exactly priority-ledger/ — an adjacent directory under
        the same central root (e.g. the intent inbox) is out of scope."""
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/priority-ledger-intent-inbox/cockpit-001.yaml",
                "content": "target_id: hnd-abc123\n",
            },
        }

        assert guard.check(payload) is None

    def test_ledger_directory_itself_passes_through(self):
        """Matches an entry directly under the directory, not the directory
        node itself (no filename component)."""
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/priority-ledger/",
                "content": "",
            },
        }

        assert guard.check(payload) is None

    def test_unrelated_file_passes_through(self):
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "state/priority-ledger.md",
                "old_string": "a",
                "new_string": "b",
            },
        }

        assert guard.check(payload) is None

    def test_non_matching_tool_passes_through(self):
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "state/priority-ledger/hnd-abc123.yaml"},
        }

        assert guard.check(payload) is None

    def test_override_env_set_passes_through(self, monkeypatch):
        monkeypatch.setenv(_OVERRIDE_ENV, "1")
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/priority-ledger/hnd-abc123.yaml",
                "content": "priority: urgent\n",
            },
        }

        assert guard.check(payload) is None

    def test_missing_file_path_passes_through(self):
        payload = {"tool_name": "Write", "tool_input": {}}

        assert guard.check(payload) is None

    def test_malformed_payload_fails_open(self):
        payload = {"tool_name": "Write", "tool_input": "not-a-dict"}

        assert guard.check(payload) is None
